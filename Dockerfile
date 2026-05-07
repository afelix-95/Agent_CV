# ── Stage 1: dependencies ──────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

COPY requirements.txt pyproject.toml ./
COPY src/ ./src/

RUN pip install --no-cache-dir --prefix=/install -r requirements.txt && \
    pip install --no-cache-dir --no-deps --prefix=/install .

# ── Stage 2: runtime ───────────────────────────────────────────────────────────
FROM python:3.12-slim

ARG APP_VERSION=dev
LABEL org.opencontainers.image.version=$APP_VERSION \
      org.opencontainers.image.title="agent-cv"

WORKDIR /app

# System libraries required by WeasyPrint (Pango, GObject, Cairo, etc.)
# --fix-missing retries with a mirror fallback on transient hash-mismatch errors.
RUN apt-get update && apt-get install -y --no-install-recommends --fix-missing \
        libgobject-2.0-0 \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libpangocairo-1.0-0 \
        libcairo2 \
        libgdk-pixbuf-2.0-0 \
        libffi8 \
        shared-mime-info \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages (deps + agent_cv package + metadata) from builder stage
COPY --from=builder /install /usr/local

# Embed SQL migrations so apply_schema() can find them at runtime.
COPY sql/ /app/sql/

# Expose the PDF folder as a mount point.
# CVs are NOT baked into the image; mount the host directory at runtime.
VOLUME ["/app/PDFs"]

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "agent_cv.main:app", "--host", "0.0.0.0", "--port", "8000"]
