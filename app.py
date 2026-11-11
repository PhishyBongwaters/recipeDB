#!/usr/bin/env python3
"""recipeDB: a tiny recipe database, Python standard library only.

Storage is SQLite. The database file lives outside the repo (in the
container, a named volume mounted at /data), so rebuilding the image
never wipes recipes. On first start, when the recipes table is empty,
the app seeds itself from data/seed.json.

Routes:
  GET /                     recipe list (HTML), ?q= searches title,
                            description and ingredients
  GET /add                  add-recipe form (HTML)
  POST /add                 save a new recipe, redirect to its page
  GET /recipes/<slug>       one recipe (HTML)
  GET /api/recipes          all recipes (JSON), ?q= filters the same way
  GET /api/recipes/<slug>   one recipe (JSON)
  GET /health               {"status": "ok"}

Recipes added through the form go into the SQLite database (the
volume), not into data/seed.json — seed.json only fills an empty
database on first start.

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
    """Seed from seed.json only when the recipes table is empty."""
    count = conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
    if count:
        return 0
    if not SEED_PATH.exists():
        return 0
    recipes = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    if isinstance(recipes, dict):
        recipes = recipes.get("recipes", [])
    for recipe in recipes:
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
    return len(recipes)


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
                    ingredients = "".join(
                        f"<li>{html.escape(i['text'])}</li>"
                        for i in recipe["ingredients"]
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
                    body = (
                        f"<p>{html.escape(recipe['description'])}</p>"
                        f"<p>{chips}</p>"
                        f"<h2>Ingredients</h2><ul>{ingredients}</ul>"
                        f"<h2>Steps</h2><ol>{steps}</ol>"
                        f"{storage}{source}"
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
    print(f"recipeDB: {DB_PATH} (seeded {seeded} recipe(s) on first run)", flush=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"recipeDB: listening on http://localhost:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
