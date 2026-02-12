# Repository Guidelines

## Project Structure & Module Organization
- `inspire/` is the main Python package.
- CLI entry point: `inspire/cli/main.py`; command groups in `inspire/cli/commands/`; formatters in `inspire/cli/formatters/`.
- CLI command groups use thin `__init__.py` files for Click group definitions and `add_command` registrations. Implementation lives in named submodules:
  - `bridge/`: `exec_cmd.py`, `ssh_cmd.py`
  - `tunnel/`: `add.py`, `remove.py`, `status.py`, `list_cmd.py`, `update.py`, `set_default.py`, `ssh_config.py`, `test_cmd.py`
  - `config/`: `check.py`, `show.py`, `env_cmd.py`
  - `init/`: `discover.py`, `templates.py`, `env_detect.py`, `toml_helpers.py`
  - `image/`: `image_commands.py` (list, detail, register, save, delete, set-default)
- Formatters: `human_formatter.py` (all human-readable output) and `json_formatter.py` (machine-readable).
- Domain packages (preferred for shared logic; used by CLI):
  - `inspire/config/` config models, TOML/env loading, and schema/options. Options are grouped in `options/api.py`, `options/forge.py`, `options/infra.py`, `options/project.py`.
  - `inspire/platform/openapi/` platform OpenAPI client + resource selection (`resources.py` is a flat module).
  - `inspire/platform/web/` web-session (SSO) + browser-only endpoints (`session/`, `browser_api/`).
  - `inspire/platform/web/browser_api/` notebook HTTP APIs (`notebooks.py`), image management APIs (`images.py`), Playwright automation (`playwright_notebooks.py`), and rtunnel setup/probe/verify (`rtunnel.py`).
  - `inspire/bridge/` bridge/tunnel/SSH integrations. Tunnel SSH helpers are in `tunnel/ssh.py` and `tunnel/ssh_exec.py` (flat modules).
- `tests/` contains pytest suites (for example, `tests/test_cli_commands.py` and `tests/test_cli_smoke.py`).
- `examples/` holds workflow YAMLs for Gitea Actions.
- `scripts/` contains exploration/automation utilities used during API and UI discovery (gitignored; internal-only by default).
- `docs/` and `README.md` document usage; `bin/inspire` is a repo-local wrapper.

## Build, Test, and Development Commands
- Prefer `uv` for all Python/CLI invocations (`uv run ...`, `uv tool run ...`); avoid system `python`/`pip`.
- `uv tool install -e .` installs the CLI in editable mode without activating a venv.
- `uv venv .venv && uv pip install -e .` creates a local venv and installs the package for development.
- `uv run inspire --help` validates the entry point (works without a global install).
- `uv run pytest` runs the unit test suite.
- `uv run pytest -m integration` runs integration tests that require live API access.
- `uv run ruff check .` and `uv tool run black .` run linting and formatting.
- `uv` may update `uv.lock` during runs; avoid committing it unless you intentionally changed dependencies.

## CI/CD and Release Process
- CI runs on Codeberg via Forgejo Actions (`.forgejo/workflows/`). Triggered on push to `main` and PRs.
- CI jobs: `lint` (ruff + black --check) and `test` (pytest) run in parallel.
- Release workflow triggers on `v*` tag push — runs tests and verifies version consistency across `pyproject.toml`, `inspire/__init__.py`, and the git tag.
- Dependencies are checked weekly (Monday 09:00 UTC) by the `deps-check` workflow.
- **Release process:**
  1. `uv run cz bump --patch` (or `--minor` / `--major`) — updates `pyproject.toml`, `inspire/__init__.py`, `CHANGELOG.md`, and creates a git tag.
  2. `git push origin main --tags` — triggers release validation CI.
  3. Sync to GitHub public using the existing file-copy process (see `CLAUDE.md`).
- New clones should run: `uv run pre-commit install` to set up formatting/lint hooks.
- Manual dependency update: `uv lock --upgrade`.

