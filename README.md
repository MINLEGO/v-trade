# V-Trade

V-Trade is an auditable, provider-neutral reproduction of PredictionArena's publicly
documented behavior. The frozen baseline `predictionarena-polymarket-v1` remains
deliberately `owner_pending`: runtime startup fails closed until the exact
`get_portfolio` pagination contract is supplied. Worst-case model and research request
prices are now frozen and enforced before provider calls.

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
available. OpenRouter may change provider routes for the same model according to the
frozen price-sorted routing policy; it must never substitute another configured model.

Phase-0 evidence and decisions are under `spec/`; versioned PostgreSQL migrations are
under `migrations/`. Apply them with `python -m vtrade.migrate` only after exporting the
real `VTRADE_DATABASE_URL`. No runtime path substitutes local storage or fake providers
for missing production resources.

Runtime scheduling, recovery, retention and private-admin deployment are documented in
`docs/runtime-operations.md`. Every admin route requires the runtime admin secret;
Swagger, ReDoc and OpenAPI endpoints are disabled.
