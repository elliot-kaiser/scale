"""Scrape recipe URLs and convert ingredient lines into weighable targets."""

from __future__ import annotations

import re
import urllib.request
from typing import Optional
from urllib.parse import urlparse

from recipe_math import parse_recipe_text, _to_grams

# Approximate grams per US cup by ingredient keyword (best-effort for targets).
_CUP_DENSITIES = [
    (["flour", "all-purpose", "ap flour"], 120),
    (["bread flour"], 127),
    (["cake flour"], 114),
    (["sugar", "granulated", "white sugar"], 200),
    (["brown sugar"], 220),
    (["powdered sugar", "icing sugar", "confectioners"], 120),
    (["butter", "margarine"], 227),
    (["oil", "olive oil", "vegetable oil", "canola"], 218),
    (["milk", "almond milk", "oat milk", "soy milk"], 240),
    (["water", "broth", "stock", "stock"], 240),
    (["cream", "heavy cream", "whipping cream"], 238),
    (["yogurt", "yoghurt", "greek yogurt"], 245),
    (["sour cream"], 230),
    (["rice", "uncooked rice", "white rice", "brown rice"], 185),
    (["oats", "oatmeal", "rolled oats"], 90),
    (["quinoa"], 170),
    (["honey"], 340),
    (["maple syrup", "syrup"], 312),
    (["peanut butter", "almond butter", "nut butter"], 258),
    (["cocoa", "cocoa powder"], 86),
    (["protein powder", "whey"], 100),
    (["cheese", "shredded cheese", "cheddar"], 113),
    (["spinach", "kale", "lettuce"], 30),
    (["berries", "blueberry", "raspberry", "strawberry"], 150),
    (["tomato", "diced tomato"], 180),
    (["onion", "chopped onion"], 160),
    (["potato"], 150),
    (["bean", "black bean", "chickpea", "lentil"], 180),
    (["pasta", "uncooked pasta", "spaghetti", "penne"], 100),
    (["breadcrumb", "panko"], 108),
    (["nut", "almond", "walnut", "cashew", "pecan"], 140),
    (["chocolate chip", "chocolate"], 170),
]

_FRACTIONS = {
    "½": 0.5, "⅓": 1 / 3, "⅔": 2 / 3, "¼": 0.25, "¾": 0.75,
    "⅛": 0.125, "⅜": 0.375, "⅝": 0.625, "⅞": 0.875,
}

_ING_LINE = re.compile(
    r"^\s*"
    r"(?P<qty>\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?|[½⅓⅔¼¾⅛⅜⅝⅞])"
    r"(?:\s*(?P<unit>cups?|c\.|tablespoons?|tbsp\.?|tbs\.?|teaspoons?|tsp\.?|"
    r"kilograms?|kg|milliliters?|ml|liters?|grams?|ounces?|oz\.?|pounds?|lbs?|lb\.?|g|l)\b)?"
    r"\s*(?:of\s+)?"
    r"(?P<name>.+?)"
    r"\s*$",
    re.I,
)


def _parse_qty(raw: str) -> float:
    raw = (raw or "").strip()
    if raw in _FRACTIONS:
        return _FRACTIONS[raw]
    if re.fullmatch(r"\d+\s+\d+/\d+", raw):
        whole, frac = raw.split()
        num, den = frac.split("/")
        return float(whole) + float(num) / float(den)
    if "/" in raw and re.fullmatch(r"\d+/\d+", raw):
        num, den = raw.split("/")
        return float(num) / float(den)
    return float(raw)


def _cup_grams_for(name: str) -> float:
    lower = (name or "").lower()
    for keys, grams in _CUP_DENSITIES:
        if any(k in lower for k in keys):
            return float(grams)
    return 240.0  # water-like default


def _volume_to_grams(amount: float, unit: str, name: str) -> float:
    u = re.sub(r"[^a-z]", "", (unit or "").lower())
    cup_g = _cup_grams_for(name)
    if u in ("cup", "cups", "c"):
        return amount * cup_g
    if u in ("tablespoon", "tablespoons", "tbsp", "tbs"):
        return amount * (cup_g / 16.0)
    if u in ("teaspoon", "teaspoons", "tsp"):
        return amount * (cup_g / 48.0)
    if u in ("ml", "milliliter", "milliliters"):
        # Approximate ml ~= g for water-like; scale vs cup density
        return amount * (cup_g / 240.0)
    if u in ("l", "liter", "liters"):
        return amount * 1000.0 * (cup_g / 240.0)
    return _to_grams(amount, unit)


