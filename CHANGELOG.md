# Changelog

## Unreleased

### Breaking Changes

- Removed deprecated `inspire bridge exec --no-tunnel` flag. SSH tunnel is now the default execution path for command execution; workflow path is selected by artifact options.
- Removed deprecated `inspire sync --via-action` flag. Use `--transport workflow` explicitly when workflow transport is required.

## v0.2.4 (2025-01-01)

### Features

- Job management commands (create, status, logs, list, stop, wait)
- Notebook management commands (list, create, start, stop, ssh)
- Resource availability listing (GPUs, nodes)
- Quick job submission with auto-resource selection (`run`)
- Code sync to Bridge runner (`sync`)
- Bridge remote execution (`bridge exec`, `bridge ssh`)
- SSH tunnel management (add, remove, status, list, ssh-config)
- Configuration management (show, check, env) with TOML + env var loading
- Project initialization with environment detection
- Dual execution paths: SSH tunnel (fast) and Gitea/GitHub Actions (fallback)
- Human-readable and JSON output formatting
- Remote environment variable injection via `[remote_env]` config
