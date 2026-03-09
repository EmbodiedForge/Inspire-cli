# Claude Development Notes (Private)

This file is gitignored - for internal development notes only.
Layered: subdirectory CLAUDE.md files provide context-specific knowledge.

## Project Overview

Python CLI for the Inspire HPC training platform. Manages training jobs, interactive notebooks, code sync, and SSH tunnels.

- **Python 3.10+** / **Click** / **uv**
- **Entry point:** `inspire = "inspire.cli.main:cli"`

## Development Workflow

```bash
uv sync                              # Install dependencies
uv run pre-commit install            # Set up hooks (required for new clones)
uv run inspire --help                # Run CLI
uv run pytest                        # Run tests
uv run pytest -m integration         # Integration tests only
uv run black inspire tests           # Format
uv run ruff check inspire tests      # Lint
uv run pre-commit run --all-files    # All pre-commit hooks
```

## CI/CD (Codeberg Forgejo Actions)

Workflows in `.forgejo/workflows/`:
- `ci.yml` — Push to `main` / PRs → lint (ruff + black) + test (pytest)
- `release.yml` — `v*` tag → tests + version consistency check
- `deps-check.yml` — Weekly (Mon 09:00 UTC) → outdated packages

## Release Process

Uses [commitizen](https://commitizen-tools.github.io/commitizen/). Version tracked in three places (kept in sync by `cz bump`):
- `pyproject.toml` → `project.version`
- `pyproject.toml` → `tool.commitizen.version`
- `inspire/__init__.py` → `__version__`

```bash
uv run cz bump --patch   # or --minor / --major
git push origin main --tags
```

## Architecture Decisions

**Execution via SSH tunnel:** All remote operations (`bridge exec`, `sync`, etc.) require an active SSH tunnel to a notebook.

**Config loading precedence:** Defaults → Global (`~/.config/inspire/config.toml`) → Project (`./.inspire/config.toml`) → Environment variables (highest).

**Output formatting:** Human-readable (colored, default) and JSON (`--json` flag). Formatters in `inspire/cli/formatters/`.

**`[remote_env]` config section:** Injects env vars into remote commands (`bridge exec`, `bridge ssh`, `job create`, `run`).

## Git Workflow

Two remotes with separate, unrelated histories:

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

### Bidirectional Workflow

```bash
# STEP 1: Check for external contributions
git fetch github-public
git log HEAD..github-public/main --oneline

# STEP 2: If external commits exist, merge first
git merge github-public/main -m "Merge external contributions"
git push origin main

# STEP 3: When ready to publish to github-public
git fetch github-public
git checkout -b public-sync github-public/main
git rm -r --quiet .          # Wipe ALL tracked files (removes stale leftovers)
git checkout main -- .        # Copy ALL files from main
# Remove private-only files
git rm -r --cached .claude/skills/check-ci .claude/skills/playwright-cli CLAUDE.md docs/ 2>/dev/null
rm -rf .claude/skills/check-ci .claude/skills/playwright-cli CLAUDE.md docs/
git add -A
git commit -m "v1.x - Description"
git push github-public public-sync:main
git checkout main && git branch -D public-sync
```

**Why file copy instead of merge?** Histories are forever unrelated (orphan commit on github-public). `git merge --squash` causes conflicts every time. The `git rm -r .` step is critical — without it, files deleted/renamed in main survive on github-public and shadow correct files.

### Lightweight Sync (small changes, no file renames/deletions)

When only a few files changed and no files were renamed or deleted, skip the full wipe-and-copy. Checkout just the changed files:

```bash
git fetch github-public
git checkout -b public-sync github-public/main
git checkout main -- path/to/file1 path/to/file2   # only changed files
git commit -m "fix: description"
git push github-public public-sync:main
git checkout main && git branch -D public-sync
```

**When to use:** Small patches, bug fixes, or changes to existing files only. **Do NOT use** when files were deleted, renamed, or when private-only files may leak — use the full sync for those.

### Security Rules

1. **Never** push regular commits to `github-public`
2. **Always** use file copy approach when publishing
3. **Always** pull external contributions first
4. **Check** for sensitive data before publishing
5. Files in `.gitignore` are safe (never tracked)

### Sensitive Files (gitignored)

`config.toml.example`, `scripts/`, `internal/`, `API_ENDPOINTS.md`, `inspire/Inspire_OpenAPI_Reference.md`, `**/CLAUDE.md`

### Private-Only Files (tracked on origin, excluded from github-public)

- `CLAUDE.md` — Project instructions (this file)
- `docs/` — Internal documentation (e.g. `FEEDBACK-CLAUDE-CODE.md`)
- `.claude/skills/check-ci/` — CI skill for Codeberg Forgejo Actions
- `.claude/skills/playwright-cli/` — Internal browser automation skill
