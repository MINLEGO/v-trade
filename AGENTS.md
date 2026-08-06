# Repository Guidelines

## Project Structure & Module Organization

- Python source is under `src/vtrade/`; domain types and ports are in `src/vtrade/domain/`. Runtime, broker, portfolio, provider, API, and admin modules live beside them.
- `tests/` contains the unit and contract suite. `tests/test_postgres_*.py` are opt-in, rollback-only integration tests.
- `migrations/` holds versioned PostgreSQL SQL. Experiment definitions are in `config/experiments/`; agent prompts, tool schemas, compatibility matrices, and fixtures are in `spec/`.
- `docs/` contains operations and research evidence, `scripts/` contains probes and analysis utilities, and `admin-dashboard/` is the separate React/TypeScript/Vite frontend. Dashboard static assets are in `admin-dashboard/public/`.

## Build, Test, and Development Commands

From the repository root, use Python 3.12 and the locked development environment:

```powershell
uv sync --extra dev
uv run --extra dev python -m pytest
uv run --extra dev python -m ruff check src tests
uv run --extra dev python -m mypy src/vtrade
```

For the dashboard, run `npm ci`, then `npm run dev`, `npm run lint`, or `npm run build` from `admin-dashboard/`. PostgreSQL integration tests require a real configured database and `VTRADE_RUN_POSTGRES_INTEGRATION=1`; keep them separate from offline validation.

## Coding Style & Naming Conventions

Python uses four-space indentation, a 100-character Ruff line limit, strict mypy, and Ruff rules `E`, `F`, `I`, `UP`, `B`, `SIM`, and `RUF`. Use `snake_case` for modules/functions, `PascalCase` for classes and React components, `useX` for React hooks, and `UPPER_SNAKE_CASE` for constants. Keep financial values, audit records, idempotency, and fail-closed behavior intact. Avoid files longer than 1000 lines ; 300-500 lines is ideal. 

Tool schemas, experiment definitions, migrations, tests, and documentation are coupled contracts. When changing `spec/tool-schemas-v1.json`, refresh the `tool_schemas.sha256` values in both experiment JSON files and update the corresponding tests and compatibility documentation.

## Testing Guidelines

Name Python tests `test_<behavior>` and place regressions beside the affected module. Offline tests must not contact model or research providers. There is no enforced coverage threshold; use `pytest-cov` for exploratory coverage when useful.

## Commit & Pull Request Guidelines

Recent commits use short, lowercase, behavior-focused subjects such as `fixed get_portfolio`. Keep commits focused. Pull requests should explain behavior or contract changes, list validation commands, call out migrations/configuration, link an issue when available, and include screenshots for dashboard UI changes.

## Security & Configuration Tips

Never commit `.env` files or secrets. Use `.env.example`, keep admin credentials out of URLs and command history, and do not replace missing production resources with fake providers or local storage.

## Agent skills

### Issue tracker

Issues and specs for this repo live as GitHub issues; use the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Domain docs

This is a single-context repo; domain documentation lives at root `CONTEXT.md` and `docs/adr/`. See `docs/agents/domain.md`.
