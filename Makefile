# Цели для разработки. Windows: запускать из Git Bash.
COMPOSE = docker compose -f deploy/docker-compose.yml --env-file deploy/.env

.PHONY: help env up down db migrate test lint openapi eval

help: ## Список целей
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-10s %s\n", $$1, $$2}'

deploy/.env:
	cp deploy/.env.example deploy/.env

env: deploy/.env ## Создать deploy/.env из примера

up: deploy/.env ## Поднять весь стек
	$(COMPOSE) up -d --build

down: ## Остановить стек
	$(COMPOSE) down

db: deploy/.env ## Поднять только базу
	$(COMPOSE) up -d db

migrate: deploy/.env ## Применить миграции в контейнере
	$(COMPOSE) build api
	$(COMPOSE) run --rm migrate

test: ## ruff и pytest
	uv run ruff check .
	uv run ruff format --check .
	uv run pytest

lint: ## Исправить и отформатировать
	uv run ruff check --fix .
	uv run ruff format .

openapi: ## Записать docs/openapi.json
	uv run orientir openapi

eval: ## Метрики по эталону
	@echo "ещё не реализовано (DQ-14)" >&2
	@exit 2
