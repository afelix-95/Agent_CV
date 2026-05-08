# ── Stage 1: dependencies ──────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS builder

WORKDIR /build

# Allow pip to connect through the corporate SSL-intercepting proxy.
# Lowering OpenSSL SECLEVEL from 2 → 1 and MinProtocol to TLSv1 fixes the
# DECRYPTION_FAILED_OR_BAD_RECORD_MAC error caused by SSL-inspection proxies.
ENV PIP_TRUSTED_HOST="pypi.org files.pythonhosted.org pypi.python.org"
RUN sed -i 's/\(CipherString\s*=\s*DEFAULT@SECLEVEL=\)2/\11/' /etc/ssl/openssl.cnf \
 && sed -i 's/MinProtocol = TLSv1.2/MinProtocol = TLSv1/g' /etc/ssl/openssl.cnf

COPY requirements.txt pyproject.toml ./
COPY src/ ./src/

RUN pip install --no-cache-dir --prefix=/install -r requirements.txt && \
    pip install --no-cache-dir --no-deps --prefix=/install .

# ── Stage 2: runtime ───────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm

ARG APP_VERSION=dev
LABEL org.opencontainers.image.version=$APP_VERSION \
      org.opencontainers.image.title="agent-cv"

WORKDIR /app

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
