# Developer command runner.
# Most targets assume dependencies from requirements-dev.txt are installed (see `make install`).

.PHONY: help install up down logs migrate revision seed benchmark-pipeline benchmark-stage-kernels degraded-path-drills production-config-check test test-unit test-integration test-integration-fresh lint fmt audit compose-config db-backup db-restore db-restore-drill db-retention-preview check

# Prefer the repository virtual environment when it exists while still allowing every
# command to be overridden by callers and CI.
VENV ?= .venv
PYTHON ?= $(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,python3)
PYTEST ?= $(PYTHON) -m pytest
RUFF ?= $(PYTHON) -m ruff
ALEMBIC ?= $(PYTHON) -m alembic
PIP_AUDIT ?= $(PYTHON) -m pip_audit
BENCHMARK_ITERATIONS ?= 1000
BENCHMARK_WARMUP ?= 100
STAGE_BENCHMARK_ITERATIONS ?= 10
STAGE_BENCHMARK_WARMUP ?= 2

# --- Disposable integration database -------------------------------------------------
# `test-integration-fresh` runs the integration suite against a throwaway Postgres that is
# created and destroyed by that target alone. It uses a unique Compose project name and lets
# Docker select a free host port by default, so parallel runs cannot tear down or bind over one
# another. Set either value explicitly only when a stable name or port is useful for debugging.
EPHEMERAL_PROJECT ?=
EPHEMERAL_PORT ?= 0
EPHEMERAL_READY_ATTEMPTS ?= 60
EPHEMERAL_POSTGRES_IMAGE ?=
RESTORE_DRILL_PROJECT ?=
RESTORE_DRILL_PORT ?= 0
RESTORE_DRILL_READY_ATTEMPTS ?= 60
RESTORE_DRILL_POSTGRES_IMAGE ?=

help: ## Show available commands
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-24s %s\n", $$1, $$2}'

install: ## Install runtime + dev dependencies
	$(PYTHON) -m pip install -r requirements-dev.txt

up: ## Start API, worker, beat, Postgres (pgvector), and Redis via Docker Compose
	docker compose up -d --build

down: ## Stop and remove the Docker Compose stack
	docker compose down

logs: ## Tail Docker Compose logs
	docker compose logs -f

migrate: ## Apply database migrations to the latest revision
	$(ALEMBIC) upgrade head

revision: ## Create a new migration: make revision m="message"
	$(ALEMBIC) revision -m "$(m)"

seed: ## Run the database seed script
	$(PYTHON) -m db.seed.seed

benchmark-pipeline: ## Benchmark the deterministic, no-network pipeline coordinator
	@$(PYTHON) scripts/benchmark-pipeline.py \
		--iterations "$(BENCHMARK_ITERATIONS)" \
		--warmup "$(BENCHMARK_WARMUP)"

benchmark-stage-kernels: ## Benchmark fixed production service kernels and gate work amplification
	@$(PYTHON) scripts/benchmark-stage-kernels.py \
		--iterations "$(STAGE_BENCHMARK_ITERATIONS)" \
		--warmup "$(STAGE_BENCHMARK_WARMUP)"

production-config-check: ## Fail closed on unsafe production configuration without external I/O
	@$(PYTHON) scripts/check-production-config.py --pretty

degraded-path-drills: ## Exercise bounded dependency-loss, lease-recovery, and shutdown paths
	@$(PYTHON) scripts/run-degraded-path-drills.py --pretty

test: ## Run the full test suite (integration auto-skips unless REQUIRE_POSTGRES=1)
	$(PYTEST)

test-unit: ## Run unit tests only (no Postgres/Redis required)
	$(PYTEST) -m 'not integration'

test-integration: ## Run integration smoke tests; fails (not skips) if Postgres is unreachable
	REQUIRE_POSTGRES=1 $(PYTEST) -m integration

test-integration-fresh: ## Run integration tests against a disposable Postgres, then destroy it
	@EPHEMERAL_PROJECT="$(EPHEMERAL_PROJECT)" \
		EPHEMERAL_PORT="$(EPHEMERAL_PORT)" \
		EPHEMERAL_READY_ATTEMPTS="$(EPHEMERAL_READY_ATTEMPTS)" \
		EPHEMERAL_POSTGRES_IMAGE="$(EPHEMERAL_POSTGRES_IMAGE)" \
		PYTHON="$(PYTHON)" \
		./scripts/test-integration-fresh.sh

lint: ## Lint with ruff
	$(RUFF) check .

fmt: ## Auto-format with ruff
	$(RUFF) format .

audit: ## Scan dependencies (runtime + dev) for known vulnerabilities
	$(PIP_AUDIT) -r requirements-dev.txt

compose-config: ## Validate the Docker Compose configuration
	@docker compose config --quiet

db-backup: ## Create a logical PostgreSQL backup using DATABASE_URL
	./scripts/backup-postgres.sh

db-restore: ## Restore only to an explicit disposable DB: make db-restore file=... disposable=1 database=name
	RESTORE_TARGET_DISPOSABLE="$(disposable)" \
		RESTORE_EXPECTED_DATABASE="$(database)" \
		./scripts/restore-postgres.sh "$(file)"

db-restore-drill: ## Backup and restore sentinel data between two disposable, version-matched databases
	@RESTORE_DRILL_PROJECT="$(RESTORE_DRILL_PROJECT)" \
		RESTORE_DRILL_PORT="$(RESTORE_DRILL_PORT)" \
		RESTORE_DRILL_READY_ATTEMPTS="$(RESTORE_DRILL_READY_ATTEMPTS)" \
		RESTORE_DRILL_POSTGRES_IMAGE="$(RESTORE_DRILL_POSTGRES_IMAGE)" \
		PYTHON="$(PYTHON)" \
		./scripts/test-postgres-restore.sh

db-retention-preview: ## Preview rows eligible for retention cleanup
	psql "$$DATABASE_URL" -f infra/sql/retention-preview.sql

check: ## Self-contained CI gate: compose, lint, unit, fresh-database integration, and audit
	$(MAKE) compose-config
	$(MAKE) lint
	$(MAKE) test-unit
	$(MAKE) benchmark-stage-kernels
	$(MAKE) test-integration-fresh
	$(MAKE) audit
