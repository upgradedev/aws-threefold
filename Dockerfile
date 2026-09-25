# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STAGE=prod \
    PORT=8001

WORKDIR /app

RUN addgroup --system --gid 1001 threefold && \
    adduser --system --uid 1001 --gid 1001 threefold

COPY pyproject.toml /app/
# The OpenAPI document travels with the pages that read it, under src/, so the
# image and the Lambda package carry the same file from the same place.
COPY src/ /app/src/
COPY scripts/ /app/scripts/

ENV PYTHONPATH="/app/src"

USER threefold

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8001/status')"

CMD ["python", "src/threefold/interfaces/server.py", "--port", "8001", "--host", "0.0.0.0"]
