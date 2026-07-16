# V-Trade

V-Trade is an auditable, provider-neutral reproduction of PredictionArena's publicly
documented behavior. The frozen baseline is `predictionarena-polymarket-v1`; unresolved
owner decisions remain visibly `owner_pending` and prevent a scored run from starting.

## Local validation

Python 3.12 is required. Install the development dependencies, then run:

```powershell
python -m pytest
python -m ruff check src tests
python -m mypy src/vtrade
```

The standard-library test suite can also run before dependencies are installed:

```powershell
$env:PYTHONPATH='src'; python -m unittest discover -s tests -v
```

Copy `.env.example` only after the Supabase resources and external API credentials are
available. No provider is silently substituted when a configured resource is missing.

Phase-0 evidence and decisions are under `spec/`; the initial PostgreSQL migration is
under `migrations/`.
