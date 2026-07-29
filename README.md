# Scale — Smart Kitchen Hub

Raspberry Pi kitchen scale with a live OLED readout and a phone-friendly web UI. Weigh food, log macros, cook recipes by the gram, track body weight, and plan calories from your real intake and weight trend.

Runs as a systemd service on the Pi. Open **http://scale.local:5000** (or the Pi’s IP on port `5000`). On your phone, use **Add to Home Screen** for the PWA.

## Features

- **Live scale** — HX711 load cell, tare / unit on the device and in the UI  
- **Quick log** — one-tap recent foods or search; logs whatever is on the scale; **Same as yesterday** copies prior day meals  
- **Food database** — ~160 common foods, barcode lookup (Open Food Facts), optional nutrition-label photo (OpenAI)  
- **Recipe cook** — paste a URL or ingredient list → weigh each item (seasonings skippable) → cooked yield → portion macros  
- **Days log** — daily calories / P / C / F + body weight spreadsheet; edit meal logs or food DB macros from History  
- **Targets** — daily kcal + protein/carbs/fat as calorie percentages; **Auto from goal** fills from the weight plan  
- **Weight plan** — estimates maintenance from logged intake + weigh-ins; goal weight/date → recommended surplus or deficit  
- **Weekly review** — avg intake vs target, weight change, vs maintenance  
- **Body chart** — weigh-ins plotted over ~90 days  
- **Calibrate** — known-weight calibration in the Body tab (saved across restarts)  
- **Backup / export / restore** — full JSON backup, meals & weights CSV, restore from file  
- **PWA** — installable home-screen app with offline shell cache  
- **Optional Supabase** — sync meals, weights, foods, recipes, targets  

## Hardware (BCM pins)

| Part | Pins |
|------|------|
| HX711 DOUT / SCK | GPIO **5** / **6** |
| Tare / next button | GPIO **17** |
| Unit button | GPIO **27** |
| SSD1306 OLED | I2C (`SDA` / `SCL`), 128×64 |

Enable I2C on the Pi (`raspi-config` → Interface Options → I2C). Buttons should be wired to ground with the app’s pull-ups.

## Requirements

- Raspberry Pi (tested on Pi OS with Python 3.13)  
- Python venv with packages such as:  
  `Flask`, `RPi.GPIO`, `gpiozero`, `adafruit-circuitpython-ssd1306`, `Pillow`, `supabase`, `recipe-scrapers`, `beautifulsoup4`  
- Optional: `OPENAI_API_KEY` for label photos  

## Setup

```bash
cd ~/scale
python3 -m venv venv
source venv/bin/activate
pip install Flask RPi.GPIO gpiozero adafruit-blinka adafruit-circuitpython-ssd1306 \
  Pillow supabase recipe-scrapers beautifulsoup4
```

Copy env and edit:

```bash
cp .env.example .env
```

| Variable | Purpose |
|----------|---------|
| `SUPABASE_URL` / `SUPABASE_KEY` | Optional cloud sync (anon key) |
| `OPENAI_API_KEY` | Optional nutrition-label OCR |
| `OPENAI_VISION_MODEL` | Defaults to something like `gpt-4o-mini` |
| `REFERENCE_UNIT` | HX711 calibration fallback if no saved calibration (default `420`) |

Local data lives in `data/logs.json` (meals, weights, foods, recipes, targets, weight goal, exclusions). Calibration is stored in `data/calibration.json` after you calibrate in the UI.

### Calibrate the load cell

**In the UI (preferred):** Body tab → enter known grams (e.g. 500) → tare empty → place the weight → **Calibrate**. The new reference unit is saved and survives restarts.

**Manual / API:**

1. Tare with nothing on the pan.  
2. Place a known weight (e.g. 500 g).  
3. `POST /api/calibrate` with `{ "known_weight_g": 500 }`, or adjust `REFERENCE_UNIT` in `.env`.  
4. Restart only needed if you changed `.env` by hand.

## Run

**Preferred — systemd (starts on boot):**

```bash
./deploy/install_service.sh
# or
sudo systemctl restart scale
sudo systemctl status scale
journalctl -u scale -f
```

**Manual** (stop the service first so GPIO isn’t double-claimed):

```bash
sudo systemctl stop scale
source venv/bin/activate
python app.py
```

UI: `http://scale.local:5000` or `http://<pi-ip>:5000`

## Web UI tabs

| Tab | What it does |
|-----|----------------|
| *(header)* | Live weight, tare, unit, **Quick log**, Same as yesterday |
| **Cook** | Scrape / paste / saved recipes → guided weighing |
| **Foods** | **Log · Manage · Add** — weigh-log, edit catalog, add foods |
| **Body** | Log lbs + weight chart |
| **Progress** | **Today · History · Targets** (weight goal + daily targets on one Targets screen) |
| **Maint** | Calibrate, cloud sync status, backup / export / restore |

