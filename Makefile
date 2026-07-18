.PHONY: up down logs test lint build ingest init backup
up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f app

test:
	pytest -q

lint:
	ruff check .
	mypy faervell_npc

build:
	python -m build

init:
	faervell-npc init-db

ingest:
	faervell-npc ingest data/sources.yaml

backup:
	./scripts/backup.sh
