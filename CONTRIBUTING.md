# Contributing to Ollama Proxy Fortress

Thank you for your interest in contributing! To maintain high code quality and ensure a smooth CI/CD process, please follow these guidelines.

## 🛠️ Development Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/ParisNeo/ollama_proxy_server.git
   cd ollama_proxy_server
   ```

2. **Install dependencies using Poetry:**
   ```bash
   poetry install --with dev
   ```

3. **Install Pre-commit hooks:**
   We use `pre-commit` to ensure your code is formatted and linted every time you commit.
   ```bash
   pip install pre-commit
   pre-commit install
   ```

## 🧹 Code Quality (Linting)

Before pushing your code, you should ensure it passes our linting suite.

### Automated (Pre-commit)
Once installed, `pre-commit` runs automatically on `git commit`. If it finds errors (like trailing whitespace or formatting issues), it will stop the commit and fix them. You just need to `git add` the changes and commit again.

To run it manually against all files:
```bash
pre-commit run --all-files
```

### Manual (Full Suite with Tox)
We use `tox` to mirror the GitHub Actions environment. This is the most reliable way to check if your code will pass the CI.

```bash
# Run all linting checks (Ruff, Flake8, Pylint, Black)
poetry run tox -e flake8,pylint,ruff,format
```

### Fast One-off Checks
If you want to check specific tools quickly without creating environments:
```bash
poetry run ruff check app/    # Fast linting
poetry run black --check app/ # Format check
poetry run pylint app/        # Deep static analysis
```

## 🧪 Testing

Always run tests before submitting a Pull Request:

```bash
# Run all tests
poetry run pytest

# Run tests via Tox for a specific Python version (e.g., 3.12)
poetry run tox -e py312
```

## 📝 Pull Request Process

1. Create a new branch for your feature or fix: `git checkout -b feature/my-new-feature`.
2. Ensure all linting and tests pass locally.
3. Write clear, descriptive commit messages.
4. Submit your PR against the `develop` branch (or `main` if specified).

---
*Note: This project adheres to a "Balanced" response and development style—be helpful, concise, and prioritize security.*