.PHONY: help
help: ## Ask for help!
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; \
		{printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'

.PHONY: setup
setup: ## Install the package and its dev dependencies
	uv sync --extra dev

.PHONY: format
format: ## Format code
	uv run ruff format .

.PHONY: check-format
check-format: ## Check code formatting
	uv run ruff format --check .

.PHONY: lint
lint: ## Run linter
	uv run ruff check .

.PHONY: lint-fix
lint-fix: ## Run linter with auto-fix
	uv run ruff check --fix .

.PHONY: typecheck
typecheck: ## Run type checker (mypy --strict)
	uv run mypy

.PHONY: test
test: ## Run tests
	uv run pytest

.PHONY: check
check: check-format lint typecheck test ## Run every check CI runs

.PHONY: build
build: ## Build the sdist and wheel
	uv build

.PHONY: clean
clean: ## Remove build artifacts
	rm -rf dist build .pytest_cache .mypy_cache .ruff_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
