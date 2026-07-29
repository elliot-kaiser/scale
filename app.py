import os
import time
import threading
import statistics
import base64
from flask import Flask, render_template, jsonify, request, Response, send_from_directory
import RPi.GPIO as GPIO
import csv
import io
import json
from datetime import datetime, timedelta
from pathlib import Path

from hx711 import HX711
from display_manager import ScaleHardwareManager, G_TO_OZ
import local_store
import cloud
import recipe_math
import weight_plan

cloud.load_dotenv()
supabase = cloud.get_client()
if not supabase:
    print("Supabase not configured — using local data/logs.json (see .env.example)")

# Ensure large common-food catalog is present locally
_seed_info = local_store.ensure_common_foods()
print(f"Food database ready: {_seed_info['total']} foods (+{_seed_info['added']} new)")

app = Flask(__name__)


@app.route("/sw.js")
def service_worker():
    """Serve SW from site root so it can control the whole origin."""
    resp = send_from_directory(Path(app.root_path) / "static", "sw.js", mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# --- HX711 HARDWARE INITIALIZATION ---
hx = HX711(dout_pin=5, pd_sck_pin=6)
_env_ref = float(os.environ.get("REFERENCE_UNIT", "420.0"))
REFERENCE_UNIT = local_store.get_saved_reference_unit(_env_ref)
hx.set_reference_unit(REFERENCE_UNIT)
print(f"Load cell reference unit: {REFERENCE_UNIT}")

print("Zeroing load cell on boot...")
hx.tare()
print("Load cell zeroed!")

# Shared state
state_lock = threading.RLock()
latest_weight_g = 0.0
_smooth_weight_g = 0.0

# Noise filter (grams) — keep responsive; only kill single-sample blips
WEIGHT_OUTLIER_G = 25.0
WEIGHT_EMA_ALPHA = 0.55

# Cook session phases: ingredients → cooked → portion → done
cook_session = {
    "active": False,
    "phase": "idle",
    "title": "",
    "recipe_id": None,
    "guided_session_id": None,
    "ingredients": [],
    "index": 0,
    "cooked_g": None,
    "portion_g": None,
    "batch_macros": None,
    "portion_macros": None,
    "last_portion_macros": None,
    "completed": False,
    "save_recipe": True,
}


def get_live_weight_g():
    """Median + outlier reject + light EMA (keeps responsive, cuts blips)."""
    global latest_weight_g, _smooth_weight_g
    try:
        readings = [hx.get_weight(1) for _ in range(5)]
        med = statistics.median(readings)
        tight = [r for r in readings if abs(r - med) <= WEIGHT_OUTLIER_G]
        if len(tight) >= 3:
            med = statistics.median(tight)

        with state_lock:
            prev = _smooth_weight_g
        smoothed = WEIGHT_EMA_ALPHA * med + (1.0 - WEIGHT_EMA_ALPHA) * prev
        weight = max(0.0, smoothed)
        with state_lock:
            latest_weight_g = weight
            _smooth_weight_g = weight
        return weight
    except Exception as e:
        print(f"Weight read error: {e}")
        return latest_weight_g


def execute_tare():
    """Called when physical Tare button is pressed or via Web UI."""
    global latest_weight_g, _smooth_weight_g
    try:
        hx.tare()
        with state_lock:
            latest_weight_g = 0.0
            _smooth_weight_g = 0.0
        print("Scale Tared!")
    except Exception as e:
        print(f"Tare error: {e}")


def _food_catalog():
    # Always prefer on-device foods for weighing/logging (stable UUID ids).
    return local_store.list_foods()


def _enrich_ingredients(raw_items):
    foods = _food_catalog()
    enriched = []
    for item in raw_items:
        name = (item.get("name") or "Ingredient").strip()
        target = float(item.get("target_g") or item.get("raw_weight_g") or 0)
        food = None
        if item.get("calories_per_100g") is not None and item.get("ingredient_id") is not None:
            food = item
        elif item.get("calories_per_100g") is not None and any(
            item.get(k) is not None for k in ("protein_per_100g", "carbs_per_100g", "fat_per_100g")
        ):
            food = item
        else:
            food = recipe_math.find_best_food(name, foods)

        cal = float((food or {}).get("calories_per_100g") or item.get("calories_per_100g") or 0)
        protein = float((food or {}).get("protein_per_100g") or item.get("protein_per_100g") or 0)
        carbs = float((food or {}).get("carbs_per_100g") or item.get("carbs_per_100g") or 0)
        fat = float((food or {}).get("fat_per_100g") or item.get("fat_per_100g") or 0)
        enriched.append(
            {
                "name": (food or {}).get("name") or name,
                "target_g": target,
                "actual_g": item.get("actual_g"),
                "ingredient_id": (food or {}).get("id") or item.get("ingredient_id"),
                "calories_per_100g": cal,
                "protein_per_100g": protein,
                "carbs_per_100g": carbs,
                "fat_per_100g": fat,
                "matched": bool(food) and (cal > 0 or protein > 0 or carbs > 0 or fat > 0),
                "skippable": bool(item.get("skippable")),
                "skipped": bool(item.get("skipped")),
                "raw": item.get("raw") or name,
            }
        )
    return enriched


def _session_public():
    ings = cook_session["ingredients"]
    index = cook_session["index"]
    current = ings[index] if cook_session["phase"] == "ingredients" and ings and index < len(ings) else None
    return {
        "active": cook_session["active"],
        "phase": cook_session["phase"],
        "title": cook_session["title"],
        "recipe_id": cook_session["recipe_id"],
        "index": index,
        "total": len(ings),
        "ingredients": ings,
        "current": current,
        "cooked_g": cook_session["cooked_g"],
        "portion_g": cook_session["portion_g"],
        "batch_macros": cook_session["batch_macros"],
        "portion_macros": cook_session["portion_macros"] or cook_session.get("last_portion_macros"),
        "completed": cook_session["completed"],
    }


def _show_ingredient_step(index):
    ingredients = cook_session["ingredients"]
    if not ingredients or index >= len(ingredients):
        return False
    item = ingredients[index]
    hw.set_guided_step(
        f"{index + 1}/{len(ingredients)}",
        item.get("name", "Ingredient"),
        float(item.get("target_g") or 0),
    )
    cook_session["index"] = index
    cook_session["phase"] = "ingredients"
    execute_tare()
    if cook_session.get("guided_session_id"):
        cloud.update_guided_session(
            cook_session["guided_session_id"],
            {"current_step": index, "status": "active"},
        )
    return True


def _show_cooked_step():
    cook_session["phase"] = "cooked"
    hw.set_guided_step("Cooked", "Total cooked", 0)
    execute_tare()


def _show_portion_step():
    cook_session["phase"] = "portion"
    target = float(cook_session.get("cooked_g") or 0)
    hw.set_guided_step("Portion", "Your serving", target)
    execute_tare()


def _clear_cook_session(mark_completed=False):
    sid = cook_session.get("guided_session_id")
    if sid:
        cloud.update_guided_session(
            sid,
            {"status": "completed" if mark_completed else "canceled"},
        )
    cook_session.update(
        {
            "active": False,
            "phase": "idle",
            "title": "",
            "recipe_id": None,
            "guided_session_id": None,
            "ingredients": [],
            "index": 0,
            "cooked_g": None,
            "portion_g": None,
            "batch_macros": None,
            "portion_macros": None,
            "last_portion_macros": cook_session.get("portion_macros") if mark_completed else None,
            "completed": mark_completed,
            "save_recipe": True,
        }
    )
    hw.clear_guided_mode()


def confirm_cook_step(weight_g=None, skip=False):
    """Confirm current scale reading for the active cook phase and advance.

    skip=True: skip weighing this ingredient (seasonings). Uses estimated
    target_g for macros when available.
    """
    if weight_g is None:
        with state_lock:
            weight_g = latest_weight_g
    weight_g = max(0.0, float(weight_g))

    if not cook_session["active"]:
        execute_tare()
        return {"status": "idle"}

    phase = cook_session["phase"]

    if phase == "ingredients":
        idx = cook_session["index"]
        ings = cook_session["ingredients"]
        if not ings or idx >= len(ings):
            return {"status": "error", "message": "No active ingredient"}

        if skip:
            estimate = float(ings[idx].get("target_g") or 0)
            ings[idx]["actual_g"] = round(estimate, 1)
            ings[idx]["skipped"] = True
        else:
            if weight_g <= 0:
                return {"status": "error", "message": "Place ingredient on scale first"}
            ings[idx]["actual_g"] = round(weight_g, 1)
            ings[idx]["skipped"] = False

        next_index = idx + 1
        if next_index < len(ings):
            _show_ingredient_step(next_index)
            return {"status": "ok", "phase": "ingredients", "index": next_index}
        cook_session["batch_macros"] = recipe_math.batch_macros(ings)
        _show_cooked_step()
        return {
            "status": "ok",
            "phase": "cooked",
            "batch_macros": cook_session["batch_macros"],
        }

    if phase == "cooked":
        if weight_g <= 0:
            return {"status": "error", "message": "Place cooked food on scale first"}
        cook_session["cooked_g"] = round(weight_g, 1)
        if not cook_session["batch_macros"]:
            cook_session["batch_macros"] = recipe_math.batch_macros(cook_session["ingredients"])
        _show_portion_step()
        return {
            "status": "ok",
            "phase": "portion",
            "cooked_g": cook_session["cooked_g"],
            "batch_macros": cook_session["batch_macros"],
        }

    if phase == "portion":
        if weight_g <= 0:
            return {"status": "error", "message": "Place your portion on scale first"}
        cook_session["portion_g"] = round(weight_g, 1)
        if not cook_session["batch_macros"]:
            cook_session["batch_macros"] = recipe_math.batch_macros(cook_session["ingredients"])
        portion = recipe_math.portion_macros(
            cook_session["batch_macros"],
            cook_session["cooked_g"],
            cook_session["portion_g"],
        )
        cook_session["portion_macros"] = portion

        food_name = f"{cook_session['title']} (portion)"
        meal = local_store.add_meal(
            {
                "food_name": food_name,
                "weight_g": portion["weight_g"],
                "calories": portion["calories"],
                "protein": portion["protein"],
                "carbs": portion["carbs"],
                "fat": portion["fat"],
            }
        )
        cloud.insert_meal(meal)

        if cook_session.get("save_recipe"):
            recipe_payload = {
                "id": cook_session.get("recipe_id"),
                "name": cook_session["title"],
                "total_cooked_weight_g": cook_session["cooked_g"],
                "servings": 1,
                "items": [
                    {
                        **ing,
                        "raw_weight_g": ing.get("actual_g") or ing.get("target_g") or 0,
                    }
                    for ing in cook_session["ingredients"]
                ],
            }
            local_recipe = local_store.save_recipe(recipe_payload)
            cloud_recipe = cloud.save_recipe(local_recipe)
            if cloud_recipe and cloud_recipe.get("id"):
                cook_session["recipe_id"] = cloud_recipe["id"]
            elif local_recipe:
                cook_session["recipe_id"] = local_recipe.get("id")

        result = {
            "status": "ok",
            "phase": "done",
            "portion_macros": portion,
            "batch_macros": cook_session["batch_macros"],
            "cooked_g": cook_session["cooked_g"],
            "portion_g": cook_session["portion_g"],
            "meal": meal,
        }
        _clear_cook_session(mark_completed=True)
        return result

    return {"status": "error", "message": f"Unknown phase {phase}"}


def start_cook_session(title, ingredients, recipe_id=None, save_recipe=True):
    enriched = _enrich_ingredients(ingredients)
    if not enriched:
        raise ValueError("Add at least one ingredient")

    cook_session.update(
        {
            "active": True,
            "phase": "ingredients",
            "title": title or "Recipe",
            "recipe_id": recipe_id,
            "ingredients": enriched,
            "index": 0,
            "cooked_g": None,
            "portion_g": None,
            "batch_macros": None,
            "portion_macros": None,
            "completed": False,
            "save_recipe": bool(save_recipe),
        }
    )
    guided = cloud.upsert_guided_session(
        {
            "recipe_title": cook_session["title"],
            "ingredients_json": enriched,
            "current_step": 0,
            "status": "active",
        }
    )
    cook_session["guided_session_id"] = (guided or {}).get("id")
    _show_ingredient_step(0)
    return _session_public()


def on_tare_or_next():
    """Physical tare button: confirm step during cook, else tare."""
    with state_lock:
        if cook_session["active"]:
            return confirm_cook_step()
    execute_tare()
    return {"status": "tared"}


# --- INITIALIZE HARDWARE MANAGER ---
hw = ScaleHardwareManager(
    tare_callback=execute_tare,
    next_step_callback=on_tare_or_next,
)
hw.start_loop(get_live_weight_g)


# --- FLASK WEB ROUTES ---

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state", methods=["GET"])
def get_state():
    with state_lock:
        weight_g = latest_weight_g
        completed = cook_session["completed"]
        if completed:
            cook_session["completed"] = False
        session = _session_public()
        session["completed"] = completed

    display = hw.get_display_state()
    display_unit = display["unit"]
    display_weight = weight_g * G_TO_OZ if display_unit == "oz" else weight_g

    mode = display["mode"]
    if session["active"]:
        mode = {
            "ingredients": "GUIDED_STEP",
            "cooked": "WEIGH_COOKED",
            "portion": "WEIGH_PORTION",
        }.get(session["phase"], display["mode"])

    return jsonify({
        "weight_raw_g": round(weight_g, 1),
        "weight_display": round(display_weight, 2 if display_unit == "oz" else 1),
        "unit": display_unit,
        "mode": mode,
        "guided": {
            "active": session["active"],
            "phase": session["phase"],
            "index": session["index"],
            "total": session["total"],
            "completed": session["completed"],
            "step_info": display["step_info"],
            "ingredient": display["current_ingredient"],
            "target_g": round(display["target_weight_g"], 1),
            "title": session["title"],
            "cooked_g": session["cooked_g"],
            "portion_g": session["portion_g"],
            "batch_macros": session["batch_macros"],
            "portion_macros": session["portion_macros"],
            "current": session["current"],
        },
        "cook": session,
    })


@app.route("/api/tare", methods=["POST"])
def api_tare():
    execute_tare()
    return jsonify({"status": "success", "message": "Tared"})


@app.route("/api/toggle_unit", methods=["POST"])
def api_toggle_unit():
    hw.toggle_unit()
    return jsonify({"status": "success", "unit": hw.get_display_state()["unit"]})


@app.route("/api/guided_step", methods=["POST"])
def api_set_guided_step():
    data = request.json or {}
    ingredients = data.get("ingredients")
    if ingredients:
        session = start_cook_session(
            data.get("name") or data.get("title") or "Recipe",
            ingredients,
        )
        return jsonify({"status": "success", "cook": session})

    step_info = data.get("step", "1/1")
    name = data.get("name", "Ingredient")
    target = float(data.get("target_g", 100.0))
    hw.set_guided_step(step_info, name, target)
    execute_tare()
    return jsonify({"status": "success"})


@app.route("/api/clear_guided", methods=["POST"])
def api_clear_guided():
    with state_lock:
        _clear_cook_session(mark_completed=False)
    return jsonify({"status": "success"})


@app.route("/api/recipe/advance", methods=["POST"])
@app.route("/api/cook/confirm", methods=["POST"])
def api_confirm_cook_step():
    data = request.json or {}
    weight = data.get("weight_g")
    skip = bool(data.get("skip"))
    with state_lock:
        result = confirm_cook_step(
            None if weight is None else float(weight),
            skip=skip,
        )
        session = _session_public()
    if result.get("status") == "error":
        return jsonify({**result, "cook": session}), 400
    return jsonify({**result, "cook": session})


@app.route("/api/calibrate", methods=["POST"])
def api_calibrate():
    data = request.json or {}
    known_weight_g = float(data.get("known_weight_g", 0))
    if known_weight_g <= 0:
        return jsonify({"status": "error", "message": "known_weight_g must be > 0"}), 400

    raw_readings = [hx.read_raw() for _ in range(10)]
    raw_avg = sum(raw_readings) / len(raw_readings)
    measured = (raw_avg - hx.OFFSET) / hx.SCALE if hx.SCALE else 0
    if measured <= 0:
        return jsonify({"status": "error", "message": "Place calibration weight on scale first"}), 400

    previous_reference = hx.SCALE
    new_reference = hx.SCALE * (measured / known_weight_g)
    hx.set_reference_unit(new_reference)
    local_store.set_saved_reference_unit(new_reference)
    return jsonify({
        "status": "success",
        "reference_unit": round(new_reference, 2),
        "previous_reference_unit": round(previous_reference, 2),
        "persisted": True,
    })


@app.route("/api/calibration", methods=["GET"])
def api_calibration():
    return jsonify({
        "reference_unit": round(float(hx.SCALE or 0), 2),
        "saved_reference_unit": round(local_store.get_saved_reference_unit(REFERENCE_UNIT), 2),
    })


@app.route("/api/ingredients/search", methods=["GET"])
def api_search_ingredients():
    query = request.args.get("q", "").strip()
    # Prefer local catalog so Log/Quick-log ids match Manage foods.
    return jsonify(local_store.search_foods(query, limit=20))


@app.route("/api/ingredients", methods=["GET", "POST"])
def api_ingredients():
    if request.method == "GET":
        # Default to on-device store (UUID ids). Optional ?source=cloud for debugging.
        source = (request.args.get("source") or "local").strip().lower()
        if source in ("cloud", "remote", "supabase"):
            remote = cloud.search_foods("", limit=500)
            if remote is not None:
                return jsonify(remote)
            return jsonify({"status": "error", "message": cloud.status().get("error") or "Cloud unavailable"}), 502
        return jsonify(local_store.list_foods())

    data = request.json or {}
    try:
        entry = local_store.add_food(data)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400

    cloud.upsert_food(entry)
    return jsonify({"status": "success", "food": entry})


@app.route("/api/ingredients/<food_id>", methods=["PUT", "PATCH", "DELETE"])
def api_ingredient_item(food_id):
    if request.method == "DELETE":
        local = local_store.get_food(food_id)
        # Also allow delete by exact name if id is unknown
        if not local and request.args.get("name"):
            name = request.args.get("name")
            for f in local_store.list_foods():
                if (f.get("name") or "").strip().lower() == name.strip().lower():
                    local = f
                    food_id = f.get("id")
                    break
        ok = local_store.delete_food(food_id) if food_id is not None else False
        if local:
            cloud.delete_food(
                food_id=local.get("id") if _is_intish(local.get("id")) else None,
                name=local.get("name"),
            )
        elif _is_intish(food_id):
            cloud.delete_food(food_id=int(food_id))
        if not ok and not cloud.is_configured():
            return jsonify({"status": "error", "message": "Food not found"}), 404
        if not ok and not local and not _is_intish(food_id):
            return jsonify({"status": "error", "message": "Food not found"}), 404
        return jsonify({"status": "success"})

    data = request.json or {}
    # Accept either per_100g fields or calories/protein form fields
    entry = local_store.update_food(food_id, data)
    if entry:
        cloud.upsert_food(entry)
        return jsonify({"status": "success", "food": entry})

    # Cloud-only id (bigint from Supabase list)
    if cloud.is_configured() and _is_intish(food_id):
        patch = {}
        if "name" in data:
            patch["name"] = data["name"]
        for key in ("calories_per_100g", "protein_per_100g", "carbs_per_100g", "fat_per_100g", "barcode"):
            if key in data:
                patch[key] = data[key]
        if "calories" in data and "calories_per_100g" not in patch:
            patch["calories_per_100g"] = data["calories"]
            patch["protein_per_100g"] = data.get("protein", 0)
            patch["carbs_per_100g"] = data.get("carbs", 0)
            patch["fat_per_100g"] = data.get("fat", 0)
        updated = cloud.update_food(int(food_id), patch)
        if updated is None:
            return jsonify({
                "status": "error",
                "message": "Cloud food update failed (check Supabase RLS). Try editing the on-device catalog under Manage foods.",
            }), 502
        return jsonify({"status": "success", "food": {"id": food_id, **patch}})

    return jsonify({"status": "error", "message": "Food not found"}), 404


def _is_intish(value):
    try:
        int(value)
        return True
    except (TypeError, ValueError):
        return False


@app.route("/api/meals/<meal_id>", methods=["PUT", "PATCH", "DELETE"])
def api_meal_item(meal_id):
    if request.method == "DELETE":
        local_store.delete_meal(meal_id)
        if cloud.is_configured() and _is_intish(meal_id):
            cloud.delete_meal(int(meal_id))
        return jsonify({"status": "success"})

    data = request.json or {}
    entry = local_store.update_meal(meal_id, data)
    if cloud.is_configured() and _is_intish(meal_id):
        cloud.update_meal(int(meal_id), data)
    if entry is None and not (cloud.is_configured() and _is_intish(meal_id)):
        return jsonify({"status": "error", "message": "Meal not found"}), 404
    return jsonify({"status": "success", "meal": entry or data})


@app.route("/api/targets", methods=["GET", "PUT", "POST"])
def api_targets():
    if request.method == "GET":
        remote = cloud.get_targets()
        if remote:
            local_store.set_targets(remote)
        return jsonify(local_store.get_targets())

    data = request.get_json(silent=True) or {}
    targets = local_store.set_targets(data)
    cloud.set_targets(targets)
    return jsonify({"status": "success", "targets": targets})


def _day_rows_for_plan(days: int = 35):
    remote_meals = cloud.fetch_meals()
    remote_bw = cloud.fetch_body_weights()
    if remote_meals is not None and remote_bw is not None:
        return local_store.daily_summary(days, meals=remote_meals, body_weights=remote_bw)
    return local_store.daily_summary(days)


@app.route("/api/weight_plan", methods=["GET", "PUT", "POST"])
def api_weight_plan():
    """Trend analysis + optional goal calorie recommendation."""
    if request.method in ("PUT", "POST"):
        data = request.get_json(silent=True) or {}
        goal = local_store.set_weight_goal(data)
    else:
        goal = local_store.get_weight_goal()

    try:
        lookback = int(request.args.get("days", 30))
    except ValueError:
        lookback = 30
    lookback = max(7, min(lookback, 90))

    excluded = local_store.get_plan_excluded_dates()
    rows = _day_rows_for_plan(lookback + 5)
    plan = weight_plan.analyze_weight_plan(
        rows,
        goal_weight_lbs=goal.get("goal_weight_lbs"),
        goal_date=goal.get("goal_date"),
        lookback_days=lookback,
        excluded_dates=excluded,
    )
    plan["saved_goal"] = goal
    return jsonify(plan)


@app.route("/api/weight_plan/exclude_day", methods=["POST"])
def api_weight_plan_exclude_day():
    """Toggle or set whether a day's calories count in the weight plan."""
    data = request.get_json(silent=True) or {}
    date_str = data.get("date")
    if not date_str:
        return jsonify({"status": "error", "message": "date required"}), 400
    excluded = data.get("excluded")
    if excluded is not None:
        excluded = bool(excluded)
    try:
        result = local_store.toggle_plan_excluded_date(date_str, excluded)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400

    goal = local_store.get_weight_goal()
    rows = _day_rows_for_plan(35)
    plan = weight_plan.analyze_weight_plan(
        rows,
        goal_weight_lbs=goal.get("goal_weight_lbs"),
        goal_date=goal.get("goal_date"),
        lookback_days=30,
        excluded_dates=result["excluded_dates"],
    )
    plan["saved_goal"] = goal
    return jsonify({"status": "success", **result, "plan": plan})


@app.route("/api/weight_plan/apply_calories", methods=["POST"])
def api_weight_plan_apply():
    """Set daily calorie target from the plan recommendation; keep macro %."""
    data = request.get_json(silent=True) or {}
    calories = data.get("calories")
    if calories is None:
        return jsonify({"status": "error", "message": "calories required"}), 400
    current = local_store.get_targets()
    targets = local_store.set_targets(
        {
            "target_calories": float(calories),
            "percent_protein": current.get("percent_protein"),
            "percent_carbs": current.get("percent_carbs"),
            "percent_fat": current.get("percent_fat"),
        }
    )
    cloud.set_targets(targets)
    return jsonify({"status": "success", "targets": targets})


@app.route("/api/today", methods=["GET"])
def api_today():
    remote_meals = cloud.fetch_meals()
    remote_targets = cloud.get_targets()
    if remote_targets:
        local_store.set_targets(remote_targets)
    targets = local_store.get_targets()
    progress = local_store.today_progress(meals=remote_meals, targets=targets)
    return jsonify(progress)


@app.route("/api/foods/from_barcode", methods=["POST"])
def api_food_from_barcode():
    """Lookup Open Food Facts by barcode and save to food database."""
    import food_lookup

    data = request.json or {}
    barcode = (data.get("barcode") or "").strip()
    save = data.get("save", True)
    if not barcode:
        return jsonify({"status": "error", "message": "barcode required"}), 400
    try:
        parsed = food_lookup.lookup_open_food_facts(barcode)
        if not save:
            return jsonify({"status": "success", "parsed": parsed, "saved": False})
        entry = local_store.add_food(parsed)
        cloud.upsert_food(entry)
        return jsonify({"status": "success", "food": entry, "parsed": parsed, "saved": True})
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/foods/from_label", methods=["POST"])
def api_food_from_label():
    """Parse a nutrition-label photo with OpenAI vision and save food."""
    import food_lookup

    save = True
    if request.content_type and "application/json" in request.content_type:
        data = request.json or {}
        save = data.get("save", True)
        b64 = data.get("image_base64") or ""
        mime = data.get("mime") or "image/jpeg"
        if "," in b64:
            header, b64 = b64.split(",", 1)
            if "image/" in header:
                mime = header.split("image/")[1].split(";")[0]
                mime = "image/" + mime
        try:
            image_bytes = base64.b64decode(b64)
        except Exception:
            return jsonify({"status": "error", "message": "Invalid image_base64"}), 400
    else:
        upload = request.files.get("image") or request.files.get("file")
        if not upload:
            return jsonify({"status": "error", "message": "image file required"}), 400
        image_bytes = upload.read()
        mime = upload.mimetype or "image/jpeg"
        save = request.form.get("save", "true").lower() != "false"

    try:
        parsed = food_lookup.parse_nutrition_label_image(image_bytes, mime=mime)
        if not save:
            return jsonify({"status": "success", "parsed": parsed, "saved": False})
        entry = local_store.add_food(parsed)
        cloud.upsert_food(entry)
        return jsonify({"status": "success", "food": entry, "parsed": parsed, "saved": True})
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/foods/scan_status", methods=["GET"])
def api_food_scan_status():
    import food_lookup
    return jsonify({
        "barcode": True,
        "nutrition_label": food_lookup.vision_available(),
        "provider": "openai" if food_lookup.vision_available() else None,
    })


@app.route("/api/log_meal", methods=["POST"])
def api_log_meal():
    data = request.json or {}
    # Allow quick-log by food id + optional weight (defaults to live scale)
    food_id = data.get("food_id") or data.get("ingredient_id")
    if food_id is not None and data.get("calories") is None:
        food = None
        for f in _food_catalog():
            if str(f.get("id")) == str(food_id):
                food = f
                break
        if not food:
            return jsonify({"status": "error", "message": "Food not found"}), 404
        weight_g = data.get("weight_g")
        if weight_g is None:
            with state_lock:
                weight_g = latest_weight_g
        weight_g = float(weight_g or 0)
        if weight_g <= 0:
            return jsonify({"status": "error", "message": "Place food on the scale first"}), 400
        factor = weight_g / 100.0
        data = {
            "food_name": food.get("name"),
            "ingredient_id": food.get("id"),
            "weight_g": round(weight_g, 1),
            "calories": round(float(food.get("calories_per_100g") or 0) * factor, 1),
            "protein": round(float(food.get("protein_per_100g") or 0) * factor, 1),
            "carbs": round(float(food.get("carbs_per_100g") or 0) * factor, 1),
            "fat": round(float(food.get("fat_per_100g") or 0) * factor, 1),
            "date": data.get("date"),
        }

    date_override = data.get("date")
    entry = local_store.add_meal(data, date_override=date_override)
    print(f"Logged Meal to Scale: {entry}")
    cloud.insert_meal(entry)
    return jsonify({"status": "success", "message": "Meal logged!", "entry": entry})


@app.route("/api/meals/copy_day", methods=["POST"])
def api_copy_day_meals():
    """Duplicate all meals from one date onto another (default: today)."""
    data = request.get_json(silent=True) or {}
    from_date = (data.get("from_date") or "").strip()
    to_date = (data.get("to_date") or "").strip() or None
    if not from_date:
        # Convenience: copy yesterday → today
        from_date = (datetime.now().astimezone().date() - timedelta(days=1)).isoformat()
    try:
        created = local_store.copy_meals_between_dates(from_date, to_date)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    for entry in created:
        cloud.insert_meal(entry)
    return jsonify({
        "status": "success",
        "from_date": from_date,
        "to_date": to_date or datetime.now().astimezone().date().isoformat(),
        "copied": len(created),
        "meals": created,
    })


@app.route("/api/body_weights", methods=["GET"])
def api_body_weights():
    try:
        days = int(request.args.get("days", 90))
    except ValueError:
        days = 90
    days = max(1, min(days, 365))
    cutoff = (datetime.now().astimezone().date() - timedelta(days=days - 1)).isoformat()
    remote = cloud.fetch_body_weights()
    rows = remote if remote is not None else local_store.get_body_weights()
    filtered = [r for r in rows if str(r.get("date") or "") >= cutoff]
    filtered.sort(key=lambda r: (r.get("date") or "", r.get("logged_at") or ""))
    return jsonify({"weights": filtered})


@app.route("/api/backup", methods=["GET"])
def api_backup():
    store = local_store.export_store()
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M")
    body = json.dumps(store, indent=2)
    return Response(
        body,
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="scale-backup-{stamp}.json"'},
    )


@app.route("/api/export.csv", methods=["GET"])
def api_export_csv():
    kind = (request.args.get("kind") or "meals").strip().lower()
    stamp = datetime.now().astimezone().strftime("%Y%m%d")
    buf = io.StringIO()
    if kind in ("weights", "body_weights", "weight"):
        rows = local_store.body_weights_csv_rows()
        fieldnames = ["date", "logged_at", "weight_lbs"]
        filename = f"scale-weights-{stamp}.csv"
    else:
        rows = local_store.meals_csv_rows()
        fieldnames = ["date", "logged_at", "food_name", "weight_g", "calories", "protein", "carbs", "fat"]
        filename = f"scale-meals-{stamp}.csv"
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/api/restore", methods=["POST"])
def api_restore():
    """Replace local store from uploaded JSON backup. Destructive."""
    payload = None
    if request.files.get("file"):
        raw = request.files["file"].read()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return jsonify({"status": "error", "message": "Invalid JSON backup file"}), 400
    else:
        payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"status": "error", "message": "Send JSON body or multipart file"}), 400
    confirm = request.args.get("confirm") or (payload.get("confirm") if isinstance(payload, dict) else None)
    # Allow nested backup without confirm key inside store
    if isinstance(payload, dict) and "meals" not in payload and isinstance(payload.get("store"), dict):
        confirm = confirm or payload.get("confirm")
        payload = payload["store"]
    if str(confirm).lower() not in ("1", "true", "yes"):
        return jsonify({
            "status": "error",
            "message": "Restore is destructive. Pass ?confirm=true or include confirm:true",
        }), 400
    try:
        # Strip non-store confirm if present
        if isinstance(payload, dict):
            payload = {k: v for k, v in payload.items() if k != "confirm"}
        store = local_store.replace_store(payload)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    return jsonify({
        "status": "success",
        "meals": len(store.get("meals") or []),
        "body_weights": len(store.get("body_weights") or []),
        "foods": len(store.get("foods") or []),
        "recipes": len(store.get("recipes") or []),
    })


