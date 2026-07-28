"""Barcode + nutrition-label helpers for creating foods."""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request


OFF_URL = "https://world.openfoodfacts.org/api/v2/product/{barcode}.json"
USER_AGENT = "ScaleKitchenHub/1.0 (local; food-logger)"


def _http_get_json(url: str, timeout: int = 12):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_post_json(url: str, payload: dict, headers: dict, timeout: int = 60):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _num(value, default=0.0):
    try:
        if value is None or value == "":
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def lookup_open_food_facts(barcode: str) -> dict:
    """Fetch product macros from Open Food Facts by barcode."""
    code = re.sub(r"\D", "", barcode or "")
    if len(code) < 8:
        raise ValueError("Barcode looks too short")

    try:
        data = _http_get_json(OFF_URL.format(barcode=code))
    except urllib.error.HTTPError as e:
        raise ValueError(f"Open Food Facts HTTP {e.code}") from e
    except Exception as e:
        raise ValueError(f"Open Food Facts unavailable: {e}") from e

    if data.get("status") != 1 or not data.get("product"):
        raise ValueError("Product not found in Open Food Facts")

    product = data["product"]
    nutriments = product.get("nutriments") or {}
    name = (
        product.get("product_name_en")
        or product.get("product_name")
        or product.get("generic_name")
        or f"Barcode {code}"
    ).strip()

    calories = nutriments.get("energy-kcal_100g")
    if calories is None and nutriments.get("energy_100g") is not None:
        # energy_100g is usually kJ
        calories = _num(nutriments.get("energy_100g")) / 4.184

    food = {
        "name": name[:120],
        "barcode": code,
        "basis": "per_100g",
        "calories": round(_num(calories), 1),
        "protein": round(_num(nutriments.get("proteins_100g")), 1),
        "carbs": round(_num(nutriments.get("carbohydrates_100g")), 1),
        "fat": round(_num(nutriments.get("fat_100g")), 1),
        "serving_size_g": 100.0,
        "source": "openfoodfacts",
        "image_url": product.get("image_front_small_url") or product.get("image_url"),
    }
    return food


def vision_available() -> bool:
    return bool((os.environ.get("OPENAI_API_KEY") or "").strip())


def parse_nutrition_label_image(image_bytes: bytes, mime: str = "image/jpeg") -> dict:
    """
    Use OpenAI vision to extract name + macros from a nutrition label photo.
    Requires OPENAI_API_KEY in the environment.
    """
    api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise ValueError(
            "Nutrition label scanning needs OPENAI_API_KEY in .env "
            "(or enter macros manually / use barcode scan)"
        )

    if not image_bytes:
        raise ValueError("Empty image")
    if len(image_bytes) > 8_000_000:
        raise ValueError("Image too large (max ~8MB)")

    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:{mime};base64,{b64}"

    prompt = (
        "You are reading a nutrition facts label photo for a kitchen scale food database. "
        "Extract the product name if visible, and nutrition values. "
        "Prefer per 100g values when shown. If only per serving is shown, include serving size in grams. "
        "Return ONLY compact JSON with keys: "
        "name (string), basis ('per_100g' or 'per_serving'), "
        "serving_size_g (number, required if basis is per_serving), "
        "calories (number), protein (number), carbs (number), fat (number). "
        "If a field is unknown use 0. No markdown."
    )

    model = (os.environ.get("OPENAI_VISION_MODEL") or "gpt-4o-mini").strip()
    payload = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    }

    try:
        result = _http_post_json(
            "https://api.openai.com/v1/chat/completions",
            payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=90,
        )
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")[:300]
        raise ValueError(f"Vision API error {e.code}: {body}") from e
    except Exception as e:
        raise ValueError(f"Vision API unavailable: {e}") from e

    try:
        content = result["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except Exception as e:
        raise ValueError("Could not parse nutrition label response") from e

    name = (parsed.get("name") or "Scanned food").strip()[:120]
    basis = (parsed.get("basis") or "per_100g").strip().lower()
    if basis not in ("per_100g", "per_serving"):
        basis = "per_100g"

    food = {
        "name": name,
        "basis": basis,
        "calories": _num(parsed.get("calories")),
        "protein": _num(parsed.get("protein")),
        "carbs": _num(parsed.get("carbs")),
        "fat": _num(parsed.get("fat")),
        "serving_size_g": _num(parsed.get("serving_size_g"), 100 if basis == "per_100g" else 0),
        "source": "nutrition_label",
    }
    if basis == "per_serving" and food["serving_size_g"] <= 0:
        raise ValueError("Label parse missing serving size in grams — enter manually")
    return food
