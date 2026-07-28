"""Local JSON persistence for foods, meals, and body-weight logs.

Always the source of truth on-device. Supabase mirrors these when configured.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path

from common_foods import as_food_dicts

DATA_DIR = Path(__file__).resolve().parent / "data"
STORE_PATH = DATA_DIR / "logs.json"

_lock = threading.Lock()

DEFAULT_FOODS = as_food_dicts()


def _empty_store():
    return {"meals": [], "body_weights": [], "foods": [], "recipes": []}


def _food_entry(item):
    return {
        "id": str(uuid.uuid4()),
        "name": item["name"],
        "barcode": item.get("barcode"),
        "calories_per_100g": float(item["calories_per_100g"]),
        "protein_per_100g": float(item["protein_per_100g"]),
        "carbs_per_100g": float(item["carbs_per_100g"]),
        "fat_per_100g": float(item["fat_per_100g"]),
        "serving_size_g": 100.0,
        "basis": "per_100g",
        "created_at": datetime.now().astimezone().isoformat(),
        "source": item.get("source") or "common_foods",
    }


def _seed_foods(_unused=None):
    return [_food_entry(item) for item in DEFAULT_FOODS]


def ensure_common_foods() -> dict:
    """Merge any missing common foods into the local catalog (by name)."""
    added = 0
    with _lock:
        data = _load()
        existing = {(f.get("name") or "").strip().lower() for f in data.get("foods", [])}
        for item in DEFAULT_FOODS:
            key = item["name"].strip().lower()
            if key in existing:
                continue
            data["foods"].append(_food_entry(item))
            existing.add(key)
            added += 1
        if added:
            _save(data)
        total = len(data.get("foods", []))
    return {"added": added, "total": total}


def _load():
    if not STORE_PATH.exists():
        store = _empty_store()
        store["foods"] = _seed_foods([])
        _save(store)
        return store
    try:
        with STORE_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _empty_store()
        data.setdefault("meals", [])
        data.setdefault("body_weights", [])
        data.setdefault("foods", [])
        data.setdefault("recipes", [])
        if not data["foods"]:
            data["foods"] = _seed_foods([])
            _save(data)
        return data
    except (json.JSONDecodeError, OSError):
        return _empty_store()


def _save(data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(STORE_PATH)


def _now_parts(date_override=None):
    now = datetime.now().astimezone()
    date_str = date_override or now.date().isoformat()
    return date_str, now.isoformat()


def _round1(value):
    return round(float(value or 0), 1)


def normalize_food_macros(payload: dict) -> dict:
    """Accept per_100g or per_package/per_serving macros; store as per-100g."""
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValueError("Food name is required")

    basis = (payload.get("basis") or "per_100g").strip().lower()
    if basis in ("per_package", "package", "serving", "per_serving"):
        basis = "per_serving"

    calories = float(payload.get("calories") or 0)
    protein = float(payload.get("protein") or 0)
    carbs = float(payload.get("carbs") or 0)
    fat = float(payload.get("fat") or 0)

    if basis == "per_100g":
        serving_size_g = float(payload.get("serving_size_g") or 100.0)
        cal100, p100, c100, f100 = calories, protein, carbs, fat
    else:
        serving_size_g = float(payload.get("serving_size_g") or payload.get("package_g") or 0)
        if serving_size_g <= 0:
            raise ValueError("Package/serving weight in grams is required")
        factor = 100.0 / serving_size_g
        cal100 = calories * factor
        p100 = protein * factor
        c100 = carbs * factor
        f100 = fat * factor

    return {
        "id": payload.get("id") or str(uuid.uuid4()),
        "name": name,
        "barcode": (payload.get("barcode") or None),
        "calories_per_100g": _round1(cal100),
        "protein_per_100g": _round1(p100),
        "carbs_per_100g": _round1(c100),
        "fat_per_100g": _round1(f100),
        "serving_size_g": _round1(serving_size_g),
        "basis": basis,
        "created_at": payload.get("created_at") or datetime.now().astimezone().isoformat(),
        "source": payload.get("source"),
    }


def add_food(payload: dict) -> dict:
    entry = normalize_food_macros(payload)
    with _lock:
        data = _load()
        data["foods"].append(entry)
        _save(data)
    return entry


def search_foods(query: str, limit: int = 20) -> list:
    q = (query or "").strip().lower()
    with _lock:
        foods = list(_load().get("foods", []))
    if not q:
        return foods[:limit]
    matches = [f for f in foods if q in (f.get("name") or "").lower()]
    return matches[:limit]


def list_foods() -> list:
    with _lock:
        return list(_load().get("foods", []))


def add_meal(payload: dict, date_override=None) -> dict:
    date_str, logged_at = _now_parts(date_override)
    entry = {
        "id": str(uuid.uuid4()),
        "date": date_str,
        "logged_at": logged_at,
        "food_name": payload.get("food_name") or payload.get("name") or "Unknown",
        "weight_g": float(payload.get("weight_g") or 0),
        "calories": float(payload.get("calories") or 0),
        "protein": float(payload.get("protein") or 0),
        "carbs": float(payload.get("carbs") or 0),
        "fat": float(payload.get("fat") or 0),
    }
    with _lock:
        data = _load()
        data["meals"].append(entry)
        _save(data)
    return entry


def add_body_weight(payload: dict, date_override=None) -> dict:
    date_str, logged_at = _now_parts(date_override)
    entry = {
        "id": str(uuid.uuid4()),
        "date": date_str,
        "logged_at": logged_at,
        "weight_lbs": float(payload.get("weight_lbs") or 0),
    }
    with _lock:
        data = _load()
        data["body_weights"].append(entry)
        _save(data)
    return entry


def get_meals() -> list:
    with _lock:
        return list(_load().get("meals", []))


def get_body_weights() -> list:
    with _lock:
        return list(_load().get("body_weights", []))


def _aggregate(meals, body_weights, days: int = 60):
    by_date = {}

    for meal in meals:
        d = meal.get("date")
        if not d:
            continue
        if isinstance(d, str) and "T" in d:
            d = d.split("T", 1)[0]
        row = by_date.setdefault(
            d,
            {
                "date": d,
                "calories": 0.0,
                "protein": 0.0,
                "carbs": 0.0,
                "fat": 0.0,
                "weight_g": 0.0,
                "meals_count": 0,
                "body_weight_lbs": None,
                "_bw_logged_at": None,
            },
        )
        row["calories"] += float(meal.get("calories") or 0)
        row["protein"] += float(meal.get("protein") or 0)
        row["carbs"] += float(meal.get("carbs") or 0)
        row["fat"] += float(meal.get("fat") or 0)
        row["weight_g"] += float(meal.get("weight_g") or 0)
        row["meals_count"] += 1

    for bw in body_weights:
        d = bw.get("date")
        if not d:
            continue
        if isinstance(d, str) and "T" in d:
            d = d.split("T", 1)[0]
        row = by_date.setdefault(
            d,
            {
                "date": d,
                "calories": 0.0,
                "protein": 0.0,
                "carbs": 0.0,
                "fat": 0.0,
                "weight_g": 0.0,
                "meals_count": 0,
                "body_weight_lbs": None,
                "_bw_logged_at": None,
            },
        )
        logged_at = bw.get("logged_at") or ""
        if row["_bw_logged_at"] is None or logged_at >= row["_bw_logged_at"]:
            row["body_weight_lbs"] = float(bw.get("weight_lbs") or 0)
            row["_bw_logged_at"] = logged_at

    rows = []
    for d in sorted(by_date.keys(), reverse=True):
        row = by_date[d]
        rows.append(
            {
                "date": row["date"],
                "calories": _round1(row["calories"]),
                "protein": _round1(row["protein"]),
                "carbs": _round1(row["carbs"]),
                "fat": _round1(row["fat"]),
                "weight_g": _round1(row["weight_g"]),
                "meals_count": row["meals_count"],
                "body_weight_lbs": (
                    _round1(row["body_weight_lbs"])
                    if row["body_weight_lbs"] is not None
                    else None
                ),
            }
        )
        if days > 0 and len(rows) >= days:
            break
    return rows


def daily_summary(days: int = 60, meals=None, body_weights=None):
    if meals is None or body_weights is None:
        with _lock:
            data = _load()
            meals = data["meals"] if meals is None else meals
            body_weights = data["body_weights"] if body_weights is None else body_weights
    return _aggregate(meals, body_weights, days)


def day_detail(date_str: str, meals=None, body_weights=None):
    if meals is None or body_weights is None:
        with _lock:
            data = _load()
            meals = data["meals"] if meals is None else meals
            body_weights = data["body_weights"] if body_weights is None else body_weights

    def _date_of(item):
        d = item.get("date") or ""
        return d.split("T", 1)[0] if isinstance(d, str) else str(d)

    day_meals = [m for m in meals if _date_of(m) == date_str]
    day_meals.sort(key=lambda m: m.get("logged_at") or "")
    day_bw = [b for b in body_weights if _date_of(b) == date_str]
    day_bw.sort(key=lambda b: b.get("logged_at") or "")

    totals = {
        "calories": _round1(sum(float(m.get("calories") or 0) for m in day_meals)),
        "protein": _round1(sum(float(m.get("protein") or 0) for m in day_meals)),
        "carbs": _round1(sum(float(m.get("carbs") or 0) for m in day_meals)),
        "fat": _round1(sum(float(m.get("fat") or 0) for m in day_meals)),
        "weight_g": _round1(sum(float(m.get("weight_g") or 0) for m in day_meals)),
        "meals_count": len(day_meals),
        "body_weight_lbs": (
            _round1(day_bw[-1]["weight_lbs"]) if day_bw else None
        ),
    }
    return {
        "date": date_str,
        "meals": day_meals,
        "body_weights": day_bw,
        "totals": totals,
    }


def list_recipes() -> list:
    with _lock:
        recipes = list(_load().get("recipes", []))
    recipes.sort(key=lambda r: r.get("name") or "")
    return recipes


def get_recipe(recipe_id) -> dict | None:
    with _lock:
        for recipe in _load().get("recipes", []):
            if str(recipe.get("id")) == str(recipe_id):
                return recipe
    return None


def save_recipe(payload: dict) -> dict:
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValueError("Recipe name is required")
    items = payload.get("items") or payload.get("ingredients") or []
    normalized_items = []
    for item in items:
        normalized_items.append(
            {
                "name": item.get("name") or "Ingredient",
                "raw_weight_g": float(item.get("raw_weight_g") or item.get("actual_g") or item.get("target_g") or 0),
                "ingredient_id": item.get("ingredient_id"),
                "calories_per_100g": float(item.get("calories_per_100g") or 0),
                "protein_per_100g": float(item.get("protein_per_100g") or 0),
                "carbs_per_100g": float(item.get("carbs_per_100g") or 0),
                "fat_per_100g": float(item.get("fat_per_100g") or 0),
            }
        )
    entry = {
        "id": payload.get("id") or str(uuid.uuid4()),
        "name": name,
        "total_cooked_weight_g": float(payload.get("total_cooked_weight_g") or 0),
        "servings": int(payload.get("servings") or 1),
        "items": normalized_items,
        "created_at": payload.get("created_at") or datetime.now().astimezone().isoformat(),
    }
    with _lock:
        data = _load()
        existing = next((i for i, r in enumerate(data["recipes"]) if str(r.get("id")) == str(entry["id"]) or (r.get("name") or "").lower() == name.lower()), None)
        if existing is not None:
            entry["id"] = data["recipes"][existing].get("id", entry["id"])
            data["recipes"][existing] = entry
        else:
            data["recipes"].append(entry)
        _save(data)
    return entry

