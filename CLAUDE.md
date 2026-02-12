# Claude Development Notes (Private)

This file is gitignored - for internal development notes only.

## Project Overview

**inspire-cli** is a Python CLI for the Inspire HPC training platform. It manages training jobs, interactive notebooks, code sync, and SSH tunnels to remote compute resources.

- **Language:** Python 3.10+
- **CLI Framework:** Click (>=8.0.0)
- **Package Manager:** uv
- **Entry Point:** `inspire = "inspire.cli.main:cli"`

## Project Structure

```
inspire/
├── cli/                    # CLI commands and formatters
│   ├── commands/           # Command implementations
│   │   ├── job/           # Job management (create, status, logs, list, stop, wait)
│   │   ├── notebook/      # Notebook management (list, create, start, stop, ssh)
│   │   ├── image/         # Image management (list, detail, register, save, delete, set-default)
│   │   ├── resources/     # GPU/node availability
│   │   ├── bridge/        # Remote execution (exec_cmd.py, ssh_cmd.py)
│   │   ├── tunnel/        # SSH tunnel management (add, remove, status, list, etc.)
│   │   ├── config/        # Config show/check/env (check.py, show.py, env_cmd.py)
│   │   ├── init/          # Init command (discover.py, templates.py, env_detect.py, toml_helpers.py)
│   │   ├── run.py         # Quick job submission
│   │   └── sync.py        # Code sync to bridge
│   └── formatters/        # Human/JSON output (human_formatter.py, json_formatter.py)
├── config/                 # Configuration models, TOML/env loading
│   └── options/           # Config option definitions (api.py, forge.py, infra.py, project.py)
├── platform/
│   ├── openapi/           # Token-based API client + resource selection (resources.py)
│   └── web/               # Web session (SSO) + browser-only endpoints
│       └── browser_api/   # notebooks.py, images.py, playwright_notebooks.py, rtunnel.py
├── bridge/
│   ├── forge/             # Gitea/GitHub workflow interactions
│   └── tunnel/            # SSH tunnel (ssh.py, ssh_exec.py, rtunnel, models, config)
```

## Main Commands

| Command | Purpose |
|---------|---------|
| `job create/status/logs/list/stop/wait` | Manage training jobs |
| `notebook list/create/start/stop/ssh` | Manage interactive notebooks |
| `image list/detail/register/save/delete/set-default` | Manage Docker images |
| `resources list/nodes` | View GPU availability |
| `run` | Quick job submission with auto-resource selection |
| `sync` | Push code to Bridge runner |
| `bridge exec/ssh` | Execute commands on Bridge |
| `tunnel add/remove/status/list/ssh-config` | SSH tunnel management |
| `config show/check/env` | Configuration management |
| `init` | Initialize configuration |

## Development Workflow

```bash
# Install dependencies
uv sync

# Set up pre-commit hooks (required for new clones)
uv run pre-commit install

# Run CLI
uv run inspire --help

# Run tests
uv run pytest
uv run pytest -m integration  # Integration tests only

# Format/lint
uv run black inspire tests
uv run ruff check inspire tests

# Run pre-commit hooks manually on all files
uv run pre-commit run --all-files
```

## CI/CD (Codeberg Forgejo Actions)

Workflows live in `.forgejo/workflows/`:

| Workflow | Trigger | Jobs |
|----------|---------|------|
| `ci.yml` | Push to `main`, PRs | `lint` (ruff + black --check), `test` (pytest) |
| `release.yml` | `v*` tag push | Tests + version consistency check across pyproject.toml, `__init__.py`, and git tag |
| `deps-check.yml` | Weekly (Mon 09:00 UTC) | Checks for outdated packages and stale lockfile |

**Note:** Codeberg repo must have Actions enabled in Settings > Repository > Actions.

## Release Process

