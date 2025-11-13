# Development Guide

This guide provides comprehensive information for developers working on the Ollama Proxy Server project.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Local Development Setup](#local-development-setup)
- [Testing](#testing)
- [Code Quality](#code-quality)
- [CI/CD Pipeline](#ci-cd-pipeline)
- [Development Workflow](#development-workflow)
- [Troubleshooting](#troubleshooting)

## Prerequisites

- Python 3.11+ (recommended 3.12)
- [Poetry](https://python-poetry.org/docs/#installation) for dependency management
- Git
- Make (optional, for convenience commands)

### Installing Poetry

```bash
# Official installer
curl -sSL https://install.python-poetry.org | python3 -

# Or using pip
pip install poetry
```

## Local Development Setup

### 1. Clone the Repository

```bash
git clone https://github.com/ParisNeo/ollama_proxy_server.git
cd ollama_proxy_server
```

### 2. Install Dependencies

```bash
# Install all dependencies including dev dependencies
poetry install --with dev

# Or install only production dependencies
poetry install
```

### 3. Activate the Virtual Environment

```bash
# Activate the poetry shell
poetry shell

# Or run commands directly with poetry
poetry run <command>
```

### 4. Environment Configuration

Copy the example environment file and configure it:

```bash
cp .env.example .env
# Edit .env with your configuration
```

### 5. Run the Development Server

```bash
# Using poetry
poetry run python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8080

# Or using the provided script
./run.sh
```

## Testing

### Running Tests with Tox

The project uses [Tox](https://tox.wiki/) for testing across multiple Python versions and environments.

```bash
# Run all test environments
tox

# Run specific Python version
tox -e py312

# Run linting only
tox -e flake8,pylint,ruff

# Run formatting check
tox -e format

# Fix formatting issues
tox -e format-fix

# Run security checks
tox -e security-safety
tox -e security-bandit

# Run all checks (format, lint, test)
tox -e all
```

### Running Tests Directly

```bash
# Run all tests
poetry run pytest

# Run with coverage
poetry run pytest --cov=app --cov-report=html

# Run specific test file
poetry run pytest tests/test_specific.py

# Run with verbose output
poetry run pytest -v
```

### Test Structure

```
tests/
├── conftest.py          # Pytest configuration and fixtures
├── test_api/            # API endpoint tests
├── test_core/           # Core functionality tests
├── test_crud/           # Database operation tests
└── test_integration/    # Integration tests
```

## Code Quality

### Linting and Formatting

The project uses multiple code quality tools:

- **Black**: Code formatting
- **Ruff**: Fast Python linter
- **Flake8**: Style guide enforcement
- **Pylint**: Deep code analysis
- **Bandit**: Security vulnerability scanner
- **Safety**: Dependency vulnerability checker

### Running Code Quality Checks

```bash
# Format code
poetry run black app/ tests/

# Check formatting
poetry run black --check app/ tests/

# Run ruff
poetry run ruff check app/ tests/

# Run flake8
poetry run flake8 app/ tests/

# Run pylint
poetry run pylint app/

# Run security checks
poetry run bandit -r app/
poetry run safety check
```

### Pre-commit Hooks (Optional)

To set up pre-commit hooks for automatic code quality checks:

```bash
# Install pre-commit
pip install pre-commit

# Install the hooks
pre-commit install

# Run manually on all files
pre-commit run --all-files
```

## CI/CD Pipeline

The project includes comprehensive CI/CD pipelines for both GitHub Actions and GitLab CI.

### GitHub Actions

**Location**: `.github/workflows/ci.yml`

**Triggers**:
- Push to `main` or `develop` branches
- Pull requests to `main` or `develop` branches

**Jobs**:

1. **Lint**: Runs flake8, pylint, and ruff on all platforms
2. **Test**: Runs pytest on Python 3.11, 3.12, 3.13 across Linux, Windows, macOS
3. **Security**: Runs bandit and safety security scans
4. **Build**: Builds and validates the package (main branch only)

**Matrix Strategy**:
```yaml
strategy:
  matrix:
    os: [ubuntu-latest, windows-latest, macos-latest]
    python-version: ['3.11', '3.12', '3.13']
```

### GitLab CI

**Location**: `.gitlab-ci.yml`

**Stages**:
1. **lint**: Code quality checks
2. **test**: Test execution
3. **security**: Security scanning
4. **build**: Package building

**Platform Support**:
- Linux (Docker)
- Windows (Shell runners)
- macOS (Shell runners)

### Running CI/CD Locally

You can run the same checks locally using tox:

```bash
# Simulate the full CI pipeline
tox -e all

# Run specific environments like CI
tox -e py311,py312,py313,flake8,pylint,ruff,security
```

## Development Workflow

### 1. Create a Feature Branch

```bash
git checkout -b feature/your-feature-name
```

### 2. Make Changes

- Write code following the project's style guidelines
- Add tests for new functionality
- Update documentation as needed

### 3. Run Quality Checks

```bash
# Format code
tox -e format-fix

# Run all checks
tox -e all
```

### 4. Commit Changes

```bash
git add .
git commit -m "feat: add your feature description"
```

### 5. Push and Create Pull Request

```bash
git push origin feature/your-feature-name
```

### Commit Message Convention

Use [Conventional Commits](https://www.conventionalcommits.org/) format:

- `feat:` for new features
- `fix:` for bug fixes
- `docs:` for documentation changes
- `style:` for code style changes
- `refactor:` for code refactoring
- `test:` for adding or updating tests
- `chore:` for maintenance tasks

## Project Structure

```
ollama_proxy_server/
├── app/                    # Main application code
│   ├── api/               # API routes
│   ├── core/              # Core functionality
│   ├── crud/              # Database operations
│   ├── database/          # Database configuration
│   ├── schema/            # Pydantic models
│   ├── static/            # Static files
│   └── templates/         # Jinja2 templates
├── tests/                 # Test suite
├── .github/               # GitHub Actions workflows
├── .gitlab-ci.yml         # GitLab CI configuration
├── tox.ini               # Tox configuration
├── pyproject.toml        # Poetry configuration
└── DEVELOPMENT.md        # This file
```

## Environment Variables

Key environment variables for development:

```bash
# Database
DATABASE_URL=sqlite:///./ollama_proxy.db

# Security
SECRET_KEY=your-secret-key-here

# Server
HOST=0.0.0.0
PORT=8080
DEBUG=true

# Redis (for rate limiting)
REDIS_URL=redis://localhost:6379
```

## Database Management

### Migrations

```bash
# Create a new migration
poetry run alembic revision --autogenerate -m "description"

# Apply migrations
poetry run alembic upgrade head

# Rollback migration
poetry run alembic downgrade -1
```

### Database Reset

```bash
# Reset database (development only)
rm ollama_proxy.db
poetry run alembic upgrade head
```

## Performance Monitoring

### Profiling

```bash
# Install profiling dependencies
poetry install --with dev

# Run with profiling
poetry run python -m cProfile -o profile.stats -m uvicorn app.main:app
```

### Load Testing

```bash
# Install locust for load testing
pip install locust

# Run load tests
locust -f tests/load_test.py --host=http://localhost:8080
```

## Troubleshooting

### Common Issues

1. **Poetry Installation Issues**
   ```bash
   # Clear poetry cache
   poetry cache clear pypi --all
   ```

2. **Tox Environment Issues**
   ```bash
   # Recreate tox environments
   tox -r
   ```

3. **Database Connection Issues**
   ```bash
   # Check database file permissions
   ls -la ollama_proxy.db
   
   # Reset database
   ./reset.sh
   ```

4. **Port Already in Use**
   ```bash
   # Find process using port 8080
   lsof -i :8080
   
   # Kill process
   kill -9 <PID>
   ```

### Getting Help

- Check the [GitHub Issues](https://github.com/ParisNeo/ollama_proxy_server/issues)
- Review the main [README.md](README.md)
- Join the community discussions

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests for new functionality
5. Ensure all tests pass
6. Submit a pull request

### Code Review Process

- All changes require code review
- Maintain test coverage above 80%
- Follow the project's coding standards
- Update documentation as needed

## Release Process

Releases are automated through the CI/CD pipeline:

1. Version bump in `pyproject.toml`
2. Update CHANGELOG.md
3. Create release tag
4. GitHub Actions builds and publishes

## Additional Resources

- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [Poetry Documentation](https://python-poetry.org/docs/)
- [Pytest Documentation](https://docs.pytest.org/)
- [Tox Documentation](https://tox.wiki/)
- [SQLAlchemy Documentation](https://docs.sqlalchemy.org/)