def _clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name or "").strip(" ,;")
    # Drop trailing prep notes in parentheses if very long
    name = re.sub(r"\s*\([^)]{40,}\)\s*", " ", name).strip()
    # Remove leading phrases like "fresh", keep substance
    return name[:80] or "Ingredient"


def parse_ingredient_string(line: str) -> dict:
    """Convert a free-form recipe ingredient string into {name, target_g, raw}."""
    raw = re.sub(r"^[\-\*\u2022]+\s*", "", (line or "").strip())
    raw = re.sub(r"^\d+[\.\)]\s+", "", raw).strip()
    if not raw:
        return {"name": "Ingredient", "target_g": 0.0, "raw": line}

    # Prefer structured parse
    m = _ING_LINE.match(raw)
    if m:
        amount = _parse_qty(m.group("qty"))
        unit = m.group("unit")
        name = _clean_name(m.group("name"))
        if unit:
            grams = _volume_to_grams(amount, unit, name)
        else:
            # Bare number — assume grams if large, else count (leave 0 target)
            grams = amount if amount >= 10 else 0.0
            if amount < 10 and not unit:
                # e.g. "2 eggs" — try to weigh later freely
                grams = 0.0
        return {
            "name": name,
            "target_g": round(max(0.0, grams), 1),
            "raw": raw,
            "parsed_amount": amount,
            "parsed_unit": unit,
        }

    # Fall back to existing text parser (expects one line with grams)
    parsed = parse_recipe_text(raw)
    if parsed["ingredients"]:
        item = parsed["ingredients"][0]
        return {
            "name": item["name"],
            "target_g": float(item.get("target_g") or 0),
            "raw": raw,
        }
    return {"name": _clean_name(raw), "target_g": 0.0, "raw": raw}


def _fetch_html(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Mobile Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read()


def scrape_recipe_url(url: str) -> dict:
    """Fetch and parse a recipe page. Raises ValueError on failure."""
    url = (url or "").strip()
    parsed_url = urlparse(url)
    if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
        raise ValueError("Enter a valid http(s) recipe URL")

    try:
        from recipe_scrapers import scrape_me, scrape_html
        from recipe_scrapers._exceptions import WebsiteNotImplementedError
    except ImportError as e:
        raise ValueError("recipe-scrapers is not installed") from e

    scraper = None
    last_err = None

    # Prefer fetching with a browser UA, then parse HTML (avoids many 403s)
    try:
        html = _fetch_html(url)
        scraper = scrape_html(html, org_url=url)
    except WebsiteNotImplementedError as e:
        last_err = e
        scraper = None
    except Exception as e:
        last_err = e
        scraper = None

    if scraper is None:
        try:
            scraper = scrape_me(url)
        except Exception as e:
            last_err = e
            scraper = None

    if scraper is None:
        raise ValueError(
            "Could not scrape this URL — paste the ingredient list instead"
            + (f" ({last_err})" if last_err else "")
        )

    try:
        title = (scraper.title() or "Scraped recipe").strip()
    except Exception:
        title = "Scraped recipe"

    try:
        lines = scraper.ingredients() or []
    except Exception as e:
        raise ValueError(f"No ingredients found: {e}") from e

    ingredients = [parse_ingredient_string(line) for line in lines if str(line).strip()]
    if not ingredients:
        raise ValueError("No ingredients found on that page")

    return {
        "title": title[:120],
        "ingredients": ingredients,
        "source_url": url,
        "yields": _safe(scraper, "yields"),
        "total_time": _safe(scraper, "total_time"),
        "host": parsed_url.netloc,
    }


def _safe(scraper, method: str):
    try:
        fn = getattr(scraper, method, None)
        return fn() if callable(fn) else None
    except Exception:
        return None


def looks_like_url(text: str) -> bool:
    text = (text or "").strip()
    if not text or "\n" in text:
        return False
    return bool(re.match(r"^https?://\S+$", text, re.I)) or (
        "." in text and " " not in text and "/" in text
    )
