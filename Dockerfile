# ─── Build stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md .
COPY app ./app
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir ".[dev]"

# ─── Runtime stage ────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

WORKDIR /app

# Runtime system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application source
COPY . .

# Run as non-root user for security
RUN useradd --no-create-home --shell /bin/false appuser \
    && chown -R appuser /app
USER appuser

EXPOSE 8000

# Apply pending Alembic migrations with retries, then start the server.
# This avoids crash loops when DNS/network for the DB is not ready yet.
CMD ["sh", "-c", "attempt=1; max_attempts=30; until alembic upgrade head; do if [ \"$attempt\" -ge \"$max_attempts\" ]; then echo 'Database migration failed after retries.'; exit 1; fi; echo \"Migration attempt $attempt/$max_attempts failed, retrying in 2s...\"; attempt=$((attempt+1)); sleep 2; done; exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
