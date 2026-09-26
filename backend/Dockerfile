# syntax=docker/dockerfile:1

# =============================================================================
# Build stage — resolve and install dependencies into an isolated virtualenv.
# Kept separate so the runtime image carries no compilers or build caches.
# =============================================================================
FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Dependency metadata first: this layer is cached until pyproject.toml changes,
# so ordinary code edits rebuild in seconds.
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .


# =============================================================================
# Runtime stage
# =============================================================================
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/code \
    PATH="/opt/venv/bin:$PATH"

# curl is used by the container healthcheck only.
RUN apt-get update \
    && apt-get install --no-install-recommends -y curl \
    && rm -rf /var/lib/apt/lists/*

# Never run the API as root.
RUN groupadd --system app && useradd --system --gid app --create-home app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /code
COPY --chown=app:app . /code

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl --fail --silent http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
