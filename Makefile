.PHONY: install format lint test build publish clean

# Python executable from venv
PYTHON := .venv/bin/python
PIP := .venv/bin/pip
RUFF := .venv/bin/ruff
MYPY := .venv/bin/mypy
PYTEST := .venv/bin/pytest

# Install package with dev dependencies
install:
	$(PIP) install -e ".[dev]"

# Format code with ruff
format:
	$(RUFF) format src tests
	$(RUFF) check --fix src tests

# Lint code
lint:
	$(RUFF) check src tests
	$(RUFF) format --check src tests
	$(MYPY) src

# Run tests with coverage
test:
	$(PYTEST) tests/ -v --cov=ha_garmin --cov-report=term-missing --cov-report=html

# Run tests without coverage (faster)
test-quick:
	$(PYTEST) tests/ -v

# Build package
build: clean
	$(PIP) install build
	$(PYTHON) -m build

# Publish to PyPI
publish: build
	$(PIP) install twine
	$(PYTHON) -m twine upload dist/*

# Publish to Test PyPI
publish-test: build
	$(PIP) install twine
	$(PYTHON) -m twine upload --repository testpypi dist/*

# Clean build artifacts
clean:
	rm -rf build/
	rm -rf dist/
	rm -rf *.egg-info/
	rm -rf src/*.egg-info/
	rm -rf .pytest_cache/
	rm -rf .mypy_cache/
	rm -rf .ruff_cache/
	rm -rf htmlcov/
	rm -rf .coverage
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# Run all checks (CI simulation)
all: format lint test

# Show help
help:
	@echo "Available targets:"
	@echo "  install       - Install package with dev dependencies"
	@echo "  format        - Format code with ruff"
	@echo "  lint          - Run linting (ruff + mypy)"
	@echo "  test          - Run tests with coverage"
	@echo "  test-quick    - Run tests without coverage"
	@echo "  build         - Build wheel and sdist"
	@echo "  publish       - Publish to PyPI"
	@echo "  publish-test  - Publish to Test PyPI"
	@echo "  clean         - Remove build artifacts"
	@echo "  all           - Run format, lint, and test"