Uses [commitizen](https://commitizen-tools.github.io/commitizen/) for version management. Config in `[tool.commitizen]` in `pyproject.toml`.

```bash
# 1. Bump version (updates pyproject.toml, __init__.py, CHANGELOG.md, creates git tag)
uv run cz bump --patch   # or --minor / --major

# 2. Push to Codeberg (triggers CI + release validation)
git push origin main --tags

# 3. When ready, sync to GitHub public (see Bidirectional Workflow below)
```

Version is tracked in three places (kept in sync by commitizen):
- `pyproject.toml` → `project.version`
- `pyproject.toml` → `tool.commitizen.version`
- `inspire/__init__.py` → `__version__`

## Architecture Patterns

**Dual Execution Paths:**
- SSH tunnel (fast, when available) - for `bridge exec`, `sync`
- Gitea/GitHub Actions (fallback) - slower but always available

**Output Formatting:**
- Human-readable (colored console) - default
- JSON (`--json` flag) - machine-readable

**Config Loading Precedence:**
1. Defaults
2. Global: `~/.config/inspire/config.toml`
3. Project: `./.inspire/config.toml`
4. Environment variables (highest)

**rtunnel Browser Automation (`rtunnel.py`):**
- `_setup_notebook_rtunnel_sync()` now tries direct Jupyter terminal delivery first: create a terminal via `POST .../api/terminals`, then send setup script over terminal WebSocket (`.../terminals/websocket/<name>`). If WS send fails, it falls back to the Playwright UI terminal flow.
- `_open_or_create_terminal()` remains as fallback, using REST API + DOM recovery (tab/card/File menu) for terminal acquisition when direct WS path is unavailable.
- `open_notebook_lab()` uses a two-phase navigation strategy: short `/ide?notebook_id=...` probe, then early fallback to direct `/api/v1/notebook/lab/<id>/` to avoid long iframe waits after cold starts.
- `wait_marker` stays as a fixed 3s guard because xterm.js renders to `<canvas>` (DOM text locators are not reliable); real readiness remains enforced by `_ensure_proxy_readiness_with_fallback()` and SSH preflight.
- `request_json()` session-expiry retry refreshes `WebSession` in place, reducing repeated stale-session retries in one command flow.
- Set `INSPIRE_RTUNNEL_TIMING=1` to enable per-step timing output to stderr.

## Git Workflow

We maintain two remotes with separate histories:

```
origin (Codeberg)          github-public (GitHub)
─────────────────          ────────────────────────
PRIVATE                    PUBLIC
Full dev history           Clean squashed history
May have sensitive         No sensitive data
intermediate commits       Each commit = squashed release
```

### Remotes

- `origin` → `https://codeberg.org/cyteena/inspire-cli.git` (private)
- `github-public` → `https://github.com/EmbodiedForge/Inspire-cli.git` (public)

### Daily Development

```bash
# Normal development on origin
git add .
git commit -m "your message"
git push origin main
```

### Bidirectional Workflow

```bash
# STEP 1: Check for external contributions on github-public
git fetch github-public
git log HEAD..github-public/main --oneline  # Shows commits we don't have

# STEP 2: If there are external commits, merge them first
git merge github-public/main -m "Merge external contributions"
git push origin main

# STEP 3: Continue development...
git commit -m "new feature"
git push origin main

# STEP 4: When ready to publish, copy files to github-public
git fetch github-public
git checkout -b public-sync github-public/main
git checkout main -- .        # Copy ALL files from main (overwrites everything)
# Remove private-only files (not in .gitignore but should stay private)
git rm -r --cached .claude/skills/check-ci .claude/skills/playwright-cli .claude/plans 2>/dev/null
rm -rf .claude/skills/check-ci .claude/skills/playwright-cli .claude/plans
git add -A
git commit -m "v1.x - Description"
git push github-public public-sync:main
git checkout main && git branch -D public-sync
```

### Sync Direction Summary

| Direction | Method | Why |
|-----------|--------|-----|
| github-public → origin | Regular merge | Keep external contributions with their history |
| origin → github-public | File copy (`git checkout main -- .`) | Replace all files, no merge conflicts |

### Why file copy instead of `git merge --squash`?

The orphan commit on github-public means histories are **forever unrelated**.
`git merge --squash` causes conflicts every time. File copy avoids this:

```bash
git checkout main -- .   # Replaces ALL tracked files with main's version
git add -A               # Stage everything (including deletions)
git commit               # Clean commit, no merge conflicts
```

### Security Rules

1. **Never** push regular commits to `github-public` (exposes history)
2. **Always** use file copy approach (`git checkout main -- .`) when pushing to `github-public`
3. **Always** pull external contributions first before publishing
4. **Check** for sensitive data before publishing: `git ls-files | xargs grep -l "pattern"`
5. Files in `.gitignore` are safe (never tracked)

### Sensitive Files (gitignored)

- `config.toml.example` - Contains platform-specific URLs
- `scripts/` - Development scripts with hardcoded values
- `internal/` - Internal documentation
- `API_ENDPOINTS.md` - API documentation with internal URLs
- `inspire/Inspire_OpenAPI_Reference.md` - OpenAPI spec
- `CLAUDE.md` - This file

### Private-Only Files (tracked on origin, excluded from github-public)

These files are tracked in the private repo but must be removed during
the `github-public` sync step (see STEP 4 above):

- `.claude/skills/check-ci/` - CI skill tied to Codeberg Forgejo Actions
- `.claude/skills/playwright-cli/` - Internal browser automation skill
- `.claude/plans/` - Development plans

## Remote Environment Variables

The `[remote_env]` config section injects env vars into remote commands:

```toml
[remote_env]
UV_PYTHON_INSTALL_DIR = "/path/to/uv"
WANDB_API_KEY = "your-key"
```

Affects: `bridge exec`, `bridge ssh`, `job create`, `run`
