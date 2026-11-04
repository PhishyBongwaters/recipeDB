# recipeDB

A small, containerized recipe database. Python standard library only —
no pip dependencies, so the image build is just Python + the app.

Storage is SQLite, in a Docker named volume (`recipedb-data`), not in
the repo and not in the image. Rebuilding the container keeps recipes.

## Run

```sh
docker compose up --build
```

Then open http://localhost:8080

- `/` — recipe list
- `/recipes/easy-meatloaf` — one recipe
- `/api/recipes` — all recipes as JSON
- `/api/recipes/easy-meatloaf` — one recipe as JSON
- `/health` — health check

## Seeding

On first start, when the recipes table is empty, the app seeds itself
from `data/seed.json`. Seeding never overwrites recipes already in the
database. To reseed from scratch, stop the container and drop the
volume (`docker compose down -v`), then start again — that deletes
all recipes, so only do it on purpose.

## Adding recipes

Add an entry to `data/seed.json` before first run, or add it to a
fresh database the same way. (A write UI / API is a later step, not
in this first version.)

## First recipe

Easy Meatloaf, from Allrecipes:
https://www.allrecipes.com/recipe/16354/easy-meatloaf/

Prep 15 mins, cook 1 hr, serves 8, done at 160°F in the middle.

## Layout

- `app.py` — the whole app: HTTP server + SQLite + seeding
- `data/seed.json` — seed recipes, loaded only into an empty database
- `Dockerfile` — python:3.12-slim image
- `docker-compose.yml` — app + named volume for `/data`
