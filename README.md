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

The UI is dark by default, with a Light/Dark toggle in the header.
Your choice is remembered in the browser (localStorage).

- `/` — recipe list, with a search box (searches title, description
  and ingredients; also `/?q=beef`)
- `/add` — add-recipe form: title, times, serves, oven/pan,
  ingredients (one per line) and steps (one per line). The slug is
  auto-generated from the title unless you enter one. Saving
  redirects to the new recipe's page.
- `/recipes/easy-meatloaf` — one recipe
- `/api/recipes` — all recipes as JSON (also accepts `?q=`)
- `/api/recipes/easy-meatloaf` — one recipe as JSON
- `/health` — health check

Recipes added through the form are saved in the SQLite volume, not
in `data/seed.json` — so they survive restarts and rebuilds, but
they are not committed to the repo. To share a recipe, add it to
`data/seed.json` as well (or export it from `/api/recipes/<slug>`).

## Seeding

On first start, when the recipes table is empty, the app seeds itself
from `data/seed.json`. Seeding never overwrites recipes already in the
database. To reseed from scratch, stop the container and drop the
volume (`docker compose down -v`), then start again — that deletes
all recipes, so only do it on purpose.

## Adding recipes

Use the `/add` form in the running app, or add an entry to
`data/seed.json` before first run. Form-added recipes live in the
volume only; seed entries are the ones shared in the repo.

## Recipes in the seed

- Easy Meatloaf, from Allrecipes:
  https://www.allrecipes.com/recipe/16354/easy-meatloaf/
  Prep 15 mins, cook 1 hr, serves 8, done at 160°F in the middle.
- New York Style Bagel, from Urban Farm and Kitchen:
  https://urbanfarmandkitchen.com/new-york-style-bagel-recipe/
  Bake 20-25 mins at 425°F, makes 6 large or 9 small bagels.
  Note: the page gives no prep/total time or single serving count,
  so those fields are left blank in the seed rather than guessed.

## Layout

- `app.py` — the whole app: HTTP server + SQLite + seeding
- `data/seed.json` — seed recipes, loaded only into an empty database
- `Dockerfile` — python:3.12-slim image
- `docker-compose.yml` — app + named volume for `/data`
