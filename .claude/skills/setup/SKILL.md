---
name: setup
description: Use when a first-time user needs help setting up inspire-cli configuration, or when troubleshooting config issues. Guides through credentials, project discovery, workspace binding, and verification.
allowed-tools: Bash(inspire *), Bash(uv run inspire *), Bash(uv run playwright *), Bash(mkdir *), Bash(ls *), Read, Write, Edit
---

# Inspire CLI Setup

Guide users through configuration by collecting info that can't be auto-detected.
Treat CLI help as source of truth (`inspire --help`, `inspire <group> --help`, `inspire <group> <command> --help`) — this doc can lag behind releases.

## What's automated vs what needs user input

| Step | Automated | Needs user input |
|------|-----------|-----------------|
| Project/workspace discovery | `inspire init --discover` | Username, password, base URL |
| Workspace aliases (cpu/gpu/internet) | Smart defaults in `--discover` | Confirmation or override |
| `target_dir` | Catalog workdir as suggestion | **Must verify via CPU notebook SSH** |
| Project preference order | Nothing | Ranked list of project names |
| CPU/4090 notebook SSH | Fully automatic | Nothing |
| GPU notebook SSH (no internet) | Automatic bootstrap with config | **Always** `rtunnel_bin`, plus `apt_mirror_url` or `dropbear_deb_dir` |
| Bridge profile | `inspire notebook ssh --save-as` | Which notebook to use |

## Setup flow

### Phase 1: Credentials

Ask for base URL, username, password. Check env vars and global config first — skip if already present. Recommend `INSPIRE_PASSWORD` env var in shell profile (never commit passwords).

### Phase 2: Discovery

Run `inspire init --discover` from the user's project working directory. Needs playwright (`uv run playwright install chromium` if missing).
If config files already exist, discovery prompts before rewrite; use `--force` for non-interactive runs.

After discovery: `inspire config show` and `inspire config check` to verify.

### Phase 2b: Project preference

Show discovered projects from global config catalog. Ask user to rank by preference. Write `project_order` (list of project **names**) to `[defaults]` in project config.

Sort order when no `--project` flag: `project_order` (first match wins) > `gpu_unlimited` (tiebreaker) > `priority` > name. Projects not in the list sort after all listed ones.

### Phase 2c: Verify paths via CPU notebook

**Always do this.** The catalog paths come from the API and can be wrong. CPU notebook SSH is guaranteed to work (zero setup needed) — use it as ground truth.

**Always use `--project` explicitly** when creating notebooks during setup. At this point `project_order` isn't configured yet, so auto-selection falls back to `gpu_unlimited > priority` which often picks the wrong project. Ask the user which project to use.

Create a CPU notebook, SSH in, explore the filesystem. Confirm:
- `shared_path_group` — usually `/inspire/hdd/global_user/<username>`, visible across ALL projects. SSH tools go here.
- `target_dir` — usually `/inspire/hdd/project/<slug>/<username>`, project-specific workdir.

**Keep the CPU notebook running** for Phase 3.

### Phase 3: GPU SSH bootstrap (skip if CPU/4090 only)

GPU notebooks (H100/H200) have no internet. `notebook ssh` still works but needs:
- **Always**: `rtunnel_bin` on shared filesystem (download via CPU notebook). Lives on `shared_path_group` so it's downloaded once for all projects.
- **Plus one of**:
  - `apt_mirror_url` — simpler, no pre-placed debs needed. Ask if the platform has an internal Ubuntu mirror (Nexus/Artifactory). Dropbear installed automatically.
  - `dropbear_deb_dir` — pre-place dropbear .deb packages on shared filesystem.

Both go in `[ssh]` section of project config.

**Questions to ask:**
1. "Do you need GPU notebook SSH?" — skip if no
2. "Does your platform have an internal APT mirror?" — determines Path A vs B
3. "Where should tools go?" — suggest `<shared_path_group>/tools/`

Reuse the CPU notebook from Phase 2c for downloading rtunnel.

### Phase 4: Bridge profile

`inspire notebook ssh <id> --save-as <name>` saves a reusable profile. Ask which notebook should be the default bridge — typically a CPU notebook for code sync/execution.

Bridge profiles break when notebooks restart. `bridge exec`/`ssh` auto-reconnect for notebook-backed profiles, but manually-added tunnels need manual recovery.

## Troubleshooting (check in this order)

1. `inspire config show` — look for `default` source tags (means not configured), placeholder values
2. `inspire config check` — validates auth, catches stale passwords
3. Missing `target_dir` — most common cause of sync/bridge failures
4. Wrong workspace — bridge/sync need CPU workspace (internet), jobs need GPU
5. GPU SSH not working — needs `rtunnel_bin` plus either `apt_mirror_url` or `dropbear_deb_dir`
6. Stale catalog — re-run `inspire init --discover` to refresh
