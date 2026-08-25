.PHONY: up

ifneq (,$(wildcard .env.example))
include .env.example
endif

ifneq (,$(wildcard .env))
include .env
endif

export

up:
	docker compose up -d --wait postgres
	uv run alembic upgrade head
	docker compose up -d
