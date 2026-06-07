# SQLoop dashboard -> Cloud Run.
#
# Build from the slim context produced by ./deploy.sh (the .deploy/ staging dir),
# NOT from the repo root: the raw repo carries the full 1.7G Spider dataset, while
# the staged context keeps only the 20 dev DBs the dashboard executes against.
#
# Deps are installed from requirements.txt (a PyPI-resolved export of uv.lock).
# We deliberately do NOT use `uv sync` / uv.lock here: the lock pins every package
# to a China mirror (pypi.tuna.tsinghua.edu.cn) that is slow/unreachable from
# Google Cloud Build. The export is index-free, so PyPI is used at build time.
FROM python:3.13-slim

# uv: just the static binary, for fast resolution-free installs.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_INDEX_URL=https://pypi.org/simple \
    UV_DEFAULT_INDEX=https://pypi.org/simple \
    UV_HTTP_TIMEOUT=180 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

# 1) Dependencies first — this layer is cached unless requirements.txt changes.
#    No pyproject.toml/uv.lock in the cwd yet, so uv can't pick up the mirror config.
COPY requirements.txt ./
RUN uv pip install --system --no-cache -r requirements.txt

# 2) Application code + the already-trimmed Spider dev data (from .deploy/).
COPY . .

EXPOSE 8080
# app.py binds 0.0.0.0 and reads $PORT (Cloud Run injects PORT=8080).
CMD ["python", "app.py"]
