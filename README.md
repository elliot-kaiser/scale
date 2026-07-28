# Scale — Smart Kitchen Hub

Raspberry Pi kitchen scale with a live OLED readout and a phone-friendly web UI. Weigh food, log macros, cook recipes by the gram, track body weight, and plan calories from your real intake and weight trend.

Runs as a systemd service on the Pi. Open **http://scale.local:5000** (or the Pi’s IP on port `5000`).

## Features

- **Live scale** — HX711 load cell, tare / unit on the device and in the UI  
- **Quick log** — one-tap recent foods or search; logs whatever is on the scale  
- **Food database** — ~160 common foods, barcode lookup (Open Food Facts), optional nutrition-label photo (OpenAI)  
- **Recipe cook** — paste a URL or ingredient list → weigh each item (seasonings skippable) → cooked yield → portion macros  
- **Days log** — daily calories / P / C / F + body weight spreadsheet  
- **Targets** — daily kcal + protein/carbs/fat as calorie percentages  
- **Weight plan** — estimates maintenance from logged intake + weigh-ins; goal weight/date → recommended surplus or deficit  
- **Weekly review** — avg intake vs target, weight change, vs maintenance  
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
| `REFERENCE_UNIT` | HX711 calibration scale (default `420`) |

Local data lives in `data/logs.json` (meals, weights, foods, targets, weight goal, exclusions).

### Calibrate the load cell

1. Tare with nothing on the pan.  
2. Place a known weight (e.g. 500 g).  
3. Adjust `REFERENCE_UNIT` (or `POST /api/calibrate` with `known_weight_g`) until the display matches.  
4. Restart the service after changing `.env`.

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
| *(header)* | Live weight, tare, unit, **Quick log** |
| **Recipe** | Scrape / paste / saved recipes → guided weighing |
| **Food log** | Search log, add foods, barcode / label, manage DB |
| **Body wt** | Log body weight (lbs) |
| **Days** | Weekly review, targets, daily sheet, weight plan |

### Recipe tips

- Count items like **eggs** get gram estimates (e.g. large egg ≈ 50 g).  
- Seasonings (salt, pepper, spices, …) are marked **skippable**; toggle per ingredient before cooking.  
- During cook: confirm on the phone or the physical tare/next button; use **Skip** for seasonings.

### Weight plan tips

- Needs at least two weigh-ins and some food logs.  
- Empty days are ignored (not counted as 0 kcal).  
- Mark incomplete tracking days **Do not count** in the day detail panel.

## API (selected)

| Method | Path | Notes |
|--------|------|--------|
| `GET` | `/api/state` | Live weight + cook session |
| `POST` | `/api/tare` | Zero the scale |
| `POST` | `/api/log_meal` | Log meal; or `{ "food_id", "weight_g?" }` for quick log |
| `GET` | `/api/recent_foods` | Chips for quick log |
| `GET` | `/api/weekly_review` | Last 7 days summary |
| `GET`/`PUT` | `/api/weight_plan` | Trend + goal calories |
| `POST` | `/api/weight_plan/exclude_day` | Exclude a day from calorie average |
| `POST` | `/api/recipe/parse` | `{ "url" }` or `{ "text" }` |
| `POST` | `/api/cook/start` / `/api/cook/confirm` | Guided cook (`skip: true` allowed) |
| `GET`/`PUT` | `/api/targets` | Daily macro targets |

## Supabase (optional)

1. Create a project and tables matching your app (`ingredients`, `daily_logs`, `weight_logs`, `recipes`, `recipe_items`, `user_targets`, `guided_sessions`).  
2. Put URL + anon key in `.env`.  
3. Apply RLS helpers in `supabase_schema.sql` if the anon key should read/write from the Pi.  

Without Supabase, everything still works from `data/logs.json`.

## Project layout

```
app.py              Flask API + hardware wiring
hx711.py            Load cell driver (median reads)
display_manager.py  OLED + GPIO buttons
local_store.py      Local JSON persistence
cloud.py            Supabase client
recipe_scrape.py    URL / line parsing (eggs, skippable seasonings)
recipe_math.py      Cook-session macros
weight_plan.py      Maintenance + goal + weekly review
food_lookup.py      Barcode + label helpers
common_foods.py     Seed catalog
templates/index.html  Phone UI
deploy/             systemd unit + install script
data/logs.json      On-device database
```

## Ops notes

- After code changes: `sudo systemctl restart scale`  
- Don’t run `python app.py` while the service is active (`GPIO` busy).  
- OLED layout: yellow top band (~16 rows) for guided chrome; blue band for the large weight digits.
