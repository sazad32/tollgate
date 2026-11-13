# Gateway image (ADR 0009). Build from the repo root, which holds the uv workspace and lock file:
#   docker build -f gateway/Dockerfile -t tollgate-gateway .

# --- build: install the gateway and its locked dependencies into /app/.venv ---------------------
# uv's image with Python 3.12 (tag python3.12-bookworm-slim), pinned by digest.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58 AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Workspace metadata first, so the dependency layer is reused until uv.lock changes.
COPY pyproject.toml uv.lock ./
COPY gateway/pyproject.toml gateway/pyproject.toml
COPY billing/pyproject.toml billing/pyproject.toml
COPY bench/pyproject.toml bench/pyproject.toml
RUN uv sync --frozen --no-dev --package tollgate-gateway --no-install-workspace

# Then the gateway itself, installed as a normal (non-editable) package inside the venv.
COPY gateway/src gateway/src
RUN uv sync --frozen --no-dev --package tollgate-gateway --no-editable

# --- runtime: Python and the venv only; no uv, no compilers, no source tree ----------------------
# The same Python base the uv image is built on, so the venv's interpreter path is identical.
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
# Not root, and not root's group either. Kubernetes enforces this too (runAsNonRoot in deploy/gateway).
USER 10001:10001
EXPOSE 8000
# uvicorn picks uvloop and httptools when installed (ADR 0020). Keep-alive longer than clients'
# (httpx: 5 s), so the server never closes an idle connection just as a client reuses it: the
# week 14 load test lost requests to that race.
CMD ["uvicorn", "--factory", "tollgate_gateway.main:create_app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "65"]
