.PHONY: install format lint test build publish clean fetch fetch-debug add-data

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
# Run the async library against a real account, without Home Assistant.
# Credentials: GARMIN_EMAIL / GARMIN_PASSWORD, or prompted; tokens are kept
# in .garmin_tokens.json between runs.
fetch:
	$(PYTHON) scripts/test_fetch_data.py

# Same, with asyncio debug mode: logs any callback that blocks the event loop
# for more than 100 ms (what Home Assistant flags as blocking I/O).
fetch-debug:
	PYTHONASYNCIODEBUG=1 $(PYTHON) -X dev scripts/test_fetch_data.py

# Interactive write actions (nutrition, hydration, blood pressure, uploads, ...).
add-data:
	$(PYTHON) scripts/test_add_data.py

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
	@echo "  fetch         - Fetch all data from a real account (no Home Assistant)"
	@echo "  fetch-debug   - fetch, reporting anything that blocks the event loop"
	@echo "  add-data      - Interactive write actions against a real account"
	@echo "  clean         - Remove build artifacts"
	@echo "  all           - Run format, lint, and test"
