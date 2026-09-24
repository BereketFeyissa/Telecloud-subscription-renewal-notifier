# syntax=docker/dockerfile:1.7

# Base image pinned by digest (CLAUDE.md §13.2). Verified on 2026-09-16 against
# docker.io/library/python:3.12-slim. To refresh:
#     docker pull python:3.12-slim
#     docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim

FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9 AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install --no-cache-dir --retries 5 --timeout 60 uv==0.7.22

WORKDIR /app

# Dependencies first so the layer caches independently of application code.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ ./src/
RUN uv sync --frozen --no-dev


FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9 AS runtime

# Non-root numeric UID, per CLAUDE.md §13.3.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv
COPY --from=builder --chown=10001:10001 /app/src /app/src

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    METRICS_PORT=9100

USER 10001:10001
EXPOSE 9100

# Args come from the manifest; the image itself is not opinionated about mode.
ENTRYPOINT ["python", "-m", "tele_scraper"]
