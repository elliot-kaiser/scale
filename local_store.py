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
        data.setdefault("targets", {})
        data.setdefault("weight_goal", {})
        data.setdefault("plan_excluded_dates", [])
        data.setdefault("recent_food_ids", [])
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


def get_food(food_id) -> dict | None:
    with _lock:
        for food in _load().get("foods", []):
            if str(food.get("id")) == str(food_id):
                return food
    return None


def update_food(food_id, payload: dict) -> dict | None:
    with _lock:
        data = _load()
        for i, food in enumerate(data.get("foods", [])):
            if str(food.get("id")) != str(food_id):
                continue
            merged = {**food, **payload, "id": food.get("id")}
            if any(
                k in payload
                for k in (
                    "calories",
                    "protein",
                    "carbs",
                    "fat",
                    "calories_per_100g",
                    "protein_per_100g",
                    "carbs_per_100g",
                    "fat_per_100g",
                    "basis",
                    "serving_size_g",
                    "name",
                )
            ):
                try:
                    # Prefer explicit per_100g fields when present.
                    calories = payload.get("calories", payload.get("calories_per_100g", merged.get("calories_per_100g")))
                    protein = payload.get("protein", payload.get("protein_per_100g", merged.get("protein_per_100g")))
                    carbs = payload.get("carbs", payload.get("carbs_per_100g", merged.get("carbs_per_100g")))
                    fat = payload.get("fat", payload.get("fat_per_100g", merged.get("fat_per_100g")))
                    basis = payload.get("basis") or merged.get("basis") or "per_100g"
                    # If caller sent per_100g keys without basis=per_serving, keep per_100g.
                    if any(k in payload for k in ("calories_per_100g", "protein_per_100g", "carbs_per_100g", "fat_per_100g")):
                        basis = "per_100g"
                    normalized = normalize_food_macros({
                        "name": merged.get("name"),
                        "basis": basis,
                        "calories": calories,
                        "protein": protein,
                        "carbs": carbs,
                        "fat": fat,
                        "serving_size_g": payload.get("serving_size_g", merged.get("serving_size_g") or 100),
                        "barcode": payload.get("barcode", merged.get("barcode")),
                        "id": food.get("id"),
                        "created_at": food.get("created_at"),
                    })
                    data["foods"][i] = normalized
                except ValueError:
                    return None
            else:
                for key in ("name", "barcode", "calories_per_100g", "protein_per_100g", "carbs_per_100g", "fat_per_100g"):
                    if key in payload:
                        data["foods"][i][key] = payload[key]
            _save(data)
            return data["foods"][i]
    return None


def delete_food(food_id) -> bool:
    with _lock:
        data = _load()
        before = len(data.get("foods", []))
        data["foods"] = [f for f in data.get("foods", []) if str(f.get("id")) != str(food_id)]
        if len(data["foods"]) == before:
            return False
        _save(data)
        return True


def update_meal(meal_id, payload: dict) -> dict | None:
    with _lock:
        data = _load()
        for i, meal in enumerate(data.get("meals", [])):
            if str(meal.get("id")) != str(meal_id):
                continue
            for key in ("food_name", "weight_g", "calories", "protein", "carbs", "fat", "date"):
                if key in payload and payload[key] is not None:
                    if key == "food_name":
                        data["meals"][i][key] = str(payload[key])
                    elif key == "date":
                        data["meals"][i][key] = str(payload[key])
                    else:
                        data["meals"][i][key] = float(payload[key])
            _save(data)
            return data["meals"][i]
    return None


def delete_meal(meal_id) -> bool:
    with _lock:
        data = _load()
        before = len(data.get("meals", []))
        data["meals"] = [m for m in data.get("meals", []) if str(m.get("id")) != str(meal_id)]
        if len(data["meals"]) == before:
            # Also allow delete by cloud numeric id stored separately
            return False
        _save(data)
        return True


DEFAULT_TARGETS = {
    "target_calories": 2200.0,
    "target_protein": 165.0,
    "target_carbs": 220.0,
    "target_fat": 73.3,
    "percent_protein": 30.0,
    "percent_carbs": 40.0,
    "percent_fat": 30.0,
}