@app.route("/api/recent_foods", methods=["GET"])
def api_recent_foods():
    try:
        limit = int(request.args.get("limit", 8))
    except ValueError:
        limit = 8
    limit = max(1, min(limit, 20))
    remote = cloud.fetch_meals()
    foods = _food_catalog()
    items = local_store.recent_foods_for_quick_log(
        limit=limit,
        foods=foods,
        meals=remote if remote is not None else None,
    )
    return jsonify({"foods": items})


@app.route("/api/weekly_review", methods=["GET"])
def api_weekly_review():
    try:
        days = int(request.args.get("days", 7))
    except ValueError:
        days = 7
    days = max(1, min(days, 30))

    remote_meals = cloud.fetch_meals()
    remote_bw = cloud.fetch_body_weights()
    remote_targets = cloud.get_targets()
    if remote_targets:
        local_store.set_targets(remote_targets)
    targets = local_store.get_targets()
    excluded = local_store.get_plan_excluded_dates()

    if remote_meals is not None and remote_bw is not None:
        rows = local_store.daily_summary(max(days, 35), meals=remote_meals, body_weights=remote_bw)
    else:
        rows = local_store.daily_summary(max(days, 35))

    plan = weight_plan.analyze_weight_plan(
        rows,
        lookback_days=30,
        excluded_dates=excluded,
    )
    maintenance = None
    if plan.get("trend") and plan["trend"].get("maintenance_calories") is not None:
        maintenance = plan["trend"]["maintenance_calories"]

    review = weight_plan.weekly_review(
        rows,
        targets,
        days=days,
        excluded_dates=excluded,
        maintenance_calories=maintenance,
    )
    review["plan_ok"] = plan.get("ok")
    return jsonify(review)


