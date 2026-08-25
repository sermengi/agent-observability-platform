# agent-observability-platform
Production-oriented observability and evaluation platform for LLM agents

## Quick start

Prerequisites: Docker and `make`.

```bash
git clone <repository-url>
cd agent-observability-platform
cp .env.example .env
make up
```

Smoke check:

```bash
curl --fail http://localhost:8000/health
```

Expected response:

```json
{"status":"ok","checks":{"database":"ok"}}
```

## Local development

For host-native development, run Postgres through Compose and start the API with
reload enabled:

```bash
docker compose up -d --wait postgres
uv run uvicorn obs_platform.main:app --reload
```