_PCT_KEYS = ("percent_protein", "percent_carbs", "percent_fat")


def macros_from_calorie_percents(calories: float, p_pct: float, c_pct: float, f_pct: float) -> dict:
    """Convert calorie % split into grams (P/C=4 kcal/g, F=9 kcal/g)."""
    cal = max(0.0, float(calories or 0))
    return {
        "target_protein": _round1(cal * (float(p_pct) / 100.0) / 4.0),
        "target_carbs": _round1(cal * (float(c_pct) / 100.0) / 4.0),
        "target_fat": _round1(cal * (float(f_pct) / 100.0) / 9.0),
    }


def percents_from_macro_grams(calories: float, protein: float, carbs: float, fat: float) -> dict:
    cal = float(calories or 0)
    if cal <= 0:
        return {
            "percent_protein": DEFAULT_TARGETS["percent_protein"],
            "percent_carbs": DEFAULT_TARGETS["percent_carbs"],
            "percent_fat": DEFAULT_TARGETS["percent_fat"],
        }
    return {
        "percent_protein": _round1(float(protein or 0) * 4.0 / cal * 100.0),
        "percent_carbs": _round1(float(carbs or 0) * 4.0 / cal * 100.0),
        "percent_fat": _round1(float(fat or 0) * 9.0 / cal * 100.0),
    }


def get_targets() -> dict:
    with _lock:
        data = _load()
        targets = data.get("targets") or {}
    out = dict(DEFAULT_TARGETS)
    for key in DEFAULT_TARGETS:
        if key in targets and targets[key] is not None:
            out[key] = float(targets[key])
    # Backfill percents from grams when older saves only have grams
    if not any(k in targets for k in _PCT_KEYS):
        out.update(
            percents_from_macro_grams(
                out["target_calories"],
                out["target_protein"],
                out["target_carbs"],
                out["target_fat"],
            )
        )
    return out


def set_targets(payload: dict) -> dict:
    current = get_targets()
    for key in DEFAULT_TARGETS:
        if key in payload and payload[key] is not None:
            try:
                current[key] = float(payload[key])
            except (TypeError, ValueError):
                pass

    has_pct = any(k in payload and payload[k] is not None for k in _PCT_KEYS)
    has_grams = any(
        k in payload and payload[k] is not None
        for k in ("target_protein", "target_carbs", "target_fat")
    )
    cal_only = (
        "target_calories" in payload
        and payload.get("target_calories") is not None
        and not has_pct
        and not has_grams
    )

    if has_pct or cal_only:
        # Calories × % → grams (save form / calorie tweak with stored split)
        current.update(
            macros_from_calorie_percents(
                current["target_calories"],
                current["percent_protein"],
                current["percent_carbs"],
                current["percent_fat"],
            )
        )
    elif has_grams and not has_pct:
        # Gram targets without percents (e.g. cloud sync) → derive %
        current.update(
            percents_from_macro_grams(
                current["target_calories"],
                current["target_protein"],
                current["target_carbs"],
                current["target_fat"],
            )
        )

    with _lock:
        data = _load()
        data["targets"] = current
        _save(data)
    return current


def today_progress(meals=None, targets=None) -> dict:
    targets = targets or get_targets()
    today = datetime.now().astimezone().date().isoformat()
    if meals is None:
        meals = get_meals()
    day = day_detail(today, meals=meals, body_weights=[])
    totals = day["totals"]
    remaining = {
        "calories": _round1(targets["target_calories"] - totals["calories"]),
        "protein": _round1(targets["target_protein"] - totals["protein"]),
        "carbs": _round1(targets["target_carbs"] - totals["carbs"]),
        "fat": _round1(targets["target_fat"] - totals["fat"]),
    }
    return {
        "date": today,
        "targets": targets,
        "consumed": {
            "calories": totals["calories"],
            "protein": totals["protein"],
            "carbs": totals["carbs"],
            "fat": totals["fat"],
            "weight_g": totals["weight_g"],
            "meals_count": totals["meals_count"],
        },
        "remaining": remaining,
    }


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
        "ingredient_id": payload.get("ingredient_id") or payload.get("food_id"),
    }
    with _lock:
        data = _load()
        data["meals"].append(entry)
        # Track recent food ids for quick-log chips
        recent = list(data.get("recent_food_ids") or [])
        fid = entry.get("ingredient_id")
        key = str(fid) if fid is not None else (entry["food_name"] or "").strip().lower()
        if key:
            recent = [r for r in recent if str(r) != key]
            recent.insert(0, key if fid is None else fid)
            data["recent_food_ids"] = recent[:20]
        _save(data)
    return entry


