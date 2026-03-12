.PHONY: dev test lint typecheck migrate downgrade shell clean help

# ─── Development ──────────────────────────────────────────────────────────────

dev:  ## Start the full stack (server + db + quickchart)
	docker compose up --build

dev-db:  ## Start only the database (useful when running the server locally)
	docker compose up db quickchart

# ─── Testing ──────────────────────────────────────────────────────────────────

test:  ## Run the test suite
	pytest

test-cov:  ## Run tests with coverage report
	pytest --cov=app --cov-report=term-missing --cov-report=html

# ─── Code quality ─────────────────────────────────────────────────────────────

lint:  ## Run ruff linter
	ruff check .

lint-fix:  ## Run ruff linter and auto-fix issues
	ruff check --fix .

format:  ## Run ruff formatter
	ruff format .

typecheck:  ## Run mypy type checker
	mypy app

check: lint typecheck  ## Run all code quality checks

# ─── Database ─────────────────────────────────────────────────────────────────

migrate:  ## Apply pending Alembic migrations
	alembic upgrade head

downgrade:  ## Roll back the last Alembic migration
	alembic downgrade -1

migration:  ## Generate a new Alembic migration (usage: make migration MSG="description")
	alembic revision --autogenerate -m "$(MSG)"

# ─── Utilities ────────────────────────────────────────────────────────────────

install:  ## Install all dependencies (including dev)
	pip install -e ".[dev]"

shell:  ## Open a Python shell with app context
	python -c "from app.core.config import get_settings; s = get_settings(); print(s)"

clean:  ## Remove build artifacts and cache
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache .mypy_cache htmlcov .coverage dist build

help:  ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'
