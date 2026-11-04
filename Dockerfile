FROM python:3.12-slim

WORKDIR /app
COPY app.py .
COPY data/seed.json ./data/seed.json

RUN mkdir -p /data

ENV PORT=8080
ENV RECIPEDB_PATH=/data/recipes.db
ENV RECIPEDB_SEED=/app/data/seed.json

EXPOSE 8080

CMD ["python3", "app.py"]
