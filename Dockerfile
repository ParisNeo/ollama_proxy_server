# --- Build Stage ---
FROM python:3.13-slim AS builder

# Set working directory
WORKDIR /app

# Install poetry
RUN pip install poetry gunicorn

# Copy only dependency-defining files
COPY pyproject.toml ./

# Install dependencies, without dev dependencies, into a virtual environment
RUN poetry config virtualenvs.create false && \
    poetry install --without dev --no-root --no-interaction --no-ansi

# Set a non-root user
RUN addgroup --system app && adduser --system --group app
USER app

# Set working directory
WORKDIR /home/app

COPY ./app ./app
COPY gunicorn_conf.py .

# Expose the port the app runs on
EXPOSE 8080

# Command to run the application using our custom Gunicorn config file.
# This ensures structured JSON logging is used in production.
CMD ["gunicorn", "-c", "./gunicorn_conf.py", "app.main:app"]
