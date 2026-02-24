# Repository Guidelines

## Project Structure & Module Organization
- `inspire/` is the main Python package.
- CLI entry point: `inspire/cli/main.py`; top-level command registration is in `inspire/cli/commands/__init__.py`.
- CLI command modules live in `inspire/cli/commands/`:
  - Group packages: `bridge/`, `config/`, `image/`, `init/`, `job/`, `notebook/`, `project/`, `resources/`, `tunnel/`
  - Top-level command modules: `run.py`, `sync.py`
- Command package conventions:
  - Most groups use thin `__init__.py` files (Click group + `add_command` calls), with implementation in submodules.
  - `init` is the exception: command implementation is in `init/init_cmd.py`; `init/__init__.py` is a stable import surface used by tests.
- Current command implementation map:
  - `bridge/`: `exec_cmd.py`, `ssh_cmd.py`, `scp_cmd.py`
  - `tunnel/`: `add.py`, `remove.py`, `status.py`, `list_cmd.py`, `update.py`, `set_default.py`, `ssh_config.py`, `test_cmd.py`
  - `config/`: `check.py`, `show.py`, `env_cmd.py`
  - `init/`: `init_cmd.py`, `discover.py`, `templates.py`, `env_detect.py`, `toml_helpers.py`, `errors.py`, `json_report.py`
  - `image/`: `image_commands.py` (list, detail, register, save, delete, set-default)
  - `job/`: `job_commands.py`, `job_create.py`, `job_logs.py`, `job_deps.py`
  - `notebook/`: `notebook_commands.py`, `notebook_create_flow.py`, `top.py`
  - `project/`: `project_commands.py`
  - `resources/`: `resources_list.py`, `resources_nodes.py`
- Formatters: `inspire/cli/formatters/human_formatter.py` (human-readable) and `json_formatter.py` (machine-readable).
- Domain packages (preferred for shared logic used by CLI):
  - `inspire/config/`: config models, TOML/env loading, schema/options, runtime helpers.
  - `inspire/config/options/`: option groups in `api.py`, `forge.py`, `infra.py`, `project.py`.
  - `inspire/platform/openapi/`: OpenAPI client/auth/jobs/nodes/resources.
  - `inspire/platform/web/`: web session (SSO) + browser-only APIs (`session/`, `browser_api/`, `resources.py`).
  - `inspire/platform/web/browser_api/`: split domain modules including `availability/`, `jobs.py`, `notebooks.py`, `images.py`, `projects.py`, `workspaces.py`, `playwright_notebooks.py`, `rtunnel.py`.
  - `inspire/bridge/tunnel/`: tunnel config/models + rtunnel/ssh/scp/sync helpers.
  - `inspire/bridge/forge/`: Forge/Gitea workflow, logs, artifacts, and API helpers.
- `tests/` contains pytest suites across CLI, bridge/tunnel, openapi, web session, and notebook flows (for example, `tests/test_cli_commands.py`, `tests/test_cli_smoke.py`).
- `examples/` holds workflow YAML examples for Gitea/Forgejo usage.
- `scripts/` contains internal exploration/automation utilities and is gitignored.
- `docs/` and `README.md` document usage; `bin/inspire` is a repo-local wrapper.

## Build, Test, and Development Commands
- Prefer `uv` for all Python/CLI invocations (`uv run ...`, `uv tool run ...`); avoid system `python`/`pip`.
- `uv tool install -e .` installs the CLI in editable mode without activating a venv.
- `uv venv .venv && uv pip install -e .` creates a local venv and installs the package for development.
- `uv run inspire --help` validates the entry point (works without a global install).
- `uv run pytest` runs the test suite.
- `uv run pytest -m integration` runs integration tests that require live API access.
- `uv run ruff check inspire tests` and `uv run black --check inspire tests` match CI lint/format checks.
- `uv tool run black .` formats the repo when needed.
- `uv` may update `uv.lock` during runs; avoid committing it unless dependencies were intentionally changed.

## CI/CD and Release Process
- CI runs on Codeberg Forgejo Actions (`.forgejo/workflows/`) on push/PR to `main`.
- CI jobs run in parallel:
  - `lint`: `uv run ruff check inspire tests` and `uv run black --check inspire tests`
  - `test`: `uv run pytest -x -q --tb=short`
