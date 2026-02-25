---
name: setup
description: Use when a first-time user needs help setting up inspire-cli configuration, or when troubleshooting config issues. Guides through credentials, project discovery, workspace binding, and verification.
allowed-tools: Bash(inspire *), Bash(uv run inspire *), Bash(mkdir *), Bash(cat *), Read, Write, Edit
---

# Inspire CLI Setup

Guide users through configuration. Use `inspire init --help` and `inspire config --help` for flag/option reference.

## Setup Order

1. **Collect credentials** — base URL, username, password
2. **Set secrets as env vars** — never write passwords/tokens into TOML files
3. **Run `inspire init --discover`** — auto-discovers projects, workspaces, compute groups (needs playwright: `uv run playwright install chromium`)
4. **Create project config** — `./.inspire/config.toml` with context bindings and paths
5. **Set up bridge** — `inspire notebook ssh <id> --save-as bridge` for fast execution
6. **Verify** — `inspire config check` then `inspire resources list`

## Config File Placement

- **Global** (`~/.config/inspire/config.toml`): API settings, account catalogs (written by `--discover`)
- **Project** (`./.inspire/config.toml`): Project/workspace bindings, job defaults, target_dir
- `.inspire/` is gitignored by default

## Minimal Working Project Config

```toml
# .inspire/config.toml
[context]
account = "username"        # Binds to account catalog from --discover
project = "project-alias"   # Alias from discovered catalog

[workspaces]
gpu = "ws-..."              # For jobs/GPU notebooks
cpu = "ws-..."              # For bridge/sync (has internet)

[paths]
target_dir = "/shared/path/to/code"  # Required for sync, bridge exec, job logs

[job]
image = "pytorch25.06-py3:25.06"     # Default Docker image
```

## Common Mistakes

1. **Missing `target_dir`** — `sync`, `bridge exec`, `job logs` all fail without it
2. **Forgetting `[context].account`** — Without it, discovered catalogs aren't used
3. **Wrong workspace for bridge** — Use a CPU workspace (has internet), not GPU
4. **Secrets in TOML** — Use env vars (`INSPIRE_PASSWORD`) or account store from `--discover`
5. **No playwright for `--discover`** — Run `uv run playwright install chromium` first
6. **Placeholder URLs** — `config check` catches `example.com` but not all placeholders

## Verification Sequence

```bash
inspire config check          # Validates credentials + API auth
inspire config show           # Shows merged config with source of each value
inspire resources list        # Confirms API access
inspire project list          # Confirms project access
```
