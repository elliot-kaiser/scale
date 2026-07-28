"""Scrape recipe URLs and convert ingredient lines into weighable targets."""

from __future__ import annotations

import re
import urllib.request
from typing import Optional
from urllib.parse import urlparse

from recipe_math import _to_grams

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
    (["water", "broth", "stock"], 240),
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
    (["bread crumb", "breadcrumb", "breadcrumbs", "panko"], 108),
    (["nut", "almond", "walnut", "cashew", "pecan"], 140),
    (["chocolate chip", "chocolate"], 170),
    (["salt"], 273),
    (["pepper"], 100),
]

# Countable items with no unit (e.g. "1 large egg", "2 garlic cloves")
_COUNT_GRAMS = [
    (["extra large egg", "extra-large egg", "xl egg"], 56.0),
    (["jumbo egg"], 63.0),
    (["large egg"], 50.0),
    (["medium egg"], 44.0),
    (["small egg"], 38.0),
    (["egg white", "egg whites"], 33.0),
    (["egg yolk", "egg yolks"], 17.0),
    (["egg", "eggs"], 50.0),
    (["garlic clove", "clove garlic", "cloves garlic", "clove of garlic"], 3.0),
    (["shallot"], 25.0),
    (["scallion", "green onion", "spring onion"], 15.0),
    (["bay leaf", "bay leaves"], 0.2),
]

# Seasonings / tiny amounts — skippable during guided weighing
_SKIPPABLE_KEYWORDS = [
    "salt", "pepper", "paprika", "cumin", "oregano", "thyme", "basil",
    "rosemary", "parsley", "cilantro", "dill", "sage", "marjoram", "tarragon",
    "garlic powder", "onion powder", "chili powder", "cayenne", "chipotle",
    "red pepper flake", "crushed red pepper", "seasoning", "spice", "spices",
    "extract", "vanilla", "almond extract", "baking soda", "baking powder",
    "dry mustard", "mustard powder", "worcestershire", "hot sauce",
    "soy sauce", "fish sauce", "sesame oil", "vinegar", "lemon juice",
    "lime juice", "pinch", "dash", "to taste", "for garnish", "garnish",
    "optional", "cooking spray", "nonstick", "for serving", "as needed",
    "black pepper", "white pepper", "kosher salt", "sea salt", "flaky salt",
    "msg", "bouillon", "stock cube", "bay leaf", "bay leaves",
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
    return 240.0


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
        return amount * (cup_g / 240.0)
    if u in ("l", "liter", "liters"):
        return amount * 1000.0 * (cup_g / 240.0)
    return _to_grams(amount, unit)


def _count_grams_for(name: str) -> Optional[float]:
    lower = (name or "").lower()
    for keys, grams in _COUNT_GRAMS:
        if any(k in lower for k in keys):
            return float(grams)
    return None


def is_skippable_ingredient(name: str, raw: str = "", unit: Optional[str] = None, amount: float = 0) -> bool:
    """Seasonings and tiny garnish amounts that don't need the scale."""
    text = f"{name or ''} {raw or ''}".lower()
    if any(k in text for k in _SKIPPABLE_KEYWORDS):
        return True
    if re.search(r"\b(to taste|as needed|optional|for garnish|for serving)\b", text):
        return True
    return False


def _clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name or "").strip(" ,;")
    name = re.sub(r"\s*\([^)]{40,}\)\s*", " ", name).strip()
    return name[:80] or "Ingredient"


def parse_ingredient_string(line: str) -> dict:
    """Convert a free-form recipe ingredient into {name, target_g, raw, skippable}."""
    raw = re.sub(r"^[\-\*\u2022]+\s*", "", (line or "").strip())
    raw = re.sub(r"^\d+[\.\)]\s+", "", raw).strip()
    if not raw:
        return {"name": "Ingredient", "target_g": 0.0, "raw": line, "skippable": False}

    m = _ING_LINE.match(raw)
    if m:
        amount = _parse_qty(m.group("qty"))
        unit = m.group("unit")
        name = _clean_name(m.group("name"))
        grams = 0.0
        if unit:
            grams = _volume_to_grams(amount, unit, name)
        else:
            per = _count_grams_for(name)
            if per is not None:
                grams = amount * per
            elif amount >= 10:
                grams = amount
            else:
                grams = 0.0

        skippable = is_skippable_ingredient(name, raw, unit, amount)
        return {
            "name": name,
            "target_g": round(max(0.0, grams), 1),
            "raw": raw,
            "parsed_amount": amount,
            "parsed_unit": unit,
            "skippable": skippable,
        }

    name = _clean_name(raw)
    return {
        "name": name,
        "target_g": 0.0,
        "raw": raw,
        "skippable": is_skippable_ingredient(name, raw),
    }


def parse_recipe_lines(text: str, default_title: str = "Custom recipe") -> dict:
    """Parse pasted multi-line recipe text using the same rules as URL scrape."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    title = default_title
    if not lines:
        return {"title": title, "ingredients": []}

    first = lines[0]
    if not re.search(r"\d", first) and len(lines) > 1:
        title = first.lstrip("#").strip() or title
        lines = lines[1:]

    ingredients = [parse_ingredient_string(line) for line in lines]
    return {"title": title, "ingredients": ingredients}


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
