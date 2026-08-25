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
	docker compose build api
	docker compose run --rm api alembic upgrade head
	docker compose up -d
