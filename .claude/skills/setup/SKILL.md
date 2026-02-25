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
| SSH tools on internet machines | `notebook ssh` auto-installs | Nothing — guaranteed to work |
| SSH tools on GPU (no internet) | `notebook ssh` uses apt mirror or pre-placed binaries | APT mirror URL, or install dir on shared filesystem |
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
- **target_dir** — "Where is your code on the shared filesystem?" The `--discover` suggests the catalog workdir but this is from the API and may not be correct. **Always verify via CPU notebook SSH in Phase 2c before finalizing.** This is the most important config value — sync, bridge exec, and job logs all depend on it.

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

The values are project **names** (not IDs). The auto-selection sort uses `project_order` as the primary ranking, with `gpu_unlimited` as a tiebreaker. Projects not listed sort after all listed ones.

## Phase 2c: Verify shared paths via CPU notebook

**CPU notebook SSH is guaranteed to work** — CPU/internet notebooks have internet access, so `notebook ssh` auto-installs openssh + rtunnel with zero pre-setup. This makes a CPU notebook the best tool for verifying filesystem paths.

**Always do this** — the `--discover` catalog gets paths from the API, but they may be stale, wrong, or the directory may not exist yet. Never blindly trust catalog paths for `target_dir`. A CPU notebook is the ground truth.

**How:**
1. Create a CPU notebook in any project:
   ```bash
   inspire notebook create --name path-check --resource 4xCPU --image pytorch25 --wait
   ```
2. SSH in and explore the shared filesystem:
   ```bash
   inspire notebook ssh <cpu-id> --command "
     echo '=== Global user dir ==='
     ls /inspire/hdd/global_user/
     echo '=== Project dirs ==='
     ls /inspire/hdd/project/
     echo '=== My home ==='
     echo \$HOME
     echo '=== Workdir ==='
     pwd
   "
   ```
3. Use the output to confirm or correct `target_dir` and `shared_path_group` in the config.
4. **Keep this notebook running** — reuse it for Phase 3 (rtunnel bootstrap) if GPU SSH is needed.

**Key paths to discover:**
- `shared_path_group`: Usually `/inspire/hdd/global_user/<username>` — visible from ALL notebooks across ALL projects. This is where SSH tools (rtunnel) should go.
- `target_dir`: Usually `/inspire/hdd/project/<project-slug>/<username>` — project-specific workdir for code sync.

## Phase 3: SSH tools bootstrap (only for GPU clusters without internet)

**Skip this phase if** the user only uses CPU/4090 notebooks (they have internet, `notebook ssh` auto-installs everything).

**When needed:** H100/H200 clusters have no internet. `notebook ssh` still works, but needs either an APT mirror URL or pre-placed binaries for dropbear. It always needs a pre-placed rtunnel binary.

**Key concept:** After `--discover`, the global config catalog has `shared_path_group` per project (e.g. `/inspire/hdd/global_user/<username>`). This path is visible from ALL notebooks across all projects. SSH tools must go here, not in a project-specific workdir.

**What to ask the user:**
1. **"Do you need SSH access to GPU notebooks (H100/H200)?"** — if no, skip this phase entirely
2. **"Does your platform have an internal APT mirror reachable from GPU notebooks?"** — many platforms provide a Nexus/Artifactory mirror on the internal network (e.g. `http://nexus.example.com/repository/ubuntu/`). If yes, this greatly simplifies setup — dropbear can be installed via apt automatically, no pre-placed debs needed.
3. **"Where should SSH tools be installed?"** — suggest `<shared_path_group>/tools/` using the path verified in Phase 2c.

**Reuse the CPU notebook from Phase 2c** if still running. Otherwise create one — CPU SSH is guaranteed to work.

### Path A: APT mirror available (simpler)

If the platform has an internal APT mirror reachable from GPU notebooks:

**Mirror requirements:**
- Must be a full Ubuntu mirror with at least `main` and `universe` components
- Must have packages for the container's Ubuntu codename (auto-detected from `/etc/os-release`, e.g. `jammy` for 22.04, `noble` for 24.04)
- `dropbear-bin` depends on `libtomcrypt1`, `libtommath1`, `zlib1g` — these are pulled automatically by `apt-get install` from the mirror
- The bootstrap temporarily disables existing apt sources (archive.ubuntu.com) that are unreachable from no-internet GPUs, installs dropbear, then restores them

1. Set `apt_mirror_url` in project config:
   ```toml
   [ssh]
   apt_mirror_url = "http://nexus.example.com/repository/ubuntu/"
   ```
2. Still need rtunnel pre-placed — SSH into the CPU notebook and download rtunnel to the shared path:
   ```bash
   inspire notebook ssh <cpu-id> --command "
     TOOLS=<shared_path_group>/tools
     mkdir -p \$TOOLS
     curl -fsSL https://github.com/Sarfflow/rtunnel/releases/download/nightly/rtunnel-linux-amd64.tar.gz \
       -o /tmp/rtunnel.tgz
     tar -xzf /tmp/rtunnel.tgz -C \$TOOLS
     chmod +x \$TOOLS/rtunnel
   "
   ```
3. Set rtunnel path in project config:
   ```toml
   [ssh]
   rtunnel_bin = "<shared_path_group>/tools/rtunnel"
   apt_mirror_url = "http://nexus.example.com/repository/ubuntu/"
   ```
4. Stop the CPU notebook (unless needed for bridge).

**Dropbear is handled automatically** — `notebook ssh` detects the apt mirror, adds it as a source, and runs `apt-get install dropbear-bin` on the GPU notebook. No `dropbear_deb_dir` needed.

### Path B: No APT mirror (full bootstrap)

If no mirror is available, pre-place both rtunnel and dropbear on the shared filesystem:

- Start a CPU notebook in the chosen project
- SSH into it (`inspire notebook ssh <id>`) — this auto-installs openssh + rtunnel on the CPU side
- From inside: download rtunnel binary + dropbear deb packages to the chosen install dir
- Set config: `rtunnel_bin`, `dropbear_deb_dir` (in `[ssh]` section of project config)
- Stop the CPU notebook

### If tools already exist

If the user says "I already have them" or paths exist on disk, just ask for the paths and set the config.

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
5. SSH paths not set — `rtunnel_bin` needed for GPU notebook SSH; also need either `apt_mirror_url` or `dropbear_deb_dir`
6. Stale session — re-run `inspire init --discover` to refresh
