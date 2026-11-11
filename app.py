#!/usr/bin/env python3
"""recipeDB: a tiny recipe database, Python standard library only.

Storage is SQLite. The database file lives outside the repo (in the
container, a named volume mounted at /data), so rebuilding the image
never wipes recipes. On every start, the app inserts any recipe from
data/seed.json whose slug is not already in the database — so a new
seed recipe reaches an existing database too. It never overwrites
existing recipes.

In the container, the seed file is at /app/data/seed.json (copied in
by the Dockerfile). /data holds only the SQLite database, recipes.db.

Routes:
  GET /                     recipe list (HTML), ?q= searches title,
                            description and ingredients
  GET /add                  add-recipe form (HTML)
  POST /add                 save a new recipe, redirect to its page
  GET /recipes/<slug>       one recipe (HTML) — a board of draggable,
                            resizable section cards (Recipe, Ingredients,
                            Steps, Details); layout is saved per recipe
                            in the browser, with a Reset layout button.
                            Tap a measurement to convert it (cup/tbsp/
                            tsp/ml/fl oz/L, lb/oz/g/kg); hover shows all
                            equivalents. "Show metric" converts them all.
  GET /api/recipes          all recipes (JSON), ?q= filters the same way
  GET /api/recipes/<slug>   one recipe (JSON)
  GET /health               {"status": "ok"}

Recipes added through the form go into the SQLite database (the
volume), not into data/seed.json.

Environment:
  RECIPEDB_PATH  SQLite path, default /data/recipes.db
  RECIPEDB_SEED  seed JSON path, default data/seed.json next to app.py
  PORT           listen port, default 8080
"""

import html
import json
import os
import re
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