@app.route("/api/log_weight", methods=["POST"])
def api_log_weight():
    data = request.json or {}
    entry = local_store.add_body_weight(data)
    print(f"Logged Body Weight: {entry.get('weight_lbs')} lbs")
    cloud.insert_body_weight(entry)
    return jsonify({"status": "success", "message": "Weight saved!", "entry": entry})


@app.route("/api/daily_summary", methods=["GET"])
def api_daily_summary():
    try:
        days = int(request.args.get("days", 60))
    except ValueError:
        days = 60
    days = max(1, min(days, 365))

    remote_meals = cloud.fetch_meals()
    remote_bw = cloud.fetch_body_weights()
    remote_targets = cloud.get_targets()
    if remote_targets:
        local_store.set_targets(remote_targets)
    targets = local_store.get_targets()

    if remote_meals is not None and remote_bw is not None:
        rows = local_store.daily_summary(days, meals=remote_meals, body_weights=remote_bw)
        progress = local_store.today_progress(meals=remote_meals, targets=targets)
        return jsonify({
            "days": rows,
            "source": "supabase",
            "targets": targets,
            "today": progress,
            "plan_excluded_dates": local_store.get_plan_excluded_dates(),
        })

    rows = local_store.daily_summary(days)
    progress = local_store.today_progress(targets=targets)
    return jsonify({
        "days": rows,
        "source": "local",
        "targets": targets,
        "today": progress,
        "plan_excluded_dates": local_store.get_plan_excluded_dates(),
    })


