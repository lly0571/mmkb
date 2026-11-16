# Repository Guidelines

## Project Structure & Module Organization

MMKB is a Python 3.12 multimodal knowledge base backed by PostgreSQL and pgvector. Application code lives in `mmkb/`: `api.py` exposes resource endpoints, `resources.py` handles resource operations, and `storage.py` and `retrieval.py` implement storage and search. Database models and sessions live in `mmkb/db/`; model clients live in `mmkb/providers/`. Alembic revisions belong in `migrations/versions/`. Tests live in `tests/`, and architecture, schema, and usage documentation live in `docs/`. Local assets default to ignored `data/assets/`.

## Build, Test, and Development Commands

Run commands from the repository root after `conda activate mmkb`.

- `python -m pip install -e '.[test]'`: install the package in editable mode with test dependencies.
- `alembic upgrade head`: apply migrations to the configured database; enable pgvector in `public` beforehand.
- `python -m mmkb.storage init`: explicitly initialize the configured storage profile; this does not create an S3 bucket.
- `uvicorn mmkb.api:create_app --factory --host 127.0.0.1 --port 8000`: start the API; interactive documentation is at `/docs`.
- `python -m pytest -q -ra`: run tests and report skipped integration checks.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` for functions and modules, `PascalCase` for classes, and uppercase constants. Follow existing type annotations and explicit configuration patterns. Keep provider, storage, and database concerns in their respective modules. No formatter or linter is currently configured.

## Testing Guidelines

Use `test_*.py` filenames and descriptive `test_*` methods/functions. Tests use pytest plus unittest-based model checks, with mocks and temporary storage for offline coverage. No coverage threshold is configured. Add regression coverage for changed behavior.

Integration tests use the `integration` marker. Retrieval/storage checks require `MMKB_TEST_DATABASE_URL`; resource tests require `TEST_DATABASE_URL` naming a disposable `mmkb_test_*` database. S3 checks require `MMKB_TEST_S3_ENDPOINT_URL` and `MMKB_TEST_S3_BUCKET`. Real model requests require `RUN_MODEL_INTEGRATION=1`. See the corresponding `docs/` guides before enabling external services.

## Commit & Pull Request Guidelines

Follow existing Conventional Commit subjects, such as `feat(storage): implement Local/S3 storage` or `docs: update schema guide`. Keep commits focused. PRs should describe behavior changes, relevant issues, validation commands/results, and configuration or migration impacts.

## Configuration & Schema Changes

Copy `.env.example` to `.env.local`; never commit credentials or local assets. Environment variables override local configuration. Keep vectors at 2048 dimensions. Generate and review new Alembic revisions for schema changes; preserve applied historical migrations and avoid startup `create_all()` calls.
