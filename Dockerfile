FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --upgrade pip && pip install .

COPY alembic.ini ./
COPY alembic/ ./alembic/

ENV PYTHONPATH=/app/src

CMD ["python", "-m", "app.main", "web"]
