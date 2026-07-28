import os
import time
import threading
import statistics
from flask import Flask, render_template, jsonify, request
import RPi.GPIO as GPIO

from hx711 import HX711
from display_manager import ScaleHardwareManager, G_TO_OZ
import local_store
import cloud
import recipe_math

cloud.load_dotenv()
supabase = cloud.get_client()
if not supabase:
    print("Supabase not configured — using local data/logs.json (see .env.example)")

app = Flask(__name__)

# --- HX711 HARDWARE INITIALIZATION ---
hx = HX711(dout_pin=5, pd_sck_pin=6)
REFERENCE_UNIT = float(os.environ.get("REFERENCE_UNIT", "420.0"))
hx.set_reference_unit(REFERENCE_UNIT)

print("Zeroing load cell on boot...")
hx.tare()
print("Load cell zeroed!")

# Shared state
state_lock = threading.RLock()
latest_weight_g = 0.0

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
    """Median-filtered weight reader passed into display manager loop."""
    global latest_weight_g
    try:
        readings = [hx.get_weight(1) for _ in range(5)]
        weight = max(0.0, statistics.median(readings))
        with state_lock:
            latest_weight_g = weight
        return weight
    except Exception as e:
        print(f"Weight read error: {e}")
        return latest_weight_g


def execute_tare():
    """Called when physical Tare button is pressed or via Web UI."""
    try:
        hx.tare()
        print("Scale Tared!")
    except Exception as e:
        print(f"Tare error: {e}")


def _food_catalog():
    remote = cloud.search_foods("", limit=500)
    if remote is not None:
        return remote
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


def confirm_cook_step(weight_g=None):
    """Confirm current scale reading for the active cook phase and advance."""
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
        if weight_g <= 0:
            return {"status": "error", "message": "Place ingredient on scale first"}

        ings[idx]["actual_g"] = round(weight_g, 1)
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
    with state_lock:
        result = confirm_cook_step(None if weight is None else float(weight))
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
    return jsonify({
        "status": "success",
        "reference_unit": round(new_reference, 2),
        "previous_reference_unit": round(previous_reference, 2),
    })


@app.route("/api/ingredients/search", methods=["GET"])
def api_search_ingredients():
    query = request.args.get("q", "").strip()
    remote = cloud.search_foods(query, limit=20)
    if remote is not None:
        return jsonify(remote)
    return jsonify(local_store.search_foods(query, limit=20))


@app.route("/api/ingredients", methods=["GET", "POST"])
def api_ingredients():
    if request.method == "GET":
        remote = cloud.search_foods("", limit=200)
        if remote is not None:
            return jsonify(remote)
        return jsonify(local_store.list_foods())

    data = request.json or {}
    try:
        entry = local_store.add_food(data)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400

    cloud.upsert_food(entry)
    return jsonify({"status": "success", "food": entry})


@app.route("/api/log_meal", methods=["POST"])
def api_log_meal():
    data = request.json or {}
    entry = local_store.add_meal(data)
    print(f"Logged Meal to Scale: {entry}")
    cloud.insert_meal(entry)
    return jsonify({"status": "success", "message": "Meal logged!", "entry": entry})


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
    if remote_meals is not None and remote_bw is not None:
        rows = local_store.daily_summary(days, meals=remote_meals, body_weights=remote_bw)
        return jsonify({"days": rows, "source": "supabase"})
    return jsonify({"days": local_store.daily_summary(days), "source": "local"})


@app.route("/api/daily_detail", methods=["GET"])
def api_daily_detail():
    date_str = (request.args.get("date") or "").strip()
    if not date_str:
        return jsonify({"status": "error", "message": "date required (YYYY-MM-DD)"}), 400

    remote_meals = cloud.fetch_meals()
    remote_bw = cloud.fetch_body_weights()
    if remote_meals is not None and remote_bw is not None:
        detail = local_store.day_detail(date_str, meals=remote_meals, body_weights=remote_bw)
        detail["source"] = "supabase"
        return jsonify(detail)

    detail = local_store.day_detail(date_str)
    detail["source"] = "local"
    return jsonify(detail)


@app.route("/api/cloud/status", methods=["GET"])
def api_cloud_status():
    return jsonify(cloud.status())


@app.route("/api/cloud/sync_foods", methods=["POST"])
def api_sync_foods():
    if not cloud.is_configured():
        return jsonify({"status": "error", "message": "Supabase not configured"}), 400
    count = cloud.sync_local_foods_to_cloud(local_store.list_foods())
    return jsonify({"status": "success", "synced": count})


@app.route("/api/recipe/parse", methods=["POST"])
def api_parse_recipe():
    data = request.json or {}
    text = data.get("text") or data.get("recipe") or ""
    title = data.get("title") or "Custom recipe"
    parsed = recipe_math.parse_recipe_text(text, default_title=title)
    enriched = _enrich_ingredients(parsed["ingredients"])
    return jsonify({
        "status": "success",
        "title": parsed["title"],
        "ingredients": enriched,
    })


@app.route("/api/recipe/scrape", methods=["POST"])
def api_scrape_recipe():
    """Accept URL or free-text paste; free-text is preferred."""
    data = request.json or {}
    text = data.get("text") or ""
    url = data.get("url", "")
    if text.strip():
        parsed = recipe_math.parse_recipe_text(text)
        enriched = _enrich_ingredients(parsed["ingredients"])
        return jsonify({"status": "success", "title": parsed["title"], "ingredients": enriched})

    # Fallback demo when only a URL is provided
    sample = {
        "status": "success",
        "title": "High-Protein Bowl",
        "ingredients": _enrich_ingredients([
            {"name": "Chicken Breast", "target_g": 150.0},
            {"name": "White Rice (Cooked)", "target_g": 200.0},
            {"name": "Peanut Butter", "target_g": 30.0},
        ]),
        "source_url": url,
        "note": "URL scraping is demo-only; paste ingredient lines for real recipes.",
    }
    return jsonify(sample)


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
