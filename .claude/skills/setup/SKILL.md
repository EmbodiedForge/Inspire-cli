---
name: setup
description: Use when a first-time user needs help setting up inspire-cli configuration, or when troubleshooting config issues. Guides through credentials, project discovery, workspace binding, and verification.
allowed-tools: Bash(inspire *), Bash(uv run inspire *), Bash(uv run playwright *), Bash(mkdir *), Bash(ls *), Read, Write, Edit
---

# Inspire CLI Setup

Help users configure inspire-cli by collecting info that can't be auto-detected.

## What's automated vs what needs user input

| Step | Automated by | User input needed |
|------|-------------|-------------------|
| Project/workspace discovery | `inspire init --discover` | Username, password, base URL |
| Workspace alias assignment | Smart defaults in `--discover` | Confirmation or override |
| target_dir | Catalog workdir as default | Confirmation or custom path |
| Project preference order | Nothing — must ask user | Ranked list of project names |
| SSH tools on internet machines | `notebook ssh` auto-installs | Nothing |
| SSH tools on GPU (no internet) | `notebook ssh` uses pre-placed binaries | Install dir on shared filesystem |
| Bridge profile | `notebook ssh <id> --save-as bridge` | Which notebook to use |

## Phase 1: Credentials

Ask user for:
- **Base URL** — the platform URL (e.g. `https://qz.sii.edu.cn`). Check if `INSPIRE_BASE_URL` env var or global config already has it.
- **Username** — numeric account ID or alphanumeric ID. Check `INSPIRE_USERNAME`.
- **Password** — recommend setting as `INSPIRE_PASSWORD` env var in shell profile. Never write passwords into committed files unless the user ask explicitly.

If credentials already exist (env vars or global config), skip to Phase 2.

## Phase 2: Discovery

Run from the user's **project working directory** (where `.inspire/` will live):
```
inspire init --discover -u <username> --force --target-dir <path>
```

`--discover` auto-handles: login via playwright, project enumeration, workspace alias assignment (cpu/gpu/internet), workdir lookup, compute group discovery, config file generation.

Needs playwright: if missing, run `uv run playwright install chromium` first.

**What to ask the user:**
- **target_dir** — "Where is your code on the shared filesystem?" The `--discover` suggests the catalog workdir but user may want a subdirectory. This is the most important config value — sync, bridge exec, and job logs all depend on it.

**After discovery, verify:**
```
inspire config show    # check all values resolved
inspire config check   # validates API auth
```

## Phase 2b: Project preference order

After `--discover`, the project catalog is written. Now ask the user to rank their projects by preference. This controls auto-selection when submitting jobs — most preferred project is tried first, falling back through the list if over quota.

**What to ask the user:**
- Show the discovered project list (from the catalog in global config)
- "Which projects do you use most? Rank them by preference (most preferred first)."
- User provides ordered list of project names

**Write to `.inspire/config.toml`:**
```toml
[defaults]
project_order = ["preferred-project", "second-choice", "fallback"]
```

The values are project **names** (not IDs). The auto-selection sort uses this as the primary ranking after `gpu_unlimited` status. Projects not listed sort after all listed ones.

## Phase 3: SSH tools bootstrap (only for GPU clusters without internet)

**Skip this phase if** the user only uses CPU/4090 notebooks (they have internet, `notebook ssh` auto-installs everything).

**When needed:** H100/H200 clusters have no internet. `notebook ssh` still auto-installs, but it needs pre-placed binaries (rtunnel, dropbear) on the shared filesystem.

**Key concept:** After `--discover`, the global config catalog has `shared_path_group` per project (e.g. `/inspire/hdd/global_user/<username>`). This path is visible from ALL notebooks across all projects. SSH tools must go here, not in a project-specific workdir.

**What to ask the user:**
1. **"Do you need SSH access to GPU notebooks (H100/H200)?"** — if no, skip this phase entirely
2. **"Where should SSH tools be installed?"** — suggest `<shared_path_group>/tools/` from the catalog. User may already have tools installed (check if paths exist). Let them provide a custom path.
3. **"Which project to use for the CPU notebook?"** — show project list from catalog. Any project works since tools go on the shared path. Prefer a project with low queue priority.

**The bootstrap itself:**
- Start a CPU notebook in the chosen project
- SSH into it (`inspire notebook ssh <id>`) — this auto-installs openssh + rtunnel on the CPU side
- From inside: download rtunnel binary + dropbear deb packages to the chosen install dir
- Set config: `INSPIRE_RTUNNEL_BIN`, `INSPIRE_DROPBEAR_DEB_DIR` (env vars or `[ssh]` in project config)
- `setup_script` is optional — the built-in bootstrap in `notebook ssh` already handles dpkg install + dropbear startup inline
- Stop the CPU notebook

**If tools already exist** (user says "I already have them" or paths exist on disk), just ask for the paths and set the config.

## Phase 4: Bridge setup

A bridge is a saved SSH profile to a running notebook for fast `bridge exec` / `sync`.

```
inspire notebook ssh <notebook-id> --save-as bridge
```

**What to ask:** "Which notebook should be your default bridge?" — typically a CPU notebook for code sync/execution.

## Troubleshooting checklist

If something isn't working, check in this order:
1. `inspire config show` — look for `[default]` or `[env]` source tags, placeholder values
2. `inspire config check` — validates auth, catches stale passwords
3. Missing `target_dir` — most common cause of sync/bridge failures
4. Wrong workspace — bridge/sync need CPU workspace (has internet), jobs need GPU
5. SSH paths not set — `INSPIRE_RTUNNEL_BIN` etc. needed for GPU notebook SSH
6. Stale session — re-run `inspire init --discover` to refresh
