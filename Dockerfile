FROM node:22-slim AS web

WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json frontend/.npmrc ./
RUN npm ci --no-audit --no-fund
COPY frontend ./
RUN npm run build

FROM python:3.15-slim AS deps

ENV POETRY_VERSION=2.5.1 POETRY_VIRTUALENVS_IN_PROJECT=true POETRY_NO_INTERACTION=1

RUN pip install --no-cache-dir "poetry==$POETRY_VERSION"

WORKDIR /app
COPY pyproject.toml poetry.lock ./
RUN poetry install --only main --no-root \
    && .venv/bin/python -m pip uninstall -y pip setuptools wheel

FROM python:3.15-slim AS worker-deps

COPY deploy/single/requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/worker \
    && /opt/worker/bin/pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
    && /opt/worker/bin/python -c "import playwright.sync_api; from importlib import metadata; assert metadata.version('playwright') == '1.65.2'" \
    && /opt/worker/bin/python -m pip uninstall -y pip

FROM python:3.15-slim AS syncthing

RUN set -eu; \
    version=v2.1.7; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) sum=2edc416e7ed136b7c858ed5f3f02a3b8316589605e54c380228ceea7acbf0142 ;; \
        arm64) sum=48e456690cd2b97e7d26b02b5ac35f62a3b36172267b144cfeaf02c993ab955c ;; \
        *) echo "no pinned Syncthing for $arch" >&2; exit 1 ;; \
    esac; \
    name="syncthing-linux-$arch-$version"; \
    python -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' \
        "https://github.com/syncthing/syncthing/releases/download/$version/$name.tar.gz" /tmp/syncthing.tar.gz; \
    echo "$sum  /tmp/syncthing.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/syncthing.tar.gz -C /tmp "$name/syncthing"; \
    install -m 0755 "/tmp/$name/syncthing" /syncthing

FROM python:3.15-slim AS runtime

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH=/app/.venv/bin:$PATH \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright DISPLAY=:99

# zlib1g is named so the image gets Debian's patched build (1:1.3.dfsg+really1.3.1-1+deb13u1) before python:3.15-slim
# is rebuilt with it; the scan fails on CVE-2026-76844 and CVE-2026-85091 until then. The krb5 libraries, which these
# packages pull in (python:3.15-slim has none), are named so this layer installs Debian's patched 1.21.3-5+deb13u2
# rather than reusing a cached layer built with 1.21.3-5+deb13u1; the scan fails on CVE-2026-71199, CVE-2026-71200,
# CVE-2026-107364, CVE-2026-112315 to CVE-2026-112323, CVE-2026-118380 and CVE-2026-119079 with the older build.
# Name libzstd1 so apt upgrades the base image's copy to Debian's patched 1.5.7+dfsg-1+deb13u1 or later (CVE-2026-124851).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libimage-exiftool-perl libheif1 zlib1g libzstd1 \
       libgssapi-krb5-2 libk5crypto3 libkrb5-3 libkrb5support0 \
       xvfb x11-utils x11vnc novnc websockify tini libatomic1 \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip uninstall -y pip

COPY --from=worker-deps /opt/worker /opt/worker
# Playwright's bundled Node driver (1.65 on) links libatomic.so.1, and it runs before --with-deps installs anything.
RUN /opt/worker/bin/python -m playwright install --with-deps --no-shell chromium \
    && if [ "$(dpkg --print-architecture)" = "amd64" ]; then /opt/worker/bin/python -m playwright install --with-deps chrome; fi \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/worker/app
COPY darkroom_browser ./darkroom_browser
COPY deploy/browser/smoke.sh ./smoke.sh

WORKDIR /app
COPY --from=deps /app/.venv ./.venv
COPY darkroom ./darkroom
COPY --from=web /src/darkroom/static/app ./darkroom/static/app
COPY run.py ./
COPY darkroom_init /opt/init/darkroom_init
COPY --from=syncthing /syncthing /usr/local/bin/syncthing

ARG PUID=99
ARG PGID=100
RUN if ! getent group "$PGID" >/dev/null; then groupadd --system --gid "$PGID" darkroom-share; fi \
    && useradd --system --uid "$PUID" --gid "$PGID" --home-dir /home/darkroom --create-home darkroom \
    && mkdir -p /data /photos /tmp/.X11-unix /run/darkroom \
    && chmod 1777 /tmp/.X11-unix \
    && chown darkroom /data /home/darkroom /run/darkroom \
    && chmod 0700 /run/darkroom

ARG DARKROOM_VERSION=""
ARG DARKROOM_COMMIT=""
LABEL org.opencontainers.image.version="${DARKROOM_VERSION:-dev}" org.opencontainers.image.revision="${DARKROOM_COMMIT}"
RUN printf '%s\n%s\n' "$DARKROOM_VERSION" "$DARKROOM_COMMIT" > /app/darkroom/build-info \
    && chmod 0644 /app/darkroom/build-info
USER darkroom

ENV DARKROOM_HOST=0.0.0.0 DARKROOM_PORT=8000 DARKROOM_DATA_DIR=/data DARKROOM_PHOTOS_ROOT=/photos DARKROOM_BROWSER_ROOT=/data/browser DARKROOM_ONE_CONTAINER=1
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/usr/bin/env", "PYTHONPATH=/opt/init", "python", "-m", "darkroom_init"]