@app.route("/api/daily_detail", methods=["GET"])
def api_daily_detail():
    date_str = (request.args.get("date") or "").strip()
    if not date_str:
        return jsonify({"status": "error", "message": "date required (YYYY-MM-DD)"}), 400

    remote_meals = cloud.fetch_meals()
    remote_bw = cloud.fetch_body_weights()
    excluded = set(local_store.get_plan_excluded_dates())
    if remote_meals is not None and remote_bw is not None:
        detail = local_store.day_detail(date_str, meals=remote_meals, body_weights=remote_bw)
        detail["source"] = "supabase"
    else:
        detail = local_store.day_detail(date_str)
        detail["source"] = "local"
    detail["plan_excluded"] = date_str in excluded
    return jsonify(detail)


@app.route("/api/cloud/status", methods=["GET"])
def api_cloud_status():
    force = str(request.args.get("refresh") or "").lower() in ("1", "true", "yes")
    if force:
        return jsonify(cloud.health_check(force=True))
    return jsonify(cloud.status())


@app.route("/api/cloud/test", methods=["POST"])
def api_cloud_test():
    return jsonify({"status": "success", **cloud.health_check(force=True)})


@app.route("/api/cloud/sync_foods", methods=["POST"])
def api_sync_foods():
    if not cloud.is_configured():
        return jsonify({"status": "error", "message": "Supabase not configured"}), 400
    result = cloud.sync_local_foods_to_cloud(local_store.list_foods())
    if result.get("synced", 0) <= 0 and result.get("failed", 0) > 0:
        return jsonify({
            "status": "error",
            "message": result.get("error") or "Cloud sync failed",
            **result,
        }), 502
    return jsonify({"status": "success", **result})


