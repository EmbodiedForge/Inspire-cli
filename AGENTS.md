# Repository Guidelines

## Project Structure & Module Organization
- `inspire/` is the main Python package. CLI entry point lives in `inspire/cli/main.py`, command groups in `inspire/cli/commands/`, shared helpers in `inspire/cli/utils/`, and output formatters in `inspire/cli/formatters/`.
- `inspire/inspire_api_control.py` is a legacy script; current CLI behavior is in `inspire/cli/` (see `inspire/README.md` for legacy notes).
- Command groups may be split across modules: `inspire/cli/commands/job.py`, `notebook.py`, `tunnel.py`, and `resources.py` are registries, with subcommands implemented in `<group>_*.py`.
- Large command/utility modules may be split behind façades to keep imports stable:
  - `inspire/cli/commands/config.py` is a registry; command bodies live in `config_show.py`, `config_env_template.py`, and `config_check.py`.
  - `inspire/cli/utils/tunnel.py` and `inspire/cli/utils/tunnel_ssh.py` re-export from `tunnel_*` and `tunnel_ssh_*` modules.
  - `inspire/cli/utils/web_session.py` re-exports from `web_session_*` modules.
  - `inspire/cli/utils/forge.py` re-exports from `forge_*` modules; similarly `inspire/cli/utils/browser_api_notebooks.py` and `browser_api_legacy.py` re-export from `browser_api_*` modules.
- `tests/` contains pytest suites (for example, `tests/test_cli_commands.py` and `tests/test_cli_smoke.py`).
- `examples/` holds workflow YAMLs for Gitea Actions.
- `scripts/` contains exploration/automation utilities used during API and UI discovery.
- `docs/` and `README.md` document usage; `bin/inspire` is a repo-local wrapper.

## Build, Test, and Development Commands
- `uv tool install -e .` installs the CLI in editable mode without activating a venv.
- `uv venv .venv && uv pip install -e .` creates a local venv and installs the package for development.
- `inspire --help` validates the entry point.
- `uv run pytest` runs the unit test suite.
- `uv run pytest -m integration` runs integration tests that require live API access.
- `uv run ruff check .` and `uv tool run black .` run linting and formatting.
- `uv` may update `uv.lock` during runs; avoid committing it unless you intentionally changed dependencies.

## Coding Style & Naming Conventions
- Python 3.10+ codebase; follow Black and Ruff defaults with a 100-character line length.
- Use `snake_case` for functions/variables, `CapWords` for classes, and `test_*.py` for test files.
- CLI command groups map to `inspire/cli/commands/<group>.py` (for example, `job.py` for `inspire job ...`); subcommands may live in `inspire/cli/commands/<group>_*.py`.

## Testing Guidelines
- Tests live under `tests/` and use pytest.
- Integration tests are marked with `@pytest.mark.integration`; keep them isolated from unit tests and avoid requiring live credentials in unit runs.
- `tests/test_cli_smoke.py` covers basic `--help` output; update it when adding/removing top-level command groups.

## Commit & Pull Request Guidelines
- Recent commits use short, imperative, sentence-case subjects (for example, "Fix job logs --follow..."); follow this style and avoid verbose prefixes.
- PRs should include a concise summary, testing notes, and any configuration or environment changes; attach CLI output or screenshots when behavior changes are user-facing.

## Configuration & Security Tips
- Required environment variables include `INSPIRE_USERNAME`, `INSPIRE_PASSWORD`, and `INSPIRE_TARGET_DIR`; Gitea variables (`INSP_GITEA_REPO`, `INSP_GITEA_TOKEN`, `INSP_GITEA_SERVER`) are needed for sync/remote logs.
- Optional: `INSPIRE_SHM_SIZE` (or `job.shm_size` in config.toml) sets default shared memory (GiB) for job creation (`inspire job create`, `inspire run`) and notebook creation (`inspire notebook create`).
- Optional: `INSPIRE_BRIDGE_ACTION_TIMEOUT` (or `bridge.action_timeout` in config.toml) sets default timeout (seconds) for `inspire bridge exec`.
- Never commit credentials; prefer shell exports or local dotenv tooling. Use `inspire config check` to validate setup.
