#!/usr/bin/env python3
"""recipeDB: a tiny recipe database, Python standard library only.

Storage is SQLite. The database file lives outside the repo (in the
container, a named volume mounted at /data), so rebuilding the image
never wipes recipes. On first start, when the recipes table is empty,
the app seeds itself from data/seed.json.

Routes:
  GET /                     recipe list (HTML)
  GET /recipes/<slug>       one recipe (HTML)
  GET /api/recipes          all recipes (JSON)
  GET /api/recipes/<slug>   one recipe (JSON)
  GET /health               {"status": "ok"}

Environment:
  RECIPEDB_PATH  SQLite path, default /data/recipes.db
  RECIPEDB_SEED  seed JSON path, default data/seed.json next to app.py
  PORT           listen port, default 8080
"""

import html
import json
import os
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

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


def list_recipes(conn):
    rows = conn.execute(
        "SELECT slug, title, total_time, serves FROM recipes ORDER BY title"
    ).fetchall()
    return [dict(r) for r in rows]


def page(title, body):
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:720px;"
        "margin:2rem auto;padding:0 1rem;line-height:1.5}</style>"
        f"</head><body><h1>{html.escape(title)}</h1>{body}</body></html>"
    ).encode("utf-8")


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
        path = urlparse(self.path).path.rstrip("/") or "/"
        conn = connect()
        try:
            if path == "/health":
                self.send_json({"status": "ok"})
            elif path == "/":
                recipes = list_recipes(conn)
                items = "".join(
                    f"<li><a href='/recipes/{html.escape(r['slug'])}'>"
                    f"{html.escape(r['title'])}</a>"
                    f" — {html.escape(r['total_time'] or '')}"
                    f", serves {r['serves']}</li>"
                    for r in recipes
                )
                self.send_html(
                    page("Recipes", f"<ul>{items}</ul>" if items else "<p>No recipes yet.</p>")
                )
            elif path == "/api/recipes":
                recipes = [
                    get_recipe(conn, r["slug"]) for r in list_recipes(conn)
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
                    body = (
                        f"<p>{html.escape(recipe['description'])}</p>"
                        f"<p>Prep {html.escape(recipe['prep_time'])} · "
                        f"Cook {html.escape(recipe['cook_time'])} · "
                        f"Total {html.escape(recipe['total_time'])} · "
                        f"Serves {recipe['serves']}</p>"
                        f"<p>Oven {html.escape(recipe['oven_temperature'])} · "
                        f"Pan {html.escape(recipe['pan'])}</p>"
                        f"<p>Done at {html.escape(recipe['done_temperature'])}</p>"
                        f"<h2>Ingredients</h2><ul>{ingredients}</ul>"
                        f"<h2>Steps</h2><ol>{steps}</ol>"
                        f"<p>Source: <a href='{html.escape(recipe['source'])}'>"
                        f"{html.escape(recipe['source'])}</a></p>"
                        f"<p><a href='/'>← All recipes</a></p>"
                    )
                    self.send_html(page(recipe["title"], body))
            else:
                self.send_html(page("Not found", "<p>No such page.</p>"), 404)
        finally:
            conn.close()


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
