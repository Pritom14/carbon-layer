# Contributing to Carbon Layer

Thanks for your interest in contributing.

## Setup

```bash
git clone https://github.com/Pritom14/carbon-layer.git
cd carbon-layer
pip install -e ".[dev]"
```

## Running tests

```bash
DATABASE_URL="sqlite:///test_carbon.db" pytest tests/ -v
```

## Linting

```bash
ruff check src/ tests/
```

## Adding a new scenario

1. Create a YAML file under `scenarios/<category>/your-scenario.yaml`
2. Follow the structure of existing scenarios (see `scenarios/disputes/dispute-spike.yaml`)
3. Define parameters, phases, validations, and findings
4. Run `carbon scenarios-list` to verify it appears
5. Add a compile test in `tests/test_webhook.py`

## Adding a new payment provider

1. Create a package under `src/carbon/adapters/<provider>/` with `__init__.py`, `client.py`, and `adapter.py`
2. Add the provider to `src/carbon/adapters/registry.py`
3. Add event mappings in `src/carbon/webhook/payloads.py`
4. Add signing logic in `src/carbon/webhook/sender.py`
5. Add config fields in `src/carbon/config.py`
6. Add CLI flags in `src/carbon/cli.py`
7. Update `README.md`

## Code style

- Python 3.9+
- Line length: 100 (configured in `pyproject.toml`)
- Use `ruff` for linting
- No emojis in code or CLI output

## Pull requests

- Ensure all tests pass before submitting
- Add tests for new features
- Update the README if adding user-facing changes
