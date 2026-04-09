# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ADK is an LLM-powered, self-healing browser automation framework. It converts natural language task descriptions into Playwright actions, executes them via an MCP server, and auto-heals failures using LLM + screenshot analysis.

## Setup & Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env  # then set OPENROUTER_API_KEY

# Start Playwright MCP server (requires external infra_network)
docker-compose up -d

# Initialize database (creates tables and indexes)
python db/migrations.py
```

There are no test, lint, or build commands configured.

## Architecture

**Core workflow:** Natural Language → Interpret → Execute → (on failure) Heal → Retry

### Components

- **`tools/`** — Five ADK tool functions exposed via `tools/__init__.py`:
  - `interpret.py` — LLM converts natural language steps into structured Playwright action JSON
  - `executor.py` — Runs action sequences through Playwright MCP, screenshots after each step
  - `healer.py` — On step failure, sends screenshot + DOM context to LLM to produce a corrected action
  - `snapshot.py` — Uploads screenshots to MinIO, returns presigned URLs
  - `history.py` — Retrieves execution history with snapshot URLs

- **`db/`** — Async PostgreSQL layer (asyncpg):
  - `connection.py` — Singleton connection pool
  - `migrations.py` — DDL for all tables (tasks, step_sequences, executions, step_logs, revisions)
  - `queries.py` — All SQL queries as async functions

- **`mcp_client/playwright_client.py`** — AsyncContextManager wrapping Playwright MCP server communication over SSE

- **`storage/minio_client.py`** — MinIO upload and presigned URL generation (singleton client)

- **`config.py`** — Pydantic v2 Settings loading from environment variables

### Data Model

Tasks hold natural language steps. Step sequences are versioned action arrays (JSONB) linked to tasks. Executions track individual runs. Step logs record per-step results with screenshot keys. Revisions track healing events linking old → new sequences.

### External Services

- **PostgreSQL** — All metadata and execution history
- **MinIO** — Screenshot storage with presigned GET URLs
- **Playwright MCP Server** — Docker container (port 8931) exposing browser automation over SSE
- **OpenRouter API** — LLM inference (default model: openai/gpt-4o)

## Key Patterns

- Fully async (asyncio + asyncpg throughout)
- Singleton pools for DB and MinIO connections
- LLM calls use OpenAI SDK pointed at OpenRouter base URL
- Tools are designed for integration with Google's ADK agent framework
