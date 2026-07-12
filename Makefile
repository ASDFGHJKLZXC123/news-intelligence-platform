# Stage 1 developer command runner.
# Most targets assume dependencies from requirements-dev.txt are installed (see `make install`).

.PHONY: help install up down logs migrate revision seed test test-unit test-integration lint fmt audit compose-config db-backup db-restore db-retention-preview check

help: ## Show available commands
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-16s %s\n", $$1, $$2}'

install: ## Install runtime + dev dependencies
	python -m pip install -r requirements-dev.txt

up: ## Start API, worker, beat, Postgres (pgvector), and Redis via Docker Compose
	docker compose up -d --build

down: ## Stop and remove the Docker Compose stack
	docker compose down

logs: ## Tail Docker Compose logs
	docker compose logs -f

migrate: ## Apply database migrations to the latest revision
	alembic upgrade head

revision: ## Create a new migration: make revision m="message"
	alembic revision -m "$(m)"

seed: ## Run the database seed script
	python -m db.seed.seed

test: ## Run the full test suite (integration auto-skips unless REQUIRE_POSTGRES=1)
	pytest

test-unit: ## Run unit tests only (no Postgres/Redis required)
	pytest -m 'not integration'

test-integration: ## Run integration smoke tests; fails (not skips) if Postgres is unreachable
	REQUIRE_POSTGRES=1 pytest -m integration

lint: ## Lint with ruff
	ruff check .

fmt: ## Auto-format with ruff
	ruff format .

audit: ## Scan dependencies (runtime + dev) for known vulnerabilities
	pip-audit -r requirements-dev.txt

compose-config: ## Validate the Docker Compose configuration
	docker compose config

db-backup: ## Create a logical PostgreSQL backup using DATABASE_URL
	./scripts/backup-postgres.sh

db-restore: ## Restore a backup: make db-restore file=backups/postgres/example.dump
	./scripts/restore-postgres.sh "$(file)"

db-retention-preview: ## Preview rows eligible for retention cleanup
	psql "$$DATABASE_URL" -f infra/sql/retention-preview.sql

check: ## CI gate: compose validation, lint, unit + mandatory integration smoke, and audit
	$(MAKE) compose-config
	$(MAKE) lint
	$(MAKE) test-unit
	$(MAKE) test-integration
	$(MAKE) audit
