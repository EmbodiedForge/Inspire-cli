# Repository Guidelines

## Project Structure & Module Organization
- `inspire/` is the main Python package. CLI entry point lives in `inspire/cli/main.py`, command groups in `inspire/cli/commands/`, shared helpers in `inspire/cli/utils/`, and output formatters in `inspire/cli/formatters/`.
- `inspire/inspire_api_control.py` is a legacy script; current CLI behavior is in `inspire/cli/` (see `inspire/README.md` for legacy notes).
- `tests/` contains pytest suites (for example, `tests/test_cli_commands.py`).
- `examples/` holds workflow YAMLs for Gitea Actions.
- `scripts/` contains exploration/automation utilities used during API and UI discovery.
- `docs/` and `README.md` document usage; `bin/inspire` is a repo-local wrapper.

## Build, Test, and Development Commands
- `uv tool install -e .` installs the CLI in editable mode without activating a venv.
- `uv venv .venv && uv pip install -e .` creates a local venv and installs the package for development.
- `inspire --help` validates the entry point.
- `pytest` runs the unit test suite.
- `pytest -m integration` runs integration tests that require live API access.
- `ruff check .` and `black .` run linting and formatting.

## Coding Style & Naming Conventions
- Python 3.10+ codebase; follow Black and Ruff defaults with a 100-character line length.
- Use `snake_case` for functions/variables, `CapWords` for classes, and `test_*.py` for test files.
- CLI commands map to modules in `inspire/cli/commands/` (for example, `job.py` for `inspire job ...`).

## Testing Guidelines
- Tests live under `tests/` and use pytest.
- Integration tests are marked with `@pytest.mark.integration`; keep them isolated from unit tests and avoid requiring live credentials in unit runs.

## Commit & Pull Request Guidelines
- Recent commits use short, imperative, sentence-case subjects (for example, "Fix job logs --follow..."); follow this style and avoid verbose prefixes.
- PRs should include a concise summary, testing notes, and any configuration or environment changes; attach CLI output or screenshots when behavior changes are user-facing.

## Configuration & Security Tips
- Required environment variables include `INSPIRE_USERNAME`, `INSPIRE_PASSWORD`, and `INSPIRE_TARGET_DIR`; Gitea variables (`INSP_GITEA_REPO`, `INSP_GITEA_TOKEN`, `INSP_GITEA_SERVER`) are needed for sync/remote logs.
- Never commit credentials; prefer shell exports or local dotenv tooling. Use `inspire config check` to validate setup.