def recent_foods_for_quick_log(limit: int = 8, foods=None, meals=None) -> list:
    """Return recently logged foods with per-100g macros for one-tap logging."""
    foods = foods if foods is not None else list_foods()
    meals = meals if meals is not None else get_meals()
    by_id = {str(f.get("id")): f for f in foods if f.get("id") is not None}
    by_name = {(f.get("name") or "").strip().lower(): f for f in foods}

    with _lock:
        recent_keys = list((_load().get("recent_food_ids") or []))

    ordered = []
    seen = set()

    def _push(food):
        if not food:
            return
        key = str(food.get("id") or (food.get("name") or "").lower())
        if key in seen:
            return
        seen.add(key)
        ordered.append(
            {
                "id": food.get("id"),
                "name": food.get("name"),
                "calories_per_100g": float(food.get("calories_per_100g") or 0),
                "protein_per_100g": float(food.get("protein_per_100g") or 0),
                "carbs_per_100g": float(food.get("carbs_per_100g") or 0),
                "fat_per_100g": float(food.get("fat_per_100g") or 0),
            }
        )

    for key in recent_keys:
        if str(key) in by_id:
            _push(by_id[str(key)])
        elif str(key).lower() in by_name:
            _push(by_name[str(key).lower()])
        if len(ordered) >= limit:
            return ordered

    # Fall back to unique meal names (newest first)
    for meal in sorted(meals, key=lambda m: m.get("logged_at") or "", reverse=True):
        name = (meal.get("food_name") or "").strip()
        if not name or name.startswith("[Sample]"):
            continue
        food = by_name.get(name.lower())
        if food:
            _push(food)
        else:
            # Synthesize from last meal macros scaled to 100g if possible
            w = float(meal.get("weight_g") or 0)
            if w > 0:
                factor = 100.0 / w
                synth = {
                    "id": meal.get("ingredient_id"),
                    "name": name,
                    "calories_per_100g": _round1(float(meal.get("calories") or 0) * factor),
                    "protein_per_100g": _round1(float(meal.get("protein") or 0) * factor),
                    "carbs_per_100g": _round1(float(meal.get("carbs") or 0) * factor),
                    "fat_per_100g": _round1(float(meal.get("fat") or 0) * factor),
                }
                _push(synth)
        if len(ordered) >= limit:
            break
    return ordered


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


DEFAULT_WEIGHT_GOAL = {
    "goal_weight_lbs": None,
    "goal_date": None,
}


def get_weight_goal() -> dict:
    with _lock:
        stored = (_load().get("weight_goal") or {})
    out = dict(DEFAULT_WEIGHT_GOAL)
    if stored.get("goal_weight_lbs") is not None:
        try:
            out["goal_weight_lbs"] = float(stored["goal_weight_lbs"])
        except (TypeError, ValueError):
            pass
    if stored.get("goal_date"):
        out["goal_date"] = str(stored["goal_date"])
    return out


def set_weight_goal(payload: dict) -> dict:
    current = get_weight_goal()
    if "goal_weight_lbs" in payload:
        raw = payload.get("goal_weight_lbs")
        if raw is None or raw == "":
            current["goal_weight_lbs"] = None
        else:
            current["goal_weight_lbs"] = float(raw)
    if "goal_date" in payload:
        raw = payload.get("goal_date")
        current["goal_date"] = str(raw).strip() if raw else None
    with _lock:
        data = _load()
        data["weight_goal"] = current
        _save(data)
    return current


def get_plan_excluded_dates() -> list:
    with _lock:
        raw = _load().get("plan_excluded_dates") or []
    out = []
    seen = set()
    for item in raw:
        text = str(item or "").strip().split("T", 1)[0]
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    out.sort()
    return out


