# Thin wrapper around scripts/dev.py. Windows and anyone without make can run the same tasks directly:
#   uv run python scripts/dev.py <task>
PYTHON ?= python3
DEV = $(PYTHON) scripts/dev.py

.PHONY: help setup start demo test lint format reset doctor validate-flows evals fixtures export up down docker-reset

help:
	@$(DEV) --help

setup:
	$(DEV) setup

start demo:
	$(DEV) start

test:
	$(DEV) test

lint:
	$(DEV) lint

format:
	$(DEV) format

reset:
	$(DEV) reset

doctor:
	$(DEV) doctor

validate-flows:
	$(DEV) validate-flows

evals:
	$(DEV) evals

fixtures:
	$(DEV) fixtures

export:
	$(DEV) export

# Docker Compose: one-command demo, stop, and full reset (deletes demo data).
up:
	docker compose up --build

down:
	docker compose down

docker-reset:
	docker compose down --volumes
