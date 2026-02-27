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
| CPU/4090 notebook SSH | Fully automatic (rtunnel auto-downloaded and uploaded) | Nothing |
| GPU notebook SSH (no internet) | Automatic (rtunnel auto-uploaded) | `apt_mirror_url` or `dropbear_deb_dir` for SSH server |
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
- `shared_path_group` — usually `/inspire/hdd/global_user/<username>`, visible across ALL projects.
- `target_dir` — usually `/inspire/hdd/project/<slug>/<username>`, project-specific workdir.

The CPU notebook can be stopped after verification — it is not needed for subsequent phases.

### Phase 3: GPU SSH bootstrap (skip if CPU/4090 only)

GPU notebooks (H100/H200) have no internet. `notebook ssh` still works but needs an SSH server package installed on the notebook.

**rtunnel is handled automatically.** The CLI downloads rtunnel locally (`~/.local/bin/rtunnel`) and uploads it to each notebook via the Jupyter Contents API. No shared filesystem placement or CPU notebook needed.

**SSH server requirement — one of:**
  - `apt_mirror_url` — simpler, no pre-placed debs needed. Ask if the platform has an internal Ubuntu mirror (Nexus/Artifactory). Dropbear installed automatically via apt.
  - `dropbear_deb_dir` — pre-place dropbear .deb packages on shared filesystem.

Both go in `[ssh]` section of project config.

**Questions to ask:**
1. "Do you need GPU notebook SSH?" — skip if no
2. "Does your platform have an internal APT mirror?" — determines `apt_mirror_url` vs `dropbear_deb_dir`

**Legacy option:** `rtunnel_bin` in `[ssh]` config is still respected as highest priority if set. Users who already have rtunnel on shared filesystem do not need to change anything.

### Phase 4: Bridge profile

`inspire notebook ssh <id> --save-as <name>` saves a reusable profile. Ask which notebook should be the default bridge — typically a CPU notebook for code sync/execution.

Bridge profiles break when notebooks restart. `bridge exec`/`ssh` auto-reconnect for notebook-backed profiles, but manually-added tunnels need manual recovery.

## Troubleshooting (check in this order)

1. `inspire config show` — look for `default` source tags (means not configured), placeholder values
2. `inspire config check` — validates auth, catches stale passwords
3. Missing `target_dir` — most common cause of sync/bridge failures
4. Wrong workspace — bridge/sync need CPU workspace (internet), jobs need GPU
5. GPU SSH not working — needs either `apt_mirror_url` or `dropbear_deb_dir` for SSH server installation. rtunnel is auto-uploaded and should not be the cause; check `~/.local/bin/rtunnel` exists locally
6. rtunnel upload failed — check Jupyter Contents API access; fallback: set `rtunnel_bin` in `[ssh]` config pointing to a shared filesystem path
7. Stale catalog — re-run `inspire init --discover` to refresh

## Reporting issues

If you encounter a setup problem you can't resolve (discovery failures, unexpected CLI behavior, missing features), suggest the user file an issue:
- **Private (Codeberg):** https://codeberg.org/cyteena/inspire-cli/issues
- **Public (GitHub):** https://github.com/EmbodiedForge/Inspire-cli/issues