@app.route("/api/recipe/parse", methods=["POST"])
def api_parse_recipe():
    import recipe_scrape

    data = request.json or {}
    text = (data.get("text") or data.get("recipe") or "").strip()
    url = (data.get("url") or "").strip()
    title = data.get("title") or "Custom recipe"

    # Single-line URL in the text box counts as a link paste
    if not url and recipe_scrape.looks_like_url(text):
        url = text if text.startswith("http") else "https://" + text
        text = ""

    try:
        if url:
            parsed = recipe_scrape.scrape_recipe_url(url)
        else:
            parsed = recipe_scrape.parse_recipe_lines(text, default_title=title)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

    enriched = _enrich_ingredients(parsed["ingredients"])
    return jsonify({
        "status": "success",
        "title": parsed.get("title") or title,
        "ingredients": enriched,
        "source_url": parsed.get("source_url") or url or None,
        "host": parsed.get("host"),
        "yields": parsed.get("yields"),
    })


@app.route("/api/recipe/scrape", methods=["POST"])
def api_scrape_recipe():
    """Alias for /api/recipe/parse — accepts url or free-text."""
    return api_parse_recipe()


@app.route("/api/foods/seed_common", methods=["POST"])
def api_seed_common_foods():
    """Merge the built-in common-foods catalog into local (+ optional Supabase sync)."""
    info = local_store.ensure_common_foods()
    payload = request.get_json(silent=True) or {}
    synced = 0
    sync_meta = {}
    if cloud.is_configured() and payload.get("sync", True):
        sync_meta = cloud.sync_local_foods_to_cloud(local_store.list_foods())
        synced = sync_meta.get("synced", 0)
    return jsonify({"status": "success", **info, "synced": synced, **sync_meta})


