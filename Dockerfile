FROM node:22-slim AS web

WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json frontend/.npmrc ./
RUN npm ci --no-audit --no-fund
COPY frontend ./
RUN npm run build

FROM python:3.14-slim AS runtime

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 POETRY_VERSION=2.5.1

RUN apt-get update \
    && apt-get install -y --no-install-recommends libimage-exiftool-perl libheif1 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "poetry==$POETRY_VERSION"

WORKDIR /app
COPY pyproject.toml poetry.lock ./
RUN poetry config virtualenvs.create false && poetry install --only main --no-root --no-interaction

COPY darkroom ./darkroom
COPY --from=web /src/darkroom/static/app ./darkroom/static/app
COPY run.py ./

RUN useradd --system --uid 10001 --home-dir /app darkroom && mkdir -p /data /photos && chown darkroom /data
USER darkroom

ENV DARKROOM_HOST=0.0.0.0 DARKROOM_PORT=8000 DARKROOM_DATA_DIR=/data DARKROOM_PHOTOS_ROOT=/photos
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"
CMD ["python", "run.py", "serve"]
