"""Recipe paste parsing and cook-session math."""

from __future__ import annotations

import re
from typing import Optional


_LINE_PATTERNS = [
    # 150g Chicken Breast / 150 g Chicken Breast
    re.compile(
        r"^\s*(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>g|grams?|kg|oz|lbs?|lb)?\s+of\s+(?P<name>.+?)\s*$",
        re.I,
    ),
    re.compile(
        r"^\s*(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>g|grams?|kg|oz|lbs?|lb)\s+(?P<name>.+?)\s*$",
        re.I,
    ),
    # Chicken Breast - 150g / Chicken Breast: 150 g
    re.compile(
        r"^\s*(?P<name>.+?)\s*[-:–]\s*(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>g|grams?|kg|oz|lbs?|lb)?\s*$",
        re.I,
    ),
    # Chicken Breast 150g
    re.compile(
        r"^\s*(?P<name>.+?)\s+(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>g|grams?|kg|oz|lbs?|lb)\s*$",
        re.I,
    ),
]


def _to_grams(amount: float, unit: Optional[str]) -> float:
    u = re.sub(r"[^a-z]", "", (unit or "g").lower())
    if u in ("g", "gram", "grams"):
        return amount
    if u in ("kg", "kilogram", "kilograms"):
        return amount * 1000.0
    if u in ("oz", "ounce", "ounces"):
        return amount * 28.3495
    if u in ("lb", "lbs", "pound", "pounds"):
        return amount * 453.592
    return amount


def parse_recipe_text(text: str, default_title: str = "Custom recipe") -> dict:
    """Parse pasted recipe text into title + ingredient lines with target grams."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    title = default_title
    ingredients = []

    if not lines:
        return {"title": title, "ingredients": []}

    # First line without a number may be the title
    first = lines[0]
    if not re.search(r"\d", first) and len(lines) > 1:
        title = first.lstrip("#").strip() or title
        lines = lines[1:]

    for raw in lines:
        # Strip list markers like "1.", "2)", "-", "*" — but keep amounts like "150g"
        line = re.sub(r"^[\-\*\u2022]+\s*", "", raw).strip()
        line = re.sub(r"^\d+[\.\)]\s+", "", line).strip()
        if not line:
            continue
        matched = None
        for pat in _LINE_PATTERNS:
            m = pat.match(line)
            if m:
                matched = m
                break
        if not matched:
            ingredients.append({"name": line, "target_g": 0.0})
            continue
        name = matched.group("name").strip(" -–:\t")
        amount = float(matched.group("amount"))
        unit = matched.groupdict().get("unit")
        ingredients.append({
            "name": name,
            "target_g": round(_to_grams(amount, unit), 1),
        })

    return {"title": title, "ingredients": ingredients}


def macros_for_weight(food: dict, weight_g: float) -> dict:
    factor = float(weight_g or 0) / 100.0
    return {
        "calories": round(float(food.get("calories_per_100g") or 0) * factor, 1),
        "protein": round(float(food.get("protein_per_100g") or 0) * factor, 1),
        "carbs": round(float(food.get("carbs_per_100g") or 0) * factor, 1),
        "fat": round(float(food.get("fat_per_100g") or 0) * factor, 1),
    }


def batch_macros(ingredients: list) -> dict:
    totals = {"calories": 0.0, "protein": 0.0, "carbs": 0.0, "fat": 0.0, "weight_g": 0.0}
    for item in ingredients:
        w = float(item.get("actual_g") or 0)
        totals["weight_g"] += w
        m = macros_for_weight(item, w)
        for k in ("calories", "protein", "carbs", "fat"):
            totals[k] += m[k]
    for k in totals:
        totals[k] = round(totals[k], 1)
    return totals


def portion_macros(batch: dict, cooked_g: float, portion_g: float) -> dict:
    cooked = float(cooked_g or 0)
    portion = float(portion_g or 0)
    if cooked <= 0 or portion <= 0:
        return {"calories": 0.0, "protein": 0.0, "carbs": 0.0, "fat": 0.0, "weight_g": 0.0}
    ratio = portion / cooked
    return {
        "calories": round(batch["calories"] * ratio, 1),
        "protein": round(batch["protein"] * ratio, 1),
        "carbs": round(batch["carbs"] * ratio, 1),
        "fat": round(batch["fat"] * ratio, 1),
        "weight_g": round(portion, 1),
    }


def find_best_food(name: str, foods: list) -> Optional[dict]:
    """Exact or substring match against food catalog."""
    needle = (name or "").strip().lower()
    if not needle:
        return None
    exact = next((f for f in foods if (f.get("name") or "").lower() == needle), None)
    if exact:
        return exact
    contains = [
        f for f in foods
        if needle in (f.get("name") or "").lower() or (f.get("name") or "").lower() in needle
    ]
    if not contains:
        return None
    contains.sort(key=lambda f: abs(len(f.get("name") or "") - len(needle)))
    return contains[0]