BASE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("RECIPEDB_PATH", "/data/recipes.db"))
SEED_PATH = Path(os.environ.get("RECIPEDB_SEED", BASE / "data" / "seed.json"))
PORT = int(os.environ.get("PORT", "8080"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS recipes (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    source TEXT DEFAULT '',
    submitted_by TEXT DEFAULT '',
    prep_time TEXT DEFAULT '',
    cook_time TEXT DEFAULT '',
    total_time TEXT DEFAULT '',
    serves INTEGER,
    oven_temperature TEXT DEFAULT '',
    pan TEXT DEFAULT '',
    done_temperature TEXT DEFAULT '',
    storage TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ingredients (
    id INTEGER PRIMARY KEY,
    recipe_id INTEGER NOT NULL REFERENCES recipes(id),
    position INTEGER NOT NULL,
    part TEXT DEFAULT '',
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY,
    recipe_id INTEGER NOT NULL REFERENCES recipes(id),
    position INTEGER NOT NULL,
    text TEXT NOT NULL
);
"""


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def seed(conn):
    """Insert seed recipes whose slug is not already in the database.

    Runs on every start, so a new seed recipe reaches an existing
    database. Existing recipes (including form-added ones) are never
    touched or duplicated.
    """
    if not SEED_PATH.exists():
        return 0
    recipes = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    if isinstance(recipes, dict):
        recipes = recipes.get("recipes", [])
    inserted = 0
    for recipe in recipes:
        exists = conn.execute(
            "SELECT 1 FROM recipes WHERE slug = ?", (recipe["slug"],)
        ).fetchone()
        if exists:
            continue
        inserted += 1
        cur = conn.execute(
            """INSERT INTO recipes
               (slug, title, description, source, submitted_by, prep_time,
                cook_time, total_time, serves, oven_temperature, pan,
                done_temperature, storage)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                recipe["slug"],
                recipe["title"],
                recipe.get("description", ""),
                recipe.get("source", ""),
                recipe.get("submitted_by", ""),
                recipe.get("prep_time", ""),
                recipe.get("cook_time", ""),
                recipe.get("total_time", ""),
                recipe.get("serves"),
                recipe.get("oven_temperature", ""),
                recipe.get("pan", ""),
                recipe.get("done_temperature", ""),
                recipe.get("storage", ""),
            ),
        )
        recipe_id = cur.lastrowid
        for i, ingredient in enumerate(recipe.get("ingredients", [])):
            if isinstance(ingredient, str):
                part, text = "", ingredient
            else:
                part, text = ingredient.get("part", ""), ingredient["text"]
            conn.execute(
                "INSERT INTO ingredients (recipe_id, position, part, text)"
                " VALUES (?, ?, ?, ?)",
                (recipe_id, i, part, text),
            )
        for i, step in enumerate(recipe.get("steps", [])):
            conn.execute(
                "INSERT INTO steps (recipe_id, position, text) VALUES (?, ?, ?)",
                (recipe_id, i, step),
            )
    conn.commit()
    return inserted


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    seeded = seed(conn)
    conn.close()
    return seeded


def get_recipe(conn, slug):
    row = conn.execute(
        "SELECT * FROM recipes WHERE slug = ?", (slug,)
    ).fetchone()
    if row is None:
        return None
    recipe = dict(row)
    recipe["ingredients"] = [
        dict(r)
        for r in conn.execute(
            "SELECT part, text FROM ingredients WHERE recipe_id = ?"
            " ORDER BY position",
            (row["id"],),
        )
    ]
    recipe["steps"] = [
        r["text"]
        for r in conn.execute(
            "SELECT text FROM steps WHERE recipe_id = ? ORDER BY position",
            (row["id"],),
        )
    ]
    return recipe


def list_recipes(conn, q=None):
    if q:
        like = f"%{q}%"
        rows = conn.execute(
            """SELECT slug, title, total_time, serves FROM recipes
               WHERE title LIKE ? OR description LIKE ?
               OR EXISTS (SELECT 1 FROM ingredients
                          WHERE ingredients.recipe_id = recipes.id
                          AND ingredients.text LIKE ?)
               ORDER BY title""",
            (like, like, like),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT slug, title, total_time, serves FROM recipes ORDER BY title"
        ).fetchall()
    return [dict(r) for r in rows]


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "recipe"


def insert_recipe(conn, recipe, ingredients, steps):
    cur = conn.execute(
        """INSERT INTO recipes
           (slug, title, description, source, submitted_by, prep_time,
            cook_time, total_time, serves, oven_temperature, pan,
            done_temperature, storage)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            recipe["slug"],
            recipe["title"],
            recipe.get("description", ""),
            recipe.get("source", ""),
            recipe.get("submitted_by", ""),
            recipe.get("prep_time", ""),
            recipe.get("cook_time", ""),
            recipe.get("total_time", ""),
            recipe.get("serves"),
            recipe.get("oven_temperature", ""),
            recipe.get("pan", ""),
            recipe.get("done_temperature", ""),
            recipe.get("storage", ""),
        ),
    )
    recipe_id = cur.lastrowid
    for i, text in enumerate(ingredients):
        conn.execute(
            "INSERT INTO ingredients (recipe_id, position, part, text)"
            " VALUES (?, ?, ?, ?)",
            (recipe_id, i, "", text),
        )
    for i, text in enumerate(steps):
        conn.execute(
            "INSERT INTO steps (recipe_id, position, text) VALUES (?, ?, ?)",
            (recipe_id, i, text),
        )
    conn.commit()
    return recipe_id


# --- Measurement conversion (issue #2) ---
# Parses the leading quantity + unit of an ingredient line so the UI
# can convert it (tap to cycle units, hover for all equivalents).

UNICODE_FRACTIONS = {
    "½": (1, 2), "⅓": (1, 3), "⅔": (2, 3), "¼": (1, 4), "¾": (3, 4),
    "⅛": (1, 8), "⅜": (3, 8), "⅝": (5, 8), "⅞": (7, 8),
    "⅕": (1, 5), "⅖": (2, 5), "⅗": (3, 5), "⅘": (4, 5),
    "⅙": (1, 6), "⅚": (5, 6), "⅐": (1, 7), "⅑": (1, 9), "⅒": (1, 10),
}
FRACTION_CHARS = {
    (1, 2): "½", (1, 3): "⅓", (2, 3): "⅔", (1, 4): "¼", (3, 4): "¾",
    (1, 8): "⅛", (3, 8): "⅜", (5, 8): "⅝", (7, 8): "⅞",
}
# alias -> (family, canonical unit, factor to base unit: ml for volume, g for weight)
UNITS = {}
for _alias, _fam, _canon, _factor in [
    ("tsp", "volume", "tsp", 4.92892159375),
    ("teaspoon", "volume", "tsp", 4.92892159375),
    ("teaspoons", "volume", "tsp", 4.92892159375),
    ("tbsp", "volume", "tbsp", 14.78676478125),
    ("tablespoon", "volume", "tbsp", 14.78676478125),
    ("tablespoons", "volume", "tbsp", 14.78676478125),
    ("cup", "volume", "cup", 236.5882365),
    ("cups", "volume", "cup", 236.5882365),
    ("fl oz", "volume", "fl oz", 29.5735295625),
    ("fluid ounce", "volume", "fl oz", 29.5735295625),
    ("fluid ounces", "volume", "fl oz", 29.5735295625),
    ("pint", "volume", "pint", 473.176473),
    ("pints", "volume", "pint", 473.176473),
    ("quart", "volume", "quart", 946.352946),
    ("quarts", "volume", "quart", 946.352946),
    ("l", "volume", "L", 1000.0),
    ("L", "volume", "L", 1000.0),
    ("liter", "volume", "L", 1000.0),
    ("liters", "volume", "L", 1000.0),
    ("litre", "volume", "L", 1000.0),
    ("litres", "volume", "L", 1000.0),
    ("ml", "volume", "ml", 1.0),
    ("milliliter", "volume", "ml", 1.0),
    ("milliliters", "volume", "ml", 1.0),
    ("millilitre", "volume", "ml", 1.0),
    ("millilitres", "volume", "ml", 1.0),
    ("gal", "volume", "gal", 3785.411784),
    ("gallon", "volume", "gal", 3785.411784),
    ("gallons", "volume", "gal", 3785.411784),
    ("g", "weight", "g", 1.0),
    ("gram", "weight", "g", 1.0),
    ("grams", "weight", "g", 1.0),
    ("kg", "weight", "kg", 1000.0),
    ("kilogram", "weight", "kg", 1000.0),
    ("kilograms", "weight", "kg", 1000.0),
    ("oz", "weight", "oz", 28.349523125),
    ("ounce", "weight", "oz", 28.349523125),
    ("ounces", "weight", "oz", 28.349523125),
    ("lb", "weight", "lb", 453.59237),
    ("lbs", "weight", "lb", 453.59237),
    ("pound", "weight", "lb", 453.59237),
    ("pounds", "weight", "lb", 453.59237),
]:
    UNITS[_alias] = (_fam, _canon, _factor)

CYCLE_UNITS = {
    "volume": ["cup", "tbsp", "tsp", "ml", "fl oz", "L"],
    "weight": ["lb", "oz", "g", "kg"],
}


def unit_display(unit, amount):
    if unit == "cup":
        return "cup" if amount == 1 else "cups"
    if unit == "pint":
        return "pint" if amount == 1 else "pints"
    if unit == "quart":
        return "quart" if amount == 1 else "quarts"
    return unit


def fmt_amount(x, unit):
    """Format an amount: fractions for US units, rounded for metric."""
    if unit in ("ml", "g"):
        if x >= 100:
            return str(int(round(x)))
        r = round(x, 1)
        return str(int(r)) if r == int(r) else f"{r:.1f}"
    if unit in ("kg", "L"):
        return f"{x:.2f}".rstrip("0").rstrip(".")
    from fractions import Fraction

    frac = Fraction(x).limit_denominator(8)
    if abs(float(frac) - x) > 0.02:
        # No close fraction (e.g. 0.08 gal) — a decimal is more honest.
        return f"{x:.2f}".rstrip("0").rstrip(".")
    whole = frac.numerator // frac.denominator
    rem = frac - whole
    if rem == 0:
        return str(whole)
    fchar = FRACTION_CHARS.get((rem.numerator, rem.denominator))
    if fchar is None:
        return f"{x:.2f}".rstrip("0").rstrip(".")
    return f"{whole}{fchar}" if whole else fchar


def convert_amount(amount, from_unit, to_unit):
    _fam, _canon, f1 = UNITS[from_unit]
    _fam2, _canon2, f2 = UNITS[to_unit]
    return amount * f1 / f2


def _parse_number(s):
    """Parse a leading number (mixed, fraction, unicode, decimal).

    Returns (value, chars_consumed) or None.
    """
    m = re.match(r"^(\d+)\s+(\d+)/(\d+)", s)
    if m:
        return int(m.group(1)) + int(m.group(2)) / int(m.group(3)), m.end()
    m = re.match(r"^(\d+)/(\d+)", s)
    if m:
        return int(m.group(1)) / int(m.group(2)), m.end()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([½⅓⅔¼¾⅛⅜⅝⅞⅕⅖⅗⅘⅙⅚⅐⅑⅒])", s)
    if m:
        num, den = UNICODE_FRACTIONS[m.group(2)]
        return float(m.group(1)) + num / den, m.end()
    m = re.match(r"^([½⅓⅔¼¾⅛⅜⅝⅞⅕⅖⅗⅘⅙⅚⅐⅑⅒])", s)
    if m:
        num, den = UNICODE_FRACTIONS[m.group(1)]
        return num / den, m.end()
    m = re.match(r"^(\d+(?:\.\d+)?)", s)
    if m:
        return float(m.group(1)), m.end()
    return None


def _match_unit(rest):
    """Match a leading unit in rest. Returns (family, canon, rest) or None."""
    low = rest.lower()
    for alias in sorted(UNITS, key=len, reverse=True):
        if low == alias or low.startswith(alias + " "):
            fam, canon, _factor = UNITS[alias]
            return fam, canon, rest[len(alias):].strip()
    return None


def parse_measure(text):
    """Parse the leading measurement of an ingredient line.

    Returns {"amount"|"low"/"high", "unit", "family", "rest"} or None
    when the line starts with no convertible measurement.
    """
    s = text.strip()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*[–-]\s*(\d+(?:\.\d+)?)\s+(.*)$", s)
    if m:
        unit = _match_unit(m.group(3))
        if unit:
            fam, canon, rest = unit
            return {
                "low": float(m.group(1)),
                "high": float(m.group(2)),
                "unit": canon,
                "family": fam,
                "rest": rest,
            }
        return None
    num = _parse_number(s)
    if not num:
        return None
    value, idx = num
    unit = _match_unit(s[idx:].strip())
    if not unit:
        return None
    fam, canon, rest = unit
    return {"amount": value, "unit": canon, "family": fam, "rest": rest}


def conversions_tooltip(m):
    """'1⅓ cups = 21⅓ tbsp · 64 tsp · 315 ml · …' for the title tooltip.

    Only the units the click-cycle offers, and never a unit the amount
    rounds to zero in (no '0 quarts' for 2 tsp).
    """
    parts = []
    for unit in CYCLE_UNITS[m["family"]]:
        if unit == m["unit"]:
            continue
        if "amount" in m:
            v = convert_amount(m["amount"], m["unit"], unit)
            if v < 0.005:
                continue
            parts.append(f"{fmt_amount(v, unit)} {unit_display(unit, v)}")
        else:
            lo = convert_amount(m["low"], m["unit"], unit)
            hi = convert_amount(m["high"], m["unit"], unit)
            if hi < 0.005:
                continue
            parts.append(
                f"{fmt_amount(lo, unit)}–{fmt_amount(hi, unit)} "
                f"{unit_display(unit, hi)}"
            )
    return " = ".join(parts)


def ingredient_li(text):
    """Render one ingredient line, with a tap-to-convert button when
    it starts with a convertible measurement."""
    m = parse_measure(text)
    if not m:
        return f"<li>{html.escape(text)}</li>"
    if "amount" in m:
        data = f" data-base-amount='{m['amount']}'"
    else:
        data = f" data-base-low='{m['low']}' data-base-high='{m['high']}'"
    title = html.escape(conversions_tooltip(m), quote=True)
    return (
        f"<li><button type='button' class='conv' data-base-unit='{m['unit']}'"
        f" data-family='{m['family']}'{data}"
        f" data-rest='{html.escape(m['rest'], quote=True)}'"
        f" title='Click to convert · {title}'>{html.escape(text)}</button></li>"
    )


def page(title, body):
    # Dark is the default theme; the toggle stores a light/dark choice
    # in localStorage, applied in <head> so there is no light flash.
    return (
        "<!doctype html><html data-theme='dark'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        "<script>(function(){try{var t=localStorage.getItem('recipedb-theme');"
        "if(t==='light'||t==='dark'){document.documentElement"
        ".setAttribute('data-theme',t);}}catch(e){}})();</script>"
        "<style>"
        ":root,[data-theme='dark']{color-scheme:dark;"
        "--bg:#17191f;--surface:#1f232b;--fg:#e8e6e3;--muted:#a8a29a;"
        "--border:#333842;--link:#7fb3ff;--chip-bg:#2a2f3a;"
        "--input-bg:#22262e;--input-border:#444a56;"
        "--btn-bg:#e8e6e3;--btn-fg:#17191f;"
        "--error-bg:#3a1d1d;--error-border:#8a3a3a}"
        "[data-theme='light']{color-scheme:light;"
        "--bg:#ffffff;--surface:#f6f6f6;--fg:#222;--muted:#555;"
        "--border:#ddd;--link:#0b5cad;--chip-bg:#eef3f8;"
        "--input-bg:#ffffff;--input-border:#bbb;"
        "--btn-bg:#222;--btn-fg:#fff;"
        "--error-bg:#fdecea;--error-border:#e57373}"
        "body{font-family:system-ui,sans-serif;max-width:760px;margin:0 auto;"
        "padding:1rem;line-height:1.5;color:var(--fg);background:var(--bg)}"
        "header{display:flex;justify-content:space-between;align-items:baseline;"
        "flex-wrap:wrap;gap:.5rem;border-bottom:2px solid var(--border);"
        "padding-bottom:.5rem}"
        "header a{color:var(--fg);text-decoration:none;font-weight:600}"
        "nav{display:flex;gap:.5rem;align-items:center}"
        "a{color:var(--link)}"
        ".card{border:1px solid var(--border);border-radius:10px;"
        "padding:1rem;margin:1rem 0;background:var(--surface)}"
        ".card h2{margin:0 0 .25rem}"
        ".meta{color:var(--muted);font-size:.9rem}"
        ".chip{display:inline-block;background:var(--chip-bg);border-radius:20px;"
        "padding:.15rem .6rem;margin:.15rem .25rem .15rem 0;font-size:.85rem}"
        "form.search{display:flex;gap:.5rem;margin:1rem 0}"
        "form.search input[type=search]{flex:1;padding:.6rem;font-size:1rem}"
        "label{display:block;margin:.75rem 0 .25rem;font-weight:600}"
        "input[type=text],input[type=url],input[type=number],input[type=search],textarea{"
        "width:100%;box-sizing:border-box;padding:.6rem;font-size:1rem;"
        "border:1px solid var(--input-border);border-radius:6px;"
        "background:var(--input-bg);color:var(--fg)}"
        "textarea{min-height:6rem}"
        "button{padding:.6rem 1.2rem;font-size:1rem;border:0;border-radius:6px;"
        "background:var(--btn-bg);color:var(--btn-fg);cursor:pointer}"
        "button.theme-toggle{padding:.3rem .6rem;font-size:.9rem;"
        "background:transparent;color:var(--fg);"
        "border:1px solid var(--border)}"
        ".error{background:var(--error-bg);border:1px solid var(--error-border);"
        "border-radius:6px;padding:.75rem 1rem}"
        ".board{position:relative}"
        ".board-tools{display:flex;justify-content:flex-end;margin-bottom:.5rem}"
        ".node{position:relative;background:var(--surface);"
        "border:1px solid var(--border);border-radius:12px;margin:0 0 1rem;"
        "box-shadow:0 2px 10px rgba(0,0,0,.25)}"
        ".board.laid-out .node{position:absolute;margin:0;max-width:100%}"
        ".node-head{display:flex;align-items:center;gap:.5rem;"
        "padding:.5rem .75rem;border-bottom:1px solid var(--border);"
        "cursor:grab;user-select:none;touch-action:none;font-weight:600}"
        ".node-head:active{cursor:grabbing}"
        ".node-head .theme-toggle{margin-left:auto}"
        ".grip{opacity:.55}"
        ".node-body{padding:.75rem 1rem}"
        ".node-body h2,.node-body h3{margin:.5rem 0 .25rem}"
        ".node-body ul,.node-body ol{margin:.25rem 0;padding-left:1.25rem}"
        ".resize-handle{position:absolute;right:5px;bottom:5px;width:16px;"
        "height:16px;cursor:nwse-resize;touch-action:none;opacity:.55;"
        "background:linear-gradient(135deg,transparent 50%,var(--muted) 50%,"
        "var(--muted) 62%,transparent 62%,transparent 72%,var(--muted) 72%,"
        "var(--muted) 84%,transparent 84%)}"
        "button.conv{background:none;border:0;padding:0;font:inherit;"
        "color:inherit;text-align:left;cursor:pointer;"
        "text-decoration:underline dotted}"
        "button.conv:hover{color:var(--link)}"
        "</style>"
        f"</head><body><header><a href='/'>🍲 recipeDB</a>"
        "<nav><a href='/add'>+ Add recipe</a>"
        "<button class='theme-toggle' id='theme-toggle' type='button'>"
        "☀️ Light</button></nav></header>"
        f"<h1>{html.escape(title)}</h1>{body}"
        "<script>(function(){var btn=document.getElementById('theme-toggle');"
        "if(!btn)return;function label(){btn.textContent="
        "document.documentElement.getAttribute('data-theme')==='dark'"
        "?'☀️ Light':'🌙 Dark';}label();"
        "btn.addEventListener('click',function(){var next="
        "document.documentElement.getAttribute('data-theme')==='dark'"
        "?'light':'dark';document.documentElement.setAttribute('data-theme',next);"
        "try{localStorage.setItem('recipedb-theme',next);}catch(e){}label();});"
        "})();</script>"
        "<script>(function(){"
        "var CONV={volume:{tsp:4.92892159375,tbsp:14.78676478125,"
        "cup:236.5882365,'fl oz':29.5735295625,pint:473.176473,"
        "quart:946.352946,L:1000,ml:1,gal:3785.411784},"
        "weight:{g:1,kg:1000,oz:28.349523125,lb:453.59237}};"
        "var CYCLE={volume:['cup','tbsp','tsp','ml','fl oz','L'],"
        "weight:['lb','oz','g','kg']};"
        "var FRAC={'1/2':'½','1/3':'⅓','2/3':'⅔','1/4':'¼','3/4':'¾',"
        "'1/8':'⅛','3/8':'⅜','5/8':'⅝','7/8':'⅞'};"
        "function fmt(x,unit){"
        "if(unit==='ml'||unit==='g'){"
        "if(x>=100){return String(Math.round(x));}"
        "var r=Math.round(x*10)/10; return (r%1===0)?String(r):r.toFixed(1);}"
        "if(unit==='kg'||unit==='L'){"
        "return x.toFixed(2).replace(/0+$/,'').replace(/\\.$/,'');}"
        "var whole=Math.floor(x+1e-9), frac=x-whole, best=[0,1], bestErr=1;"
        "[2,3,4,8].forEach(function(d){var n=Math.round(frac*d);"
        "var e=Math.abs(frac-n/d); if(e<bestErr){bestErr=e; best=[n,d];}});"
        "if(bestErr<0.03&&best[0]>0){"
        "var ch=FRAC[best[0]+'/'+best[1]];"
        "if(ch){return whole?whole+ch:ch;}}"
        "if(bestErr<0.03&&best[0]===0){return String(whole);}"
        "return x.toFixed(2).replace(/0+$/,'').replace(/\\.$/,'');}"
        "function disp(unit,x){"
        "if(unit==='cup'){return x===1?'cup':'cups';}"
        "if(unit==='pint'){return x===1?'pint':'pints';}"
        "if(unit==='quart'){return x===1?'quart':'quarts';}"
        "return unit;}"
        "function convert(b,unit){"
        "var fam=b.getAttribute('data-family');"
        "var base=b.getAttribute('data-base-unit');"
        "var rest=b.getAttribute('data-rest');"
        "function amt(v){return v*CONV[fam][base]/CONV[fam][unit];}"
        "var txt;"
        "if(b.hasAttribute('data-base-amount')){"
        "var v=amt(parseFloat(b.getAttribute('data-base-amount')));"
        "txt=fmt(v,unit)+' '+disp(unit,v)+' '+rest;"
        "}else{"
        "var lo=amt(parseFloat(b.getAttribute('data-base-low')));"
        "var hi=amt(parseFloat(b.getAttribute('data-base-high')));"
        "txt=fmt(lo,unit)+'–'+fmt(hi,unit)+' '+disp(unit,hi)+' '+rest;}"
        "b.setAttribute('data-unit',unit); b.textContent=txt;}"
        "document.querySelectorAll('.conv').forEach(function(b){"
        "b.setAttribute('data-unit',b.getAttribute('data-base-unit'));"
        "b.addEventListener('click',function(){"
        "var cycle=CYCLE[b.getAttribute('data-family')];"
        "var idx=cycle.indexOf(b.getAttribute('data-unit'));"
        "convert(b,cycle[(idx+1)%cycle.length]);});});"
        "var metricBtn=document.getElementById('units-metric');"
        "if(metricBtn){metricBtn.addEventListener('click',function(){"
        "var toMetric=metricBtn.getAttribute('data-mode')!=='metric';"
        "document.querySelectorAll('.conv').forEach(function(b){"
        "var fam=b.getAttribute('data-family');"
        "convert(b,toMetric?(fam==='volume'?'ml':'g')"
        ":b.getAttribute('data-base-unit'));});"
        "metricBtn.setAttribute('data-mode',toMetric?'metric':'us');"
        "metricBtn.textContent=toMetric?'Show US units':'Show metric';});}"
        "var board=document.getElementById('board');"
        "if(!board){return;}"
        "var slug=board.getAttribute('data-slug');"
        "var key='recipedb-layout-'+slug;"
        "var nodes=Array.prototype.slice.call(board.querySelectorAll('.node'));"
        "var saved=null;"
        "try{saved=JSON.parse(localStorage.getItem(key)||'null');}catch(e){}"
        "function boardHeight(){var h=0;"
        "nodes.forEach(function(n){h=Math.max(h,n.offsetTop+n.offsetHeight);});"
        "board.style.height=h+'px';}"
        "function save(){var out={};"
        "nodes.forEach(function(n){var o={left:n.style.left,top:n.style.top,"
        "width:n.style.width};"
        "if(n.style.height){o.height=n.style.height;}"
        "out[n.getAttribute('data-card')]=o;});"
        "try{localStorage.setItem(key,JSON.stringify(out));}catch(e){}}"
        "if(saved){"
        "nodes.forEach(function(n){var o=saved[n.getAttribute('data-card')];"
        "if(!o){return;}"
        "n.style.left=o.left; n.style.top=o.top; n.style.width=o.width;"
        "if(o.height){n.style.height=o.height; n.style.overflow='auto';}});"
        "board.classList.add('laid-out');"
        "requestAnimationFrame(boardHeight);"
        "}else{"
        "var br=board.getBoundingClientRect();"
        "nodes.forEach(function(n){var r=n.getBoundingClientRect();"
        "n.style.left=(r.left-br.left)+'px';"
        "n.style.top=(r.top-br.top)+'px';"
        "n.style.width=r.width+'px';});"
        "board.classList.add('laid-out'); boardHeight();}"
        "nodes.forEach(function(n){"
        "var head=n.querySelector('.node-head');"
        "head.addEventListener('pointerdown',function(e){"
        "if(e.target.closest('button')){return;}"
        "e.preventDefault();"
        "var sx=e.clientX, sy=e.clientY, sl=n.offsetLeft, st=n.offsetTop;"
        "head.setPointerCapture(e.pointerId);"
        "function move(ev){"
        "n.style.left=(sl+ev.clientX-sx)+'px';"
        "n.style.top=Math.max(0,st+ev.clientY-sy)+'px'; boardHeight();}"
        "function up(){"
        "head.removeEventListener('pointermove',move);"
        "head.removeEventListener('pointerup',up); save();}"
        "head.addEventListener('pointermove',move);"
        "head.addEventListener('pointerup',up);});"
        "var h=n.querySelector('.resize-handle');"
        "h.addEventListener('pointerdown',function(e){"
        "e.preventDefault(); e.stopPropagation();"
        "var sx=e.clientX, sy=e.clientY, sw=n.offsetWidth, sh=n.offsetHeight;"
        "h.setPointerCapture(e.pointerId);"
        "function move(ev){"
        "n.style.width=Math.max(240,sw+ev.clientX-sx)+'px';"
        "n.style.height=Math.max(80,sh+ev.clientY-sy)+'px';"
        "n.style.overflow='auto'; boardHeight();}"
        "function up(){"
        "h.removeEventListener('pointermove',move);"
        "h.removeEventListener('pointerup',up); save();}"
        "h.addEventListener('pointermove',move);"
        "h.addEventListener('pointerup',up);});});"
        "var reset=document.getElementById('layout-reset');"
        "if(reset){reset.addEventListener('click',function(){"
        "try{localStorage.removeItem(key);}catch(e){} location.reload();});}"
        "})();</script>"
        "</body></html>"
    ).encode("utf-8")


ADD_FIELDS = [
    ("title", "Title *", "text"),
    ("slug", "Slug (optional — auto-generated from the title)", "text"),
    ("description", "Description", "text"),
    ("source", "Source URL", "url"),
    ("submitted_by", "Submitted by", "text"),
    ("prep_time", "Prep time", "text"),
    ("cook_time", "Cook time", "text"),
    ("total_time", "Total time", "text"),
    ("serves", "Serves", "number"),
    ("oven_temperature", "Oven temperature", "text"),
    ("pan", "Pan", "text"),
    ("done_temperature", "Done temperature", "text"),
    ("storage", "Storage notes", "text"),
]


def add_form(values=None, error=None):
    values = values or {}
    parts = []
    if error:
        parts.append(f"<p class='error'>{html.escape(error)}</p>")
    parts.append("<form method='post' action='/add'>")
    for name, label, kind in ADD_FIELDS:
        parts.append(
            f"<label for='{name}'>{html.escape(label)}</label>"
            f"<input type='{kind}' id='{name}' name='{name}'"
            f" value='{html.escape(values.get(name, ''), quote=True)}'>"
        )
    parts.append(
        "<label for='ingredients'>Ingredients * — one per line</label>"
        f"<textarea id='ingredients' name='ingredients'>{html.escape(values.get('ingredients', ''))}</textarea>"
        "<label for='steps'>Steps * — one per line</label>"
        f"<textarea id='steps' name='steps'>{html.escape(values.get('steps', ''))}</textarea>"
        "<p><button type='submit'>Save recipe</button></p>"
        "</form>"
    )
    return "".join(parts)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep stdout clean
        pass

    def send_html(self, body, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, status=200):
        body = json.dumps(obj, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        q = (query.get("q", [""])[0] or "").strip()
        conn = connect()
        try:
            if path == "/health":
                self.send_json({"status": "ok"})
            elif path == "/add":
                self.send_html(page("Add recipe", add_form()))
            elif path == "/":
                recipes = list_recipes(conn, q or None)
                search = (
                    f"<form class='search' method='get' action='/'>"
                    f"<input type='search' name='q' placeholder='Search recipes…'"
                    f" value='{html.escape(q, quote=True)}'>"
                    "<button type='submit'>Search</button></form>"
                )
                if q:
                    search += (
                        f"<p>Results for “{html.escape(q)}” — "
                        f"<a href='/'>clear search</a></p>"
                    )
                cards = "".join(
                    f"<div class='card'><h2><a href='/recipes/{html.escape(r['slug'])}'>"
                    f"{html.escape(r['title'])}</a></h2>"
                    f"<p class='meta'>{html.escape(r['total_time'] or '')}"
                    f" · serves {r['serves']}</p></div>"
                    for r in recipes
                )
                self.send_html(
                    page(
                        "Recipes",
                        search + (cards if cards else "<p>No recipes found.</p>"),
                    )
                )
            elif path == "/api/recipes":
                recipes = [
                    get_recipe(conn, r["slug"])
                    for r in list_recipes(conn, q or None)
                ]
                self.send_json(recipes)
            elif path.startswith("/api/recipes/"):
                recipe = get_recipe(conn, unquote(path[len("/api/recipes/"):]))
                if recipe is None:
                    self.send_json({"error": "not found"}, 404)
                else:
                    self.send_json(recipe)
            elif path.startswith("/recipes/"):
                recipe = get_recipe(conn, unquote(path[len("/recipes/"):]))
                if recipe is None:
                    self.send_html(page("Not found", "<p>No such recipe.</p>"), 404)
                else:
                    # Ingredients grouped by part (dough / bath / toppings…),
                    # each line convertible when it starts with a measurement.
                    ing_groups = []
                    for i in recipe["ingredients"]:
                        part = i.get("part", "")
                        if not ing_groups or ing_groups[-1][0] != part:
                            ing_groups.append((part, []))
                        ing_groups[-1][1].append(i["text"])
                    ingredients = "".join(
                        (f"<h3>{html.escape(part)}</h3>" if part else "")
                        + "<ul class='ingredients'>"
                        + "".join(ingredient_li(t) for t in texts)
                        + "</ul>"
                        for part, texts in ing_groups
                    )
                    steps = "".join(
                        f"<li>{html.escape(s)}</li>" for s in recipe["steps"]
                    )
                    chips = "".join(
                        f"<span class='chip'>{html.escape(v)}</span>"
                        for v in [
                            f"Prep {recipe['prep_time']}" if recipe["prep_time"] else "",
                            f"Cook {recipe['cook_time']}" if recipe["cook_time"] else "",
                            f"Total {recipe['total_time']}" if recipe["total_time"] else "",
                            f"Serves {recipe['serves']}" if recipe["serves"] else "",
                            f"Oven {recipe['oven_temperature']}" if recipe["oven_temperature"] else "",
                            f"Pan {recipe['pan']}" if recipe["pan"] else "",
                            f"Done at {recipe['done_temperature']}" if recipe["done_temperature"] else "",
                        ]
                        if v
                    )
                    storage = (
                        f"<h2>Storage</h2><p>{html.escape(recipe['storage'])}</p>"
                        if recipe["storage"]
                        else ""
                    )
                    source = (
                        f"<p>Source: <a href='{html.escape(recipe['source'], quote=True)}'>"
                        f"{html.escape(recipe['source'])}</a></p>"
                        if recipe["source"]
                        else ""
                    )
                    # Board of draggable, resizable section cards
                    # (issue #1, darkBoard-style nodes).
                    details = storage + source
                    details_node = (
                        "<div class='node' data-card='details'>"
                        "<div class='node-head'><span class='grip'>⠿</span>"
                        "Details</div>"
                        f"<div class='node-body'>{details}</div>"
                        "<div class='resize-handle'></div></div>"
                        if details
                        else ""
                    )
                    body = (
                        "<div class='board-tools'>"
                        "<button type='button' id='layout-reset'"
                        " class='theme-toggle'>Reset layout</button></div>"
                        f"<div class='board' id='board' data-slug='"
                        f"{html.escape(recipe['slug'], quote=True)}'>"
                        "<div class='node' data-card='recipe'>"
                        "<div class='node-head'><span class='grip'>⠿</span>"
                        "Recipe</div>"
                        f"<div class='node-body'><p>"
                        f"{html.escape(recipe['description'])}</p>"
                        f"<p>{chips}</p></div>"
                        "<div class='resize-handle'></div></div>"
                        "<div class='node' data-card='ingredients'>"
                        "<div class='node-head'><span class='grip'>⠿</span>"
                        "Ingredients"
                        "<button type='button' id='units-metric'"
                        " class='theme-toggle' data-mode='us'>"
                        "Show metric</button></div>"
                        f"<div class='node-body'>{ingredients}"
                        "<p class='meta'>Tap a measurement to convert it,"
                        " or hover for all equivalents.</p></div>"
                        "<div class='resize-handle'></div></div>"
                        "<div class='node' data-card='steps'>"
                        "<div class='node-head'><span class='grip'>⠿</span>"
                        "Steps</div>"
                        f"<div class='node-body'><ol>{steps}</ol></div>"
                        "<div class='resize-handle'></div></div>"
                        f"{details_node}"
                        "</div>"
                        f"<p><a href='/'>← All recipes</a></p>"
                    )
                    self.send_html(page(recipe["title"], body))
            else:
                self.send_html(page("Not found", "<p>No such page.</p>"), 404)
        finally:
            conn.close()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path != "/add":
            self.send_html(page("Not found", "<p>No such page.</p>"), 404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0 or length > 200_000:
            self.send_html(
                page("Add recipe", add_form(error="Form was empty or too large.")),
                400,
            )
            return
        raw = self.rfile.read(length).decode("utf-8", "replace")
        fields = {k: v[0] for k, v in parse_qs(raw).items()}
        values = {name: fields.get(name, "") for name, _, _ in ADD_FIELDS}
        values["ingredients"] = fields.get("ingredients", "")
        values["steps"] = fields.get("steps", "")
        title = values["title"].strip()
        ingredients = [l.strip() for l in values["ingredients"].splitlines() if l.strip()]
        steps = [l.strip() for l in values["steps"].splitlines() if l.strip()]
        slug = slugify(values["slug"] or title)
        serves = None
        if values["serves"].strip():
            try:
                serves = int(values["serves"])
            except ValueError:
                serves = None
                self.send_html(
                    page("Add recipe", add_form(values, "Serves must be a whole number.")),
                    400,
                )
                return
        if not title or not ingredients or not steps:
            self.send_html(
                page(
                    "Add recipe",
                    add_form(values, "Title, at least one ingredient, and at least one step are required."),
                ),
                400,
            )
            return
        recipe = {
            "slug": slug,
            "title": title,
            "description": values["description"].strip(),
            "source": values["source"].strip(),
            "submitted_by": values["submitted_by"].strip(),
            "prep_time": values["prep_time"].strip(),
            "cook_time": values["cook_time"].strip(),
            "total_time": values["total_time"].strip(),
            "serves": serves,
            "oven_temperature": values["oven_temperature"].strip(),
            "pan": values["pan"].strip(),
            "done_temperature": values["done_temperature"].strip(),
            "storage": values["storage"].strip(),
        }
        conn = connect()
        try:
            try:
                insert_recipe(conn, recipe, ingredients, steps)
            except sqlite3.IntegrityError:
                self.send_html(
                    page(
                        "Add recipe",
                        add_form(values, f"A recipe with slug “{slug}” already exists — pick a different slug."),
                    ),
                    400,
                )
                return
        finally:
            conn.close()
        self.send_response(303)
        self.send_header("Location", f"/recipes/{slug}")
        self.end_headers()


def main():
    seeded = init_db()
    print(f"recipeDB: {DB_PATH} (seeded {seeded} new recipe(s) from seed)", flush=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"recipeDB: listening on http://localhost:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
