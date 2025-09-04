# --- Build Stage ---
FROM python:3.11-slim as builder

# Set working directory
WORKDIR /app

# Install poetry
RUN pip install poetry

# Copy only dependency-defining files
COPY poetry.lock pyproject.toml ./

# Install dependencies, without dev dependencies, into a virtual environment
RUN poetry config virtualenvs.create false && \
    poetry install --no-dev --no-interaction --no-ansi

# --- Final Stage ---
FROM python:3.11-slim

# Set a non-root user
RUN addgroup --system app && adduser --system --group app
USER app

# Set working directory
WORKDIR /home/app

# Copy virtual environment from builder stage
COPY --from=builder /app ./

# Copy application code
COPY ./app ./app
COPY gunicorn_conf.py .

# Expose the port the app runs on
EXPOSE 8080

# Command to run the application using our custom Gunicorn config file.
# This ensures structured JSON logging is used in production.
CMD ["gunicorn", "-c", "./gunicorn_conf.py", "app.main:app"]