- Release workflow triggers on `v*` tag push and validates version consistency across `pyproject.toml`, `inspire/__init__.py`, and the git tag.
- Dependency checks run weekly (Monday 09:00 UTC) via `deps-check`.
- Release process:
  1. `uv run cz bump --patch` (or `--minor` / `--major`) updates `pyproject.toml`, `inspire/__init__.py`, `CHANGELOG.md`, and creates a git tag.
  2. `git push origin main --tags` triggers release validation CI.
  3. Sync to GitHub public with the existing file-copy process (see `CLAUDE.md`).
- New clones should run `uv run pre-commit install` to install hooks.
- Manual dependency update: `uv lock --upgrade`.

## Coding Style & Naming Conventions
- Python 3.10+ codebase; follow Black and Ruff defaults with 100-character line length.
- Use `snake_case` for functions/variables, `CapWords` for classes, and `test_*.py` for test files.
- Keep Click group wiring and implementation separated; avoid putting business logic in command registration modules.

## Testing Guidelines
- Tests live under `tests/` and use pytest.
- Integration tests are marked with `@pytest.mark.integration`; keep them isolated from unit tests and avoid requiring live credentials in standard runs.
- `tests/test_cli_smoke.py` validates `--help` output and key command presence; update it when adding/removing top-level command groups in `inspire/cli/main.py`.

## Commit & Pull Request Guidelines
- Prefer concise, imperative commit subjects. Conventional-commit prefixes are acceptable when useful.
- PRs should include a short behavior summary, testing notes, and any config/environment changes; include CLI output snippets for user-visible behavior changes.

## Configuration & Security Tips
- Config is layered from:
  1. `~/.config/inspire/config.toml` (global)
  2. `./.inspire/config.toml` (project)
  3. Environment variables
- Typical required inputs for authenticated commands are `INSPIRE_USERNAME`, `INSPIRE_PASSWORD` (or `[accounts."<username>"].password`), and `INSPIRE_TARGET_DIR`.
- Gitea/Forge workflow sync and remote logs rely on `INSP_GITEA_REPO`, `INSP_GITEA_TOKEN`, and `INSP_GITEA_SERVER`.
- Optional: `INSPIRE_SHM_SIZE` (or `job.shm_size` in config) sets default shared memory (GiB) for job and notebook creation.
- Optional: `INSPIRE_BRIDGE_ACTION_TIMEOUT` (or `bridge.action_timeout` in config) sets default timeout (seconds) for `inspire bridge exec`.
- Optional: `INSPIRE_BROWSER_API_PREFIX` overrides the default browser API path prefix.
- Never commit credentials/tokens. Prefer local env exports or local config; run `inspire config check` to validate setup.

## Public Sync & Ignore Policy
- Treat `.gitignore` as the source of truth for non-public/internal artifacts.
- Paths currently ignored/internal include `.inspire/`, `internal/`, `scripts/`, `scripts/bridge-tunnel-setup.sh`, `scripts/inspire-tunnel`, `API_ENDPOINTS.md`, `CLAUDE.md`, `config.toml.example`, `inspire/Inspire_OpenAPI_Reference.md`, `docs/rtunnel-ssh-setup.md`, and `.playwright-cli/`.
- When preparing `github-public` sync, avoid introducing dependencies on ignored paths in docs, examples, tests, or command instructions.

## Current Debug Status (rtunnel/browser automation)
- `notebook ssh` setup prefers the direct Jupyter terminal API path first: create terminal via `POST .../api/terminals`, then send setup over terminal WebSocket (`.../terminals/websocket/<name>`). If WS delivery fails, fallback is Playwright UI terminal automation.
- `open_notebook_lab()` now probes `/ide` briefly (short frame probe window) and falls back early to direct `/api/v1/notebook/lab/<id>/` navigation.
- Session-expiry handling refreshes credentials in place: `request_json()` re-authenticates once and updates the same `WebSession` object.
- HTTP proxy readiness checks can still report transient failures (`404`, `ECONNREFUSED`) even when SSH succeeds. Treat HTTP probe as advisory; use SSH preflight (`inspire tunnel test`) as authoritative.
- rtunnel proxy state is cached per account under `~/.cache/inspire-cli/rtunnel-proxy-state*.json` with TTL-based reuse.
- Set `INSPIRE_RTUNNEL_TIMING=1` to enable per-step timing output in `_setup_notebook_rtunnel_sync()`.
- Keep tracked tests/docs free of credentials, tokens, and private endpoint values.
- `inspire init` probe controls (`--probe-limit`, `--probe-keep-notebooks`, `--probe-pubkey`/`--pubkey`, `--probe-timeout`) are only effective with `--discover --probe-shared-path`; otherwise they are accepted but ignored.