## Coding Style & Naming Conventions
- Python 3.10+ codebase; follow Black and Ruff defaults with a 100-character line length.
- Use `snake_case` for functions/variables, `CapWords` for classes, and `test_*.py` for test files.
- CLI command groups use thin `__init__.py` files (group definition + `add_command` registrations only); implementation code goes in named submodules.

## Testing Guidelines
- Tests live under `tests/` and use pytest.
- Integration tests are marked with `@pytest.mark.integration`; keep them isolated from unit tests and avoid requiring live credentials in unit runs.
- `tests/test_cli_smoke.py` covers basic `--help` output; update it when adding/removing top-level command groups.

## Commit & Pull Request Guidelines
- Recent commits use short, imperative, sentence-case subjects (for example, "Fix job logs --follow..."); follow this style and avoid verbose prefixes.
- PRs should include a concise summary, testing notes, and any configuration or environment changes; attach CLI output or screenshots when behavior changes are user-facing.

## Configuration & Security Tips
- Required environment variables include `INSPIRE_USERNAME`, `INSPIRE_PASSWORD`, and `INSPIRE_TARGET_DIR`; Gitea variables (`INSP_GITEA_REPO`, `INSP_GITEA_TOKEN`, `INSP_GITEA_SERVER`) are needed for sync/remote logs.
- Config files are loaded from `~/.config/inspire/config.toml` (global) and `./.inspire/config.toml` (project).
- Optional: `INSPIRE_SHM_SIZE` (or `job.shm_size` in config.toml) sets default shared memory (GiB) for job creation (`inspire job create`, `inspire run`) and notebook creation (`inspire notebook create`).
- Optional: `INSPIRE_BRIDGE_ACTION_TIMEOUT` (or `bridge.action_timeout` in config.toml) sets default timeout (seconds) for `inspire bridge exec`.
- Never commit credentials; prefer shell exports or local dotenv tooling. Use `inspire config check` to validate setup.

## Public Sync & Ignore Policy
- Treat `.gitignore` as the source of truth for non-public/internal artifacts.
- Paths currently treated as internal/ignored include `.inspire/`, `internal/`, `scripts/`, `API_ENDPOINTS.md`, `CLAUDE.md`, `config.toml.example`, and `docs/rtunnel-ssh-setup.md`.
- When preparing `github-public` sync, avoid introducing dependencies on ignored paths in docs, examples, tests, or command instructions.

## Current Debug Status (rtunnel/browser automation)
- `notebook ssh` setup now uses a direct Jupyter terminal path first: create terminal via `POST .../api/terminals`, then send setup script via terminal WebSocket (`.../terminals/websocket/<name>`). If WS delivery fails, code falls back to the Playwright UI terminal path.
- `open_lab` no longer waits the full 60s iframe loop on restart-heavy cases. `open_notebook_lab()` now probes `/ide` briefly, then fails over early to direct `/api/v1/notebook/lab/<id>/`.
- Session expiry handling now refreshes in place (`request_json()` updates the same `WebSession` object after re-auth), which avoids repeated stale-session retries in a single command flow.
- HTTP proxy readiness checks can still return non-ready responses (often `404` or `ECONNREFUSED`) even when SSH tunnel connectivity later succeeds. HTTP probe remains advisory; SSH preflight (`inspire tunnel test` semantics) is authoritative.
- Typical current pipeline profile:
  - Dominant: platform notebook start/allocate time (`inspire notebook start --wait`, often ~30–40s).
  - CLI setup after notebook is running: now often ~5–6s end-to-end when WS path succeeds.
  - Fixed guard delay: `wait_marker` remains 3s (xterm/canvas limitation).
- Set `INSPIRE_RTUNNEL_TIMING=1` to enable per-step timing instrumentation in `_setup_notebook_rtunnel_sync()` (summary table to stderr).
- Keep tracked tests/docs free of credentials, tokens, and private endpoint values.
