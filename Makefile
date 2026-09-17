# CLAUDE.md §18. `make check` is the gate: if it is red, the work is not done.

SHELL := /bin/bash
IMAGE ?= tele-scraper
TAG   ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)

.PHONY: help install run once dry-run check-config test-notify telegram-chats check test cov fmt lint types image manifests clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Sync the locked environment
	uv sync --frozen --all-groups

run: ## Run the long-running scheduler locally (dry run by default)
	DRY_RUN=true uv run python -m tele_scraper

once: ## Run a single cycle and exit with that cycle's exit code
	uv run python -m tele_scraper --once

dry-run: ## Scrape and evaluate, log intended notifications, send nothing
	uv run python -m tele_scraper --once --dry-run

check-config: ## Validate settings and the routing table, then exit
	uv run python -m tele_scraper --check-config

test-notify: ## Send a test message to every configured recipient (needs NOTIFY_ENABLED=true)
	uv run python -m tele_scraper --test-notify

telegram-chats: ## List Telegram chat ids that have messaged the bot
	uv run python -m tele_scraper --telegram-chats

lint: ## ruff check + format check
	uv run ruff check .
	uv run ruff format --check .

types: ## mypy --strict
	uv run mypy

test: ## Run the test suite (live tests excluded)
	uv run pytest

cov: ## Test suite with the coverage gate
	uv run pytest --cov --cov-branch --cov-report=term-missing

check: lint types cov ## The gate: lint + types + tests + coverage

fmt: ## Format the codebase
	uv run ruff format .
	uv run ruff check --fix .

image: ## Build the container image, tagged with the git SHA
	docker build -t $(IMAGE):$(TAG) .

manifests: ## Render and validate the Kubernetes manifests
	kustomize build deploy/overlays/dev | kubeconform -strict -summary

clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage dist build
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
