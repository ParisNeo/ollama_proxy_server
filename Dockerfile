# --- Builder Stage ---
FROM python:3.13-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y \
    build-essential \
    pkg-config \
    libcairo2-dev \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir poetry

COPY pyproject.toml ./

RUN poetry config virtualenvs.create false && \
    poetry install --without dev --no-root --no-interaction --no-ansi

# --- Runtime Stage ---
FROM python:3.13-slim

WORKDIR /home/app

RUN apt-get update && apt-get install -y \
    libcairo2 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir gunicorn

RUN addgroup --system app && adduser --system --group app

COPY --from=builder /usr/local/lib/python3.13 /usr/local/lib/python3.13
COPY --from=builder /usr/local/bin /usr/local/bin

COPY ./app ./app
COPY gunicorn_conf.py .

RUN mkdir -p ./app/static/uploads ./.ssl ./benchmarks && \
    chown -R app:app /home/app

USER app

EXPOSE 8080

CMD ["gunicorn", "-c", "./gunicorn_conf.py", "app.main:app"]