import os
import random
from flask import Flask, render_template, request, jsonify
from supabase import create_client, Client
from recipe_scrapers import scrape_me
from display_manager import ScaleHardwareManager

app = Flask(__name__)

# ---------------------------------------------------------------------------
# 🛠️ Supabase Configuration
# ---------------------------------------------------------------------------
SUPABASE_URL = "https://togypkkxxcyaylaxybcn.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRvZ3lwa2t4eGN5YXlsYXh5YmNuIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODQ5MDUwMDgsImV4cCI6MjEwMDQ4MTAwOH0.ETkN6B_eQGJK_lnu0AjtNx_rNVeb_0HE4NxkpUufwV8"
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ---------------------------------------------------------------------------
# ⚖️ Scale State & Mock Engine
# ---------------------------------------------------------------------------
simulated_tare_offset = 0.0

active_guided_session = {
    "is_active": False,
    "title": "",
    "steps": [],        # list of {"name": str, "target_g": float, "actual_g": float}
    "current_step": 0
}

def read_scale_weight():
    """Simulates live scale readings in grams for testing before HX711 arrives."""
    global simulated_tare_offset
    raw_mock_reading = round(random.uniform(75.0, 85.0), 1)
    return max(0.0, raw_mock_reading - simulated_tare_offset)

def execute_tare():
    """Resets scale reading to 0.0g."""
    global simulated_tare_offset
    simulated_tare_offset = read_scale_weight()
    print("[Scale Engine] Tared to 0.0g")

def execute_next_guided_step():
    """Callback triggered when physical Tare button (GPIO 17) is pressed during Guided Cook."""
    global active_guided_session
    if not active_guided_session["is_active"]:
        return

    current_idx = active_guided_session["current_step"]
    
    # 1. Record actual poured weight from live scale reading
    actual_weight = read_scale_weight()
    active_guided_session["steps"][current_idx]["actual_g"] = actual_weight
    print(f"[Guided Session] Step {current_idx + 1} logged: {actual_weight}g")

    # 2. Auto-tare scale for the next ingredient container
    execute_tare()

    # 3. Advance to next step index
    active_guided_session["current_step"] += 1
    next_idx = active_guided_session["current_step"]

    # 4. Update OLED Display with next ingredient or finalize
    if next_idx < len(active_guided_session["steps"]):
        next_item = active_guided_session["steps"][next_idx]
        hw_manager.set_guided_step(
            step_info=f"{next_idx + 1}/{len(active_guided_session['steps'])}",
            name=next_item["name"],
            target_g=next_item["target_g"]
        )
    else:
        # Complete Guided Session & Clear OLED
        hw_manager.clear_guided_mode()
        active_guided_session["is_active"] = False
        print("[Guided Session] Recipe Completed! Final weights ready for Supabase logging.")

# ---------------------------------------------------------------------------
# 🖥️ Hardware Manager Initialization
# ---------------------------------------------------------------------------
hw_manager = ScaleHardwareManager(
    tare_callback=execute_tare,
    next_step_callback=execute_next_guided_step
)
hw_manager.start_loop(weight_provider_func=read_scale_weight)


# ---------------------------------------------------------------------------
# 🌐 Web UI & API Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html')

# 1. Read live weight & active unit
@app.route('/api/weight', methods=['GET'])
def get_weight():
    return jsonify({
        "weight_g": read_scale_weight(),
        "unit": hw_manager.unit,
        "mode": hw_manager.mode,
        "guided": active_guided_session
    })

# 2. Manual Tare endpoint (from Web UI)
@app.route('/api/tare', methods=['POST'])
def tare_scale():
    execute_tare()
    return jsonify({"status": "success", "message": "Scale tared to 0.0g"})

# 3. Search ingredients in Supabase
@app.route('/api/ingredients/search', methods=['GET'])
def search_ingredients():
    query = request.args.get('q', '')
    if not query:
        return jsonify([])
    
    res = supabase.table('ingredients').select('*').ilike('name', f'%{query}%').limit(8).execute()
    return jsonify(res.data)

# 4. Scrape online recipe URL
@app.route('/api/recipe/scrape', methods=['POST'])
def scrape_recipe():
    data = request.json or {}
    url = data.get('url')
    
    if not url:
        return jsonify({"status": "error", "message": "No URL provided"}), 400

    try:
        scraper = scrape_me(url)
        ingredients_raw = scraper.ingredients()
        
        parsed_ingredients = []
        for item in ingredients_raw:
            parsed_ingredients.append({
                "name": item,
                "target_g": 100.0,  # Estimated target fallback
                "actual_g": 0.0
            })

        return jsonify({
            "status": "success",
            "title": scraper.title(),
            "servings": scraper.yields(),
            "ingredients": parsed_ingredients
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# 5. Start Guided Cooking Session on Scale OLED
@app.route('/api/recipe/start_guided', methods=['POST'])
def start_guided():
    global active_guided_session
    data = request.json or {}
    
    title = data.get("title", "Guided Recipe")
    ingredients = data.get("ingredients", [])

    if not ingredients:
        return jsonify({"status": "error", "message": "No ingredients found"}), 400

    active_guided_session["is_active"] = True
    active_guided_session["title"] = title
    active_guided_session["steps"] = ingredients
    active_guided_session["current_step"] = 0

    # Push Step 1 to OLED Display
    first_item = ingredients[0]
    hw_manager.set_guided_step(
        step_info=f"1/{len(ingredients)}",
        name=first_item["name"],
        target_g=first_item["target_g"]
    )

    return jsonify({"status": "success", "message": "Guided cook mode activated on scale display!"})

# 6. Log meal entry to Supabase daily_logs
@app.route('/api/log_meal', methods=['POST'])
def log_meal():
    data = request.json or {}
    
    entry = {
        "food_name": data.get('food_name', 'Quick Item'),
        "weight_g": float(data.get('weight_g', 0.0)),
        "calories": float(data.get('calories', 0.0)),
        "protein": float(data.get('protein', 0.0)),
        "carbs": float(data.get('carbs', 0.0)),
        "fat": float(data.get('fat', 0.0))
    }
    
    supabase.table('daily_logs').insert(entry).execute()
    return jsonify({"status": "success", "logged": entry})

# 7. Log body weight entry to Supabase weight_logs
@app.route('/api/log_weight', methods=['POST'])
def log_body_weight():
    data = request.json or {}
    weight_lbs = float(data.get('weight_lbs', 0.0))
    
    supabase.table('weight_logs').upsert({
        "weight_lbs": weight_lbs
    }).execute()
    
    return jsonify({"status": "success", "weight_lbs": weight_lbs})

if __name__ == '__main__':
    # Make server reachable across local Wi-Fi network
    app.run(host='0.0.0.0', port=5000, debug=True)