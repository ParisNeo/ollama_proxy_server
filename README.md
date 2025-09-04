# Ollama Proxy Server v8

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python Version](https://img.shields.io/badge/python-3.11+-green.svg)](https://www.python.org/downloads/)
[![Framework](https://img.shields.io/badge/Framework-FastAPI-blueviolet)](https://fastapi.tiangolo.com/)
[![GitHub Stars](https://img.shields.io/github/stars/ParisNeo/ollama_proxy_server?style=social)](https://github.com/ParisNeo/ollama_proxy_server)

Ollama Proxy Server is a secure, high-performance proxy and load balancer for Ollama, rebuilt from the ground up using FastAPI. It is designed to act as a security gateway, protecting your Ollama instances from vulnerabilities while providing robust user management, rate limiting, IP filtering, usage tracking, and centralized control.

**This new version is a complete architectural overhaul and is not backward-compatible with the previous version.**

**Author:** ParisNeo

**License:** Apache 2.0

**Repository:** [https://github.com/ParisNeo/ollama_proxy_server](https://github.com/ParisNeo/ollama_proxy_server)

## Core Features

*   **Modern & Fast:** Built on FastAPI for high performance and asynchronous request handling.
*   **Database-Backed User Management:** Manages users and API keys via a robust database (SQLite by default).
*   **Secure API Key Authentication:** All requests require a valid `Bearer` token.
*   **Admin UI:** A simple, secure web interface for managing users, keys, and viewing usage statistics.
*   **Rate Limiting:** Protects backend servers from abuse using a Redis-backed rate limiter.
*   **IP Filtering:** Configure IP allow/deny lists for network-level access control.
*   **Model Federation:** The `/api/tags` endpoint aggregates models from all connected Ollama backends into a single list.
*   **Containerized:** A production-ready, multi-stage `Dockerfile` for secure and efficient deployment.
*   **Structured Logging:** Outputs JSON logs for easy integration with modern monitoring systems like ELK Stack, Datadog, or Splunk.

## Admin Interface

The server includes a web-based admin interface available at `/admin`. To log in, use the `ADMIN_USER` and `ADMIN_PASSWORD` credentials specified in your `.env` file.

From the admin dashboard, you can:
*   Create and delete users.
*   Create and revoke API keys for any user.
*   View a dashboard of API usage statistics per key.

## API Authentication

To use the proxy, you must include an `Authorization` header with a valid API key.

```bash
curl http://localhost:8080/api/tags \
  -H "Authorization: Bearer <your_api_key>"
```

API keys must be generated via the admin interface.

## Getting Started

### Prerequisites

*   Python 3.11+
*   Poetry for dependency management
*   Docker and Docker Compose (recommended for deployment)
*   An active Redis instance (required for rate limiting)

### 1. Local Development

**Clone the repository:**
```bash
git clone https://github.com/ParisNeo/ollama_proxy_server.git
cd ollama_proxy_server
```

**Install dependencies:**
```bash
poetry install
```

**Configure the server:**
Copy `.env.example` to `.env` and customize the settings.
```bash
cp .env.example .env
```
You must configure your `OLLAMA_SERVERS`, `SECRET_KEY`, and `REDIS_URL`.

**Initialize and upgrade the database:**
```bash
poetry run alembic upgrade head
```

**Run the development server:**
```bash
poetry run uvicorn app.main:app --reload
```
The server is now available at `http://127.0.0.1:8000`.

### 2. Deployment with Docker

**Build the Docker image:**
```bash
docker build -t ollama-proxy-server .
```

**Run the container:**
Create a `.env` file on your host machine with your production configuration. Then, run the container.
```bash
docker run -d --name ollama-proxy \
  -p 8080:8080 \
  --env-file ./.env \
  -v ./ollama_proxy.db:/home/app/ollama_proxy.db \
  ollama-proxy-server
```
This command mounts a local SQLite database file for persistence. The container will automatically run database migrations on startup.

## Configuration

Configuration is managed via environment variables, documented in `.env.example`. Key settings include `OLLAMA_SERVERS`, `DATABASE_URL`, `REDIS_URL`, `RATE_LIMIT_REQUESTS`, and `ALLOWED_IPS`/`DENIED_IPS`.

## Contributing

Contributions are welcome! Please fork the repository, create a feature branch, and open a pull request.

## License

This project is licensed under the Apache License 2.0. See the `LICENSE` file for details.