### Recipe tips

- Count items like **eggs** get gram estimates (e.g. large egg ≈ 50 g).  
- Seasonings (salt, pepper, spices, …) are marked **skippable**; toggle per ingredient before cooking.  
- During cook: confirm on the phone or the physical tare/next button; use **Skip** for seasonings.

### Weight plan tips

- Needs at least two weigh-ins and some food logs.  
- Empty days are ignored (not counted as 0 kcal).  
- Mark incomplete tracking days **Do not count** in the day detail panel.  
- On Targets, **Auto from goal** fills calories + macro % from the plan.

### History tips

- Tap a day to edit a meal log, **Edit food** (catalog macros per 100 g), or **Copy meals to today**.

### Install as app (PWA)

On iPhone/Android, open the UI in Safari/Chrome → Share / menu → **Add to Home Screen**. The shell caches for offline open; logging still needs the Pi on the network.

## API (selected)

| Method | Path | Notes |
|--------|------|--------|
| `GET` | `/api/state` | Live weight + cook session |
| `POST` | `/api/tare` | Zero the scale |
| `POST` | `/api/calibrate` | `{ "known_weight_g" }` — persists reference unit |
| `GET` | `/api/calibration` | Current / saved reference unit |
| `POST` | `/api/log_meal` | Log meal; or `{ "food_id", "weight_g?" }`; optional `date` |
| `POST` | `/api/meals/copy_day` | `{ "from_date?", "to_date?" }` — defaults yesterday → today |
| `PUT`/`DELETE` | `/api/meals/<id>` | Edit / delete a meal log |
| `GET` | `/api/recent_foods` | Chips for quick log |
| `GET` | `/api/body_weights` | Weigh-ins for chart (`?days=90`) |
| `POST` | `/api/log_weight` | Body weigh-in (lbs) |
| `GET` | `/api/daily_summary` | History sheet rows |
| `GET` | `/api/daily_detail` | Meals for one date |
| `GET` | `/api/weekly_review` | Last 7 days summary |
| `GET`/`PUT` | `/api/weight_plan` | Trend + goal calories |
| `POST` | `/api/weight_plan/exclude_day` | Exclude a day from calorie average |
| `POST` | `/api/weight_plan/apply_calories` | Set calorie target from plan |
| `GET`/`PUT` | `/api/targets` | Daily macro targets |
| `GET` | `/api/backup` | Download full `logs.json` backup |
| `GET` | `/api/export.csv` | `?kind=meals` or `weights` |
| `POST` | `/api/restore?confirm=true` | Replace local store from JSON |
| `POST` | `/api/recipe/parse` | `{ "url" }` or `{ "text" }` |
| `POST` | `/api/cook/start` / `/api/cook/confirm` | Guided cook (`skip: true` allowed) |

## Supabase (optional)

1. Create a project and tables matching your app (`ingredients`, `daily_logs`, `weight_logs`, `recipes`, `recipe_items`, `user_targets`, `guided_sessions`).  
2. Put URL + **anon** key in `.env`.  
3. In Supabase **SQL Editor**, run `supabase_schema.sql` (grants + RLS policies). Without this, the Pi gets **403 Forbidden** and stays local-only.  
4. On the phone UI → **Maint → Cloud sync → Test connection**.  

If cloud is blocked, logging still works from `data/logs.json`. The header pill shows **Cloud OK**, **Cloud down**, or **Local only**.

Optional: `CLOUD_READS=off` forces local reads even when cloud is configured.

## Project layout

```
app.py                 Flask API + hardware wiring
hx711.py               Load cell driver (median reads)
display_manager.py     OLED + GPIO buttons
local_store.py         Local JSON persistence
cloud.py               Supabase client
recipe_scrape.py       URL / line parsing (eggs, skippable seasonings)
recipe_math.py         Cook-session macros
weight_plan.py         Maintenance + goal + weekly review
food_lookup.py         Barcode + label helpers
common_foods.py        Seed catalog
templates/index.html   Phone UI
static/                PWA manifest, icons, service worker
deploy/                systemd unit + install script
data/logs.json         On-device database
data/calibration.json  Saved load-cell reference unit
```

## Ops notes

- After code changes: `sudo systemctl restart scale`  
- Don’t run `python app.py` while the service is active (`GPIO` busy).  
- OLED layout: yellow top band (~16 rows) for guided chrome; blue band for the large weight digits.  
- Hard-refresh the phone browser after UI changes (or re-open the PWA).