@app.route("/api/recipes", methods=["GET", "POST"])
def api_recipes():
    if request.method == "GET":
        remote = cloud.list_recipes()
        if remote is not None:
            return jsonify(remote)
        return jsonify(local_store.list_recipes())

    data = request.json or {}
    try:
        local = local_store.save_recipe(data)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    remote = cloud.save_recipe(local)
    return jsonify({"status": "success", "recipe": remote or local})


@app.route("/api/recipes/<recipe_id>", methods=["GET"])
def api_recipe_detail(recipe_id):
    remote = cloud.get_recipe(recipe_id)
    if remote is not None:
        return jsonify(remote)
    local = local_store.get_recipe(recipe_id)
    if not local:
        return jsonify({"status": "error", "message": "Recipe not found"}), 404
    return jsonify(local)


@app.route("/api/recipe/start_guided", methods=["POST"])
@app.route("/api/cook/start", methods=["POST"])
def api_start_guided():
    data = request.json or {}
    ingredients = data.get("ingredients") or data.get("items") or []
    title = data.get("title") or data.get("name") or "Recipe"
    recipe_id = data.get("recipe_id")
    try:
        with state_lock:
            session = start_cook_session(
                title,
                ingredients,
                recipe_id=recipe_id,
                save_recipe=data.get("save_recipe", True),
            )
        return jsonify({"status": "success", "cook": session})
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400


if __name__ == "__main__":
    try:
        print("\n===========================================")
        print("SMART SCALE BACKEND ACTIVE")
        print("Phone UI: http://scale.local:5000")
        print("===========================================\n")
        app.run(host="0.0.0.0", port=5000, debug=False)
    except KeyboardInterrupt:
        print("\nShutting down Scale Hardware...")
    finally:
        hw.stop()
        hx.clean_up()
        GPIO.cleanup()