def set_plan_excluded_dates(dates) -> list:
    cleaned = []
    seen = set()
    for item in dates or []:
        text = str(item or "").strip().split("T", 1)[0]
        if text and text not in seen:
            seen.add(text)
            cleaned.append(text)
    cleaned.sort()
    with _lock:
        data = _load()
        data["plan_excluded_dates"] = cleaned
        _save(data)
    return cleaned


def toggle_plan_excluded_date(date_str: str, excluded: bool | None = None) -> dict:
    """Mark/unmark a day as excluded from weight-plan calorie averages."""
    key = str(date_str or "").strip().split("T", 1)[0]
    if not key:
        raise ValueError("date is required")
    current = set(get_plan_excluded_dates())
    if excluded is None:
        excluded = key not in current
    if excluded:
        current.add(key)
    else:
        current.discard(key)
    dates = set_plan_excluded_dates(sorted(current))
    return {"date": key, "excluded": key in set(dates), "excluded_dates": dates}


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


CALIBRATION_PATH = DATA_DIR / "calibration.json"

STORE_LIST_KEYS = (
    "meals",
    "body_weights",
    "foods",
    "recipes",
    "plan_excluded_dates",
    "recent_food_ids",
)
STORE_DICT_KEYS = ("targets", "weight_goal")


def get_saved_reference_unit(default: float = 420.0) -> float:
    try:
        if CALIBRATION_PATH.exists():
            with CALIBRATION_PATH.open("r", encoding="utf-8") as f:
                data = json.load(f)
            value = float(data.get("reference_unit"))
            if value > 0:
                return value
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    return float(default)


def set_saved_reference_unit(value: float) -> float:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    unit = float(value)
    payload = {
        "reference_unit": unit,
        "updated_at": datetime.now().astimezone().isoformat(),
    }
    with CALIBRATION_PATH.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return unit


def export_store() -> dict:
    with _lock:
        data = _load()
        return json.loads(json.dumps(data))


def replace_store(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Backup must be a JSON object")
    store = _empty_store()
    for key in STORE_LIST_KEYS:
        value = payload.get(key, store.get(key, []))
        if not isinstance(value, list):
            raise ValueError(f"{key} must be a list")
        store[key] = value
    for key in STORE_DICT_KEYS:
        value = payload.get(key, {})
        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise ValueError(f"{key} must be an object")
        store[key] = value
    with _lock:
        _save(store)
        return json.loads(json.dumps(store))


def copy_meals_between_dates(from_date: str, to_date: str | None = None) -> list:
    """Clone all meals from from_date onto to_date (default: today)."""
    if not from_date:
        raise ValueError("from_date is required")
    target = to_date or datetime.now().astimezone().date().isoformat()
    with _lock:
        data = _load()
        source = [m for m in data.get("meals", []) if str(m.get("date")) == str(from_date)]
    if not source:
        return []
    created = []
    for meal in source:
        created.append(
            add_meal(
                {
                    "food_name": meal.get("food_name"),
                    "weight_g": meal.get("weight_g"),
                    "calories": meal.get("calories"),
                    "protein": meal.get("protein"),
                    "carbs": meal.get("carbs"),
                    "fat": meal.get("fat"),
                    "ingredient_id": meal.get("ingredient_id"),
                },
                date_override=target,
            )
        )
    return created


def meals_csv_rows() -> list[dict]:
    rows = []
    for meal in get_meals():
        rows.append(
            {
                "date": meal.get("date"),
                "logged_at": meal.get("logged_at"),
                "food_name": meal.get("food_name"),
                "weight_g": meal.get("weight_g"),
                "calories": meal.get("calories"),
                "protein": meal.get("protein"),
                "carbs": meal.get("carbs"),
                "fat": meal.get("fat"),
            }
        )
    rows.sort(key=lambda r: (r.get("date") or "", r.get("logged_at") or ""))
    return rows


def body_weights_csv_rows() -> list[dict]:
    rows = []
    for row in get_body_weights():
        rows.append(
            {
                "date": row.get("date"),
                "logged_at": row.get("logged_at"),
                "weight_lbs": row.get("weight_lbs"),
            }
        )
    rows.sort(key=lambda r: (r.get("date") or "", r.get("logged_at") or ""))
    return rows

