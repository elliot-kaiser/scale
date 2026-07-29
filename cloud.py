"""Supabase cloud sync — matches the project's SQL schema:

ingredients, daily_logs, weight_logs, recipes, recipe_items,
user_targets, guided_sessions
"""

from __future__ import annotations

import os
import time
from pathlib import Path

_client = None
_init_error = None
_health = {
    "ok": None,
    "writable": None,
    "error": None,
    "hint": None,
    "checked_at": None,
    "skip_reads_until": 0.0,
}

# After a cloud failure, skip remote reads briefly and use local data.
_READ_COOLDOWN_S = 45.0


def load_dotenv(path=None):
    """Minimal .env loader (no python-dotenv dependency)."""
    env_path = Path(path or Path(__file__).resolve().parent / ".env")
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def get_client():
    global _client, _init_error
    if _client is not None:
        return _client

    load_dotenv()
    url = (os.environ.get("SUPABASE_URL") or "").strip()
    key = (os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_ANON_KEY") or "").strip()
    if not url or not key:
        _init_error = "SUPABASE_URL / SUPABASE_KEY not set"
        return None

    try:
        from supabase import create_client
        _client = create_client(url, key)
        _init_error = None
        print(f"Supabase connected: {url}")
        return _client
    except Exception as e:
        _init_error = str(e)
        print(f"Supabase init failed: {e}")
        return None


def is_configured():
    return get_client() is not None


def _set_health(*, ok=None, writable=None, error=None, hint=None):
    now = time.time()
    if ok is not None:
        _health["ok"] = bool(ok)
    if writable is not None:
        _health["writable"] = bool(writable)
    if error is not None:
        _health["error"] = str(error) if error else None
    if hint is not None:
        _health["hint"] = hint
    _health["checked_at"] = now
    if _health["ok"] is False:
        _health["skip_reads_until"] = now + _READ_COOLDOWN_S
    elif _health["ok"] is True:
        _health["skip_reads_until"] = 0.0


def _hint_for_error(message: str) -> str:
    text = (message or "").lower()
    if "403" in text or "forbidden" in text or "row-level security" in text or "rls" in text:
        return (
            "Supabase blocked the anon key (RLS/grants). "
            "Run supabase_schema.sql in the Supabase SQL Editor, then tap Test connection."
        )
    if "jwt" in text or "api key" in text or "invalid" in text:
        return "Check SUPABASE_URL and SUPABASE_KEY (anon) in .env on the Pi."
    return "Cloud unavailable — app keeps using local data/logs.json."


def _record_failure(label: str, error) -> None:
    message = f"{label}: {error}"
    print(f"Supabase {message}")
    _set_health(ok=False, writable=False, error=message, hint=_hint_for_error(str(error)))


def reads_enabled() -> bool:
    """Whether remote reads should be attempted right now."""
    if not is_configured():
        return False
    mode = (os.environ.get("CLOUD_READS") or "auto").strip().lower()
    if mode in ("0", "off", "false", "no", "local"):
        return False
    if mode in ("1", "on", "true", "yes", "force", "always"):
        return True
    # auto
    if _health["ok"] is False and time.time() < float(_health.get("skip_reads_until") or 0):
        return False
    return True


def health_check(force: bool = False) -> dict:
    """Probe read + write access. Safe to call from UI."""
    client = get_client()
    if not client:
        _set_health(
            ok=False,
            writable=False,
            error=_init_error or "Not configured",
            hint="Add SUPABASE_URL and SUPABASE_KEY to .env",
        )
        return status()

    stale = True
    checked = _health.get("checked_at")
    if checked and (time.time() - float(checked)) < 8 and not force:
        stale = False
    if not stale and _health.get("ok") is not None and not force:
        return status()

    # Read probe
    try:
        client.table("ingredients").select("id").limit(1).execute()
        read_ok = True
    except Exception as e:
        _record_failure("health read", e)
        return status()

    # Write probe: upsert a tiny marker row (unique name), then leave it.
    writable = False
    try:
        client.table("ingredients").upsert(
            {
                "name": "__scale_healthcheck__",
                "calories_per_100g": 0,
                "protein_per_100g": 0,
                "carbs_per_100g": 0,
                "fat_per_100g": 0,
            },
            on_conflict="name",
        ).execute()
        writable = True
    except Exception as e:
        _set_health(
            ok=True,
            writable=False,
            error=f"health write: {e}",
            hint=_hint_for_error(str(e)),
        )
        return status()

    _set_health(ok=read_ok, writable=writable, error=None, hint=None)
    return status()


def status():
    client = get_client()
    configured = client is not None
    # Lazy first probe so the pill is accurate after boot
    if configured and _health.get("ok") is None:
        try:
            health_check(force=True)
        except Exception:
            pass
    return {
        "configured": configured,
        "ok": _health.get("ok"),
        "writable": _health.get("writable"),
        "error": _health.get("error") or _init_error,
        "hint": _health.get("hint"),
        "checked_at": _health.get("checked_at"),
        "reads_enabled": reads_enabled(),
        "url": (os.environ.get("SUPABASE_URL") or "").strip() or None,
        "schema": "ingredients / daily_logs / weight_logs",
    }


def _safe(action, label):
    client = get_client()
    if not client:
        return None
    try:
        result = action(client)
        if _health.get("ok") is not True:
            _set_health(ok=True, error=None, hint=None)
        return result
    except Exception as e:
        _record_failure(label, e)
        return None


def _date_str(value):
    if value is None:
        return None
    text = str(value)
    return text.split("T", 1)[0]


def normalize_food(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "barcode": row.get("barcode"),
        "calories_per_100g": float(row.get("calories_per_100g") or 0),
        "protein_per_100g": float(row.get("protein_per_100g") or 0),
        "carbs_per_100g": float(row.get("carbs_per_100g") or 0),
        "fat_per_100g": float(row.get("fat_per_100g") or 0),
        "created_at": row.get("created_at"),
    }


def normalize_meal(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "date": _date_str(row.get("log_date") or row.get("date")),
        "logged_at": row.get("created_at") or row.get("logged_at"),
        "food_name": row.get("food_name"),
        "weight_g": float(row.get("weight_g") or 0),
        "calories": float(row.get("calories") or 0),
        "protein": float(row.get("protein") or 0),
        "carbs": float(row.get("carbs") or 0),
        "fat": float(row.get("fat") or 0),
    }


def normalize_body_weight(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "date": _date_str(row.get("log_date") or row.get("date")),
        "logged_at": row.get("created_at") or row.get("logged_at"),
        "weight_lbs": float(row.get("weight_lbs") or 0),
    }


def upsert_food(entry: dict):
    """Insert/update ingredient by unique name. DB assigns BIGINT id."""
    payload = {
        "name": entry["name"],
        "calories_per_100g": entry["calories_per_100g"],
        "protein_per_100g": entry["protein_per_100g"],
        "carbs_per_100g": entry["carbs_per_100g"],
        "fat_per_100g": entry["fat_per_100g"],
    }
    barcode = entry.get("barcode")
    if barcode:
        payload["barcode"] = str(barcode)

    return _safe(
        lambda c: c.table("ingredients").upsert(payload, on_conflict="name").execute(),
        "food upsert",
    )


def search_foods(query: str, limit: int = 20):
    if not reads_enabled():
        return None
    client = get_client()
    if not client:
        return None
    try:
        q = (query or "").strip()
        if q:
            res = (
                client.table("ingredients")
                .select("*")
                .ilike("name", f"%{q}%")
                .limit(limit)
                .execute()
            )
        else:
            res = (
                client.table("ingredients")
                .select("*")
                .order("name")
                .limit(limit)
                .execute()
            )
        if _health.get("ok") is not True:
            _set_health(ok=True, error=None, hint=None)
        return [normalize_food(r) for r in (res.data or [])]
    except Exception as e:
        _record_failure("food search", e)
        return None


def insert_meal(entry: dict):
    payload = {
        "log_date": entry["date"],
        "food_name": entry["food_name"],
        "weight_g": entry["weight_g"],
        "calories": entry["calories"],
        "protein": entry["protein"],
        "carbs": entry["carbs"],
        "fat": entry["fat"],
    }
    return _safe(
        lambda c: c.table("daily_logs").insert(payload).execute(),
        "meal insert",
    )


def insert_body_weight(entry: dict):
    """One weigh-in per day — upserts on unique log_date."""
    payload = {
        "log_date": entry["date"],
        "weight_lbs": entry["weight_lbs"],
    }
    return _safe(
        lambda c: c.table("weight_logs").upsert(payload, on_conflict="log_date").execute(),
        "body weight upsert",
    )


def fetch_meals():
    if not reads_enabled():
        return None
    client = get_client()
    if not client:
        return None
    try:
        res = client.table("daily_logs").select("*").order("created_at").execute()
        if _health.get("ok") is not True:
            _set_health(ok=True, error=None, hint=None)
        return [normalize_meal(r) for r in (res.data or [])]
    except Exception as e:
        _record_failure("fetch meals", e)
        return None


def fetch_body_weights():
    if not reads_enabled():
        return None
    client = get_client()
    if not client:
        return None
    try:
        res = client.table("weight_logs").select("*").order("created_at").execute()
        if _health.get("ok") is not True:
            _set_health(ok=True, error=None, hint=None)
        return [normalize_body_weight(r) for r in (res.data or [])]
    except Exception as e:
        _record_failure("fetch body weights", e)
        return None


def sync_local_foods_to_cloud(foods: list):
    """Push local food catalog to Supabase (upsert by name)."""
    if not get_client():
        return {"synced": 0, "failed": len(foods or []), "error": _init_error or "Not configured"}
    synced = 0
    failed = 0
    last_error = None
    for food in foods or []:
        if upsert_food(food) is not None:
            synced += 1
        else:
            failed += 1
            last_error = _health.get("error")
    return {
        "synced": synced,
        "failed": failed,
        "error": last_error,
        "writable": _health.get("writable"),
        "hint": _health.get("hint"),
    }


def list_recipes():
    if not reads_enabled():
        return None
    client = get_client()
    if not client:
        return None
    try:
        res = client.table("recipes").select("*").order("name").execute()
        if _health.get("ok") is not True:
            _set_health(ok=True, error=None, hint=None)
        return res.data or []
    except Exception as e:
        _record_failure("list recipes", e)
        return None


def get_recipe(recipe_id):
    client = get_client()
    if not client:
        return None
    try:
        recipe_res = (
            client.table("recipes")
            .select("*")
            .eq("id", recipe_id)
            .limit(1)
            .execute()
        )
        if not recipe_res.data:
            return None
        recipe = recipe_res.data[0]
        items_res = (
            client.table("recipe_items")
            .select("id, raw_weight_g, ingredient_id, ingredients(id, name, calories_per_100g, protein_per_100g, carbs_per_100g, fat_per_100g)")
            .eq("recipe_id", recipe_id)
            .execute()
        )
        items = []
        for row in items_res.data or []:
            ing = row.get("ingredients") or {}
            items.append(
                {
                    "name": ing.get("name") or "Ingredient",
                    "raw_weight_g": float(row.get("raw_weight_g") or 0),
                    "ingredient_id": row.get("ingredient_id") or ing.get("id"),
                    "calories_per_100g": float(ing.get("calories_per_100g") or 0),
                    "protein_per_100g": float(ing.get("protein_per_100g") or 0),
                    "carbs_per_100g": float(ing.get("carbs_per_100g") or 0),
                    "fat_per_100g": float(ing.get("fat_per_100g") or 0),
                }
            )
        return {
            "id": recipe.get("id"),
            "name": recipe.get("name"),
            "total_cooked_weight_g": float(recipe.get("total_cooked_weight_g") or 0),
            "servings": int(recipe.get("servings") or 1),
            "items": items,
            "created_at": recipe.get("created_at"),
        }
    except Exception as e:
        print(f"Supabase get recipe error: {e}")
        return None


def save_recipe(payload: dict):
    """Upsert recipe by unique name and replace recipe_items."""
    client = get_client()
    if not client:
        return None
    try:
        name = payload["name"]
        recipe_payload = {
            "name": name,
            "total_cooked_weight_g": float(payload.get("total_cooked_weight_g") or 0),
            "servings": int(payload.get("servings") or 1),
        }
        upsert = (
            client.table("recipes")
            .upsert(recipe_payload, on_conflict="name")
            .execute()
        )
        recipe_row = (upsert.data or [None])[0]
        if not recipe_row:
            # Some PostgREST configs omit returning rows; fetch by name
            fetched = (
                client.table("recipes")
                .select("*")
                .eq("name", name)
                .limit(1)
                .execute()
            )
            recipe_row = (fetched.data or [None])[0]
        if not recipe_row:
            return None

        recipe_id = recipe_row["id"]
        client.table("recipe_items").delete().eq("recipe_id", recipe_id).execute()

        item_rows = []
        for item in payload.get("items") or []:
            ingredient_id = item.get("ingredient_id")
            if not ingredient_id:
                # Ensure ingredient exists in cloud by name
                food = {
                    "name": item.get("name"),
                    "calories_per_100g": item.get("calories_per_100g") or 0,
                    "protein_per_100g": item.get("protein_per_100g") or 0,
                    "carbs_per_100g": item.get("carbs_per_100g") or 0,
                    "fat_per_100g": item.get("fat_per_100g") or 0,
                }
                upsert_food(food)
                found = (
                    client.table("ingredients")
                    .select("id")
                    .eq("name", food["name"])
                    .limit(1)
                    .execute()
                )
                if found.data:
                    ingredient_id = found.data[0]["id"]
            if not ingredient_id:
                continue
            item_rows.append(
                {
                    "recipe_id": recipe_id,
                    "ingredient_id": ingredient_id,
                    "raw_weight_g": float(item.get("raw_weight_g") or item.get("actual_g") or 0),
                }
            )
        if item_rows:
            client.table("recipe_items").insert(item_rows).execute()

        return get_recipe(recipe_id)
    except Exception as e:
        print(f"Supabase save recipe error: {e}")
        return None


def upsert_guided_session(payload: dict):
    client = get_client()
    if not client:
        return None
    try:
        row = {
            "recipe_title": payload.get("recipe_title") or payload.get("title") or "Recipe",
            "ingredients_json": payload.get("ingredients_json") or payload.get("ingredients") or [],
            "current_step": int(payload.get("current_step") or 0),
            "status": payload.get("status") or "active",
        }
        # Keep a single active session: mark old ones completed, insert new
        client.table("guided_sessions").update({"status": "canceled"}).eq("status", "active").execute()
        res = client.table("guided_sessions").insert(row).execute()
        return (res.data or [None])[0]
    except Exception as e:
        print(f"Supabase guided session error: {e}")
        return None


def update_guided_session(session_id, patch: dict):
    if not session_id:
        return None
    return _safe(
        lambda c: c.table("guided_sessions").update(patch).eq("id", session_id).execute(),
        "guided session update",
    )


def get_targets():
    if not reads_enabled():
        return None
    client = get_client()
    if not client:
        return None
    try:
        res = client.table("user_targets").select("*").eq("id", 1).limit(1).execute()
        if not res.data:
            return None
        if _health.get("ok") is not True:
            _set_health(ok=True, error=None, hint=None)
        row = res.data[0]
        out = {
            "target_calories": float(row.get("target_calories") or 0),
            "target_protein": float(row.get("target_protein") or 0),
            "target_carbs": float(row.get("target_carbs") or 0),
            "target_fat": float(row.get("target_fat") or 0),
        }
        # Only pass percents through if the table actually has them stored
        if row.get("percent_protein") is not None:
            out["percent_protein"] = float(row.get("percent_protein") or 0)
            out["percent_carbs"] = float(row.get("percent_carbs") or 0)
            out["percent_fat"] = float(row.get("percent_fat") or 0)
        return out
    except Exception as e:
        _record_failure("get targets", e)
        return None


def set_targets(payload: dict):
    client = get_client()
    if not client:
        return None
    row = {
        "id": 1,
        "target_calories": float(payload.get("target_calories") or 0),
        "target_protein": float(payload.get("target_protein") or 0),
        "target_carbs": float(payload.get("target_carbs") or 0),
        "target_fat": float(payload.get("target_fat") or 0),
    }
    return _safe(
        lambda c: c.table("user_targets").upsert(row, on_conflict="id").execute(),
        "targets upsert",
    )


def update_meal(meal_id, payload: dict):
    client = get_client()
    if not client:
        return None
    patch = {}
    for key in ("food_name", "weight_g", "calories", "protein", "carbs", "fat"):
        if key in payload and payload[key] is not None:
            patch[key] = payload[key]
    if "date" in payload and payload["date"]:
        patch["log_date"] = payload["date"]
    if not patch:
        return None
    return _safe(
        lambda c: c.table("daily_logs").update(patch).eq("id", meal_id).execute(),
        "meal update",
    )


def delete_meal(meal_id):
    return _safe(
        lambda c: c.table("daily_logs").delete().eq("id", meal_id).execute(),
        "meal delete",
    )


def update_food(food_id, payload: dict):
    client = get_client()
    if not client:
        return None
    patch = {}
    mapping = {
        "name": "name",
        "barcode": "barcode",
        "calories_per_100g": "calories_per_100g",
        "protein_per_100g": "protein_per_100g",
        "carbs_per_100g": "carbs_per_100g",
        "fat_per_100g": "fat_per_100g",
    }
    for src, dest in mapping.items():
        if src in payload and payload[src] is not None:
            patch[dest] = payload[src]
    if not patch:
        return None
    return _safe(
        lambda c: c.table("ingredients").update(patch).eq("id", food_id).execute(),
        "food update",
    )


def delete_food(food_id=None, name=None):
    client = get_client()
    if not client:
        return None
    try:
        if food_id is not None:
            return client.table("ingredients").delete().eq("id", food_id).execute()
        if name:
            return client.table("ingredients").delete().eq("name", name).execute()
    except Exception as e:
        print(f"Supabase food delete error: {e}")
    return None

