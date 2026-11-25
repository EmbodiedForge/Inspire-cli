# Inspire CLI

Command-line interface for the Inspire HPC training platform.

**What this is.** The tool we wrote for our own daily work on the platform: submitting training
jobs, keeping code and checkpoints moving, and getting a shell into notebooks the platform does
not officially hand out. It is not a platform SDK, not a replacement for the web app, and not a
coverage-driven client for every Inspire endpoint. A command earns its place by removing a
foot-gun we actually hit or saving time we actually spend.

**Relationship to `qzcli`.** They grew up around the same time and overlap deliberately. Use
whichever fits your workflow. This one is optimised for scriptable output (`--json` on every
command), repo-driven job iteration, and SSH/Jupyter bridges into notebooks.

**Support.** Maintained by the people who use it, in daily use; there is no support desk. Read
the [Security Compliance Notice](#security-compliance-notice) before touching the SSH features -
one of them crosses a line the platform cares about.

## Installation

```bash
# Recommended for agent hosts: includes the browser extra used by `api eval`.
uv tool install --force --refresh 'inspire-cli[browser] @ git+https://github.com/EmbodiedForge/Inspire-cli.git'
"$(uv tool dir)/inspire-cli/bin/playwright" install chromium

# CLI-only (no browser extra, so `api eval` and Playwright login are unavailable):
uv tool install git+https://github.com/EmbodiedForge/Inspire-cli.git
```

### Local Development

```bash
git clone https://github.com/EmbodiedForge/Inspire-cli.git inspire-cli
cd inspire-cli
uv sync --frozen --dev     # runtime and development dependencies from the lockfile
uv run --frozen pre-commit install
uv run inspire --help      # run the working tree without installing anything
```

Do **not** `uv tool install -e .` this project. An editable install rewrites the shared
`inspire` binary the moment anyone edits the tree. Install a snapshot instead, and note that
`--refresh` is required or a cached wheel from an older tree gets reused:

```bash
uv tool install --force --refresh .
```

Run the public checks with the versions in the lockfile:

```bash
uv run --frozen ruff check .
uv run --frozen black --check .
uv run --frozen pytest
```

## Zsh Completion

`inspire` uses Click's native shell completion. On `zsh`, the clean setup is the
standard `fpath + compinit` flow, the same model many mature CLIs use.

1. Make sure your `~/.zshrc` adds a personal completion directory before `compinit`:

```zsh
fpath=($HOME/.zsh/completions $fpath)
autoload -Uz compinit
compinit
```

2. Generate the completion function once:

```bash
mkdir -p ~/.zsh/completions
_INSPIRE_COMPLETE=zsh_source inspire > ~/.zsh/completions/_inspire
```

3. Reload your shell:

```bash
exec zsh
```

Notes:
- `oh-my-zsh` is not required.
- Completion stays local-config-driven through Click `shell_complete=` hooks.
- For local development, you can replace `inspire` with `uv run inspire` when generating `_inspire`.

## Quick Start

### 1. Auto-discover your platform

```bash
inspire config account register lab \
  --username YOUR_USERNAME \
  --base-url https://your-platform.com
inspire --account lab init --discover --force --yes
```

This logs into one explicit account, refreshes its live platform catalog, and
writes the selected account into the current project's `.inspire/config.toml`.
Global TOML keeps stable Workspace and Project name selectors. Rebuildable IDs,
Compute Groups, Workspace Specs, and refresh diagnostics are stored separately
under `~/.local/state/inspire-cli/accounts/lab/discovery.json`.

Set your password as an env var to avoid repeated prompts:
```bash
export INSPIRE_PASSWORD="your_password"
```

### 2. Verify

```bash
inspire config show    # Check all values resolved
inspire config check   # Validate API auth
```

### 3. Start using

```bash
inspire resources list          # View GPU availability
inspire notebook create --name dev --resource 4xCPU --wait
inspire notebook ssh <id>       # SSH into notebook (auto-installs tunnel)
```

## Commands

| Command | Description |
|---------|-------------|
| `inspire job create` | Submit a training job |
| `inspire job status/logs/list` | Monitor and manage jobs |
| `inspire job stop/wait` | Stop or wait for a job |
| `inspire job availability --workspace-id <id>` | Plan one workspace: capacity plus Scheduling Offer priority evidence |
| `inspire job offers --workspace-id <id>` | Requestable group/quota shapes, each with a copyable `--group/--quota` selector |
| `inspire hpc create/list/status/logs/metrics` | Create and observe HPC workloads |
| `inspire ray create/list/status/logs/metrics` | Create and observe Ray workloads |
| `inspire hpc batch` / `inspire ray batch` | Prepare or submit JSON/TOML launch matrices |
| `inspire run "<cmd>"` | Deprecated shorthand for `job create`; still works, including `--sync` / `--watch` |
| `inspire sync` | Sync code through SSH or notebook Jupyter transport |
| `inspire bridge exec "<cmd>" [--stdin]` | Run command through a configured Bridge profile |
| `inspire bridge ssh [--bridge <name>]` | Interactive SSH shell to a Bridge profile |
| `inspire bridge scp <source> <destination>` | Upload/download files through SSH or Jupyter |
| `inspire runtime show/explain/set` | Inspect or mutate effective runtime state for agents and debugging |
| `inspire notebook list` | List notebooks (supports `--columns`, `--tunneled`; JSON output via the global `--json`) |
| `inspire notebook create` | Create a notebook instance |
| `inspire notebook start/stop` | Start or stop a notebook |
| `inspire notebook download` | Download files or directories via Jupyter HTTP/proxy (no SSH tunnel) |
| `inspire notebook ssh <id>` | Set up SSH, or save a Jupyter-only Bridge profile when GPU ingress is denied |
| `inspire notebook resource <notebook>` | One screen per instance: what the platform declares, what the container's cgroup enforces (memory.max, memory.events, cpu.max, pids), what is in use now, and — with `--since` — the platform's usage history. Exits non-zero on a recorded OOM kill or a declaration that disagrees with the cgroup |
| `inspire image list/detail` | Browse Docker images |
| `inspire image save/register` | Save or register custom images |
| `inspire model deploy` | Deploy a model inference serving (e.g., SGLang, vLLM) |
| `inspire model list` | List model inference servings |
| `inspire model status <id>` | Get serving status and endpoint URL |
| `inspire model stop <id>` | Stop a model serving |
| `inspire model delete <id>` | Delete a model serving |
| `inspire tunnel add/list/status` | Manage SSH tunnels to Bridge |
| `inspire tunnel ssh-config` | Generate SSH config for direct access |
| `inspire project list` | View projects and GPU quota |
| `inspire resources list [--view nodes]` | View GPU and CPU availability |
| `inspire resources policy --workspace <name>` | View idle-reclaim rules and runtime limits |
| `inspire config show/check` | Inspect and validate configuration |
| `inspire config account list/register/show/doctor/export/remove` | Edit and diagnose the alias-keyed Account Catalog |
| `inspire init` | Generate starter config from env vars |
| `inspire init --discover` | Auto-discover projects, workspaces, compute groups |
| `inspire status <ref>` | Status of a job, notebook or model serving |
| `inspire logs <ref>` | Logs for a job or model serving |
| `inspire stop <ref>` | Stop a job, notebook or model serving |
| `inspire exec <ref> -- <cmd>` | Run a command in a job, notebook or serving |
| `inspire resolve <ref> [--type] [--verify]` | Show what a reference points at |
| `inspire notebook doctor [notebook] [-b bridge]` | Diagnose notebook SSH/tunnel failures |
| `inspire storage ls/get/put/cat/stat/rm/mkdir/mv/roots/probe` | WebDAV and SFTPGo storage surface |
| `inspire model-hub list/detail/versions/related-servings/delete-check` | Inspect Model Hub models |
| `inspire cache show/stats/clear` | Inspect the local resource-reference cache |
| `inspire user whoami/permissions/api-keys/ssh-keys` | Inspect the identity in use (secrets redacted) |

**Global Flags:**
- `--json` - Output in JSON format (useful for scripting)
- `--account <alias>` - Select one configured account for this invocation

### One reference, several verbs

`status`, `logs`, `stop`, `exec` and `resolve` all take the same REFERENCE: a resource name, a
full ID, or an unambiguous ID prefix, so you never have to remember which subcommand tree a
resource lives under.

```bash
inspire status img-probe            # by name
inspire status 76f01301             # by ID prefix
inspire exec img-probe -- pwd       # runs remotely; prints /inspire/hdd/project/...
inspire resolve img-probe --type notebook --verify
```

`resolve` prints tab-separated `type / id / name / status`, which pipes straight into shell
scripts. Lookups go through a local cache so a loop does not re-query the platform; `inspire
cache show` and `inspire cache clear` manage it, and `resolve --verify` forces a platform check.

## Examples

```bash
# Submit a training job (no --group/--quota: the CLI picks and states the row it used)
inspire job create --name "train-v1" --resource "4xH200" --command "bash train.sh"

# The submit path: read capacity and the offer's priority evidence, preview the exact
# selectors, submit with those same selectors, then follow the job.
inspire job availability --workspace gpu --resource 4xH200 --priority 6
inspire job offers --workspace gpu          # copy a row's --group ... --quota ...
inspire job create --name "train-v1" --resource "4xH200" --command "bash train.sh" \
  --group "GPU group" --quota 4,80,900 --priority 6 --dry-run
inspire job create --name "train-v1" --resource "4xH200" --command "bash train.sh" \
  --group "GPU group" --quota 4,80,900 --priority 6
inspire job wait <job-id>
inspire job logs <job-id>

# Low-priority jobs (priority 1-3) are preemptible and do not consume compute points.
# Fault tolerance is enabled by default for them; the platform reruns the same command.
inspire job create --name "train-low" --resource H200 --priority 1 \
  --fault-tolerance-max-retry 100 \
  --command "bash train.sh --resume-latest"

# `inspire run` is deprecated (its --sync/--watch still work); sync explicitly and
# submit with `inspire job create` as above.
inspire sync && inspire bridge exec "git log -1"

# A GPU notebook whose SSH proxy ingress is blocked uses Jupyter automatically
inspire notebook ssh <notebook-id> --save-as mybridge
inspire sync --transport auto --bridge mybridge --source bundle
inspire bridge exec --transport auto --bridge mybridge -- nvidia-smi

# Audit Bridge worktree safety before syncing
inspire sync --check

# --allow-dirty only accepts local dirty state; Bridge-side dirty files still block sync.
# --force can reset diverged remote history, but it does not override the remote dirty audit.

# Stream local stdin to remote command (no heredoc quoting hacks)
inspire bridge exec --bridge mybridge --stdin -- bash -s < scripts/watch_eval.sh

# Set up SSH tunnel to a notebook
inspire notebook ssh <notebook-id> --save-as mybridge
ssh mybridge

# What is this container actually capped at, and what is using it? free(1) answers the host;
# memory.max is the cap. One screen pairs what the platform declares, what the cgroup enforces and
# what is in use, and exits non-zero when memory.events records an OOM kill or the declaration
# disagrees with the enforced cgroup - so it is safe to poll from a watcher.
inspire notebook resource <notebook-id>
inspire --json notebook resource <notebook-id>
inspire notebook resource <notebook-id> --since 6h   # plus the platform's own usage history

# Live per-process view over saved notebook tunnels: the GPU table on GPU hosts, a CPU/memory view
# (load, host memory, heaviest processes) on CPU-only pods
inspire notebook resource --processes
inspire notebook resource --bridge mybridge --processes --watch
inspire notebook resource --processes 20   # 20 rows in the CPU view's process table

# Copy files through a configured bridge profile (SSH or Jupyter)
inspire bridge scp ./model.py /tmp/model.py --bridge mybridge
inspire bridge scp -d /tmp/checkpoints/ ./checkpoints/ -r --bridge mybridge

# Download checkpoints without the SSH tunnel path
inspire notebook download -n <notebook-id> /inspire/path/to/checkpoints ./checkpoints.tar.gz

# Check GPU availability and project quota
inspire resources list
inspire project list

# List notebooks with custom columns (show tunnel status)
inspire notebook list -c name,status,tunnel

# Show only notebooks with active SSH tunnels
inspire notebook list --tunneled -n 10

# Deploy GLM-5.1-FP8 with preset (recommended - includes EAGLE speculative decoding, parsers)
inspire model deploy --name glm51-serve --preset glm51 --project "CQ项目-科研驾驶舱"

# Manual deployment with full control (use single quotes to preserve spaces)
inspire model deploy \
  --name glm51-serve \
  --image sglang:v0.5.10-cu130 \
  --command 'SGLANG_ENABLE_SPEC_V2=1 python -m sglang.launch_server --model-path /path/to/model --tp 8 --reasoning-parser glm45 --tool-call-parser glm47 --speculative-algorithm EAGLE --speculative-num-steps 3 --host 0.0.0.0 --port 8000' \
  --gpu-type NVIDIA_H200 \
  --gpu-count 8 \
  --project "CQ项目-科研驾驶舱"

# Check model serving status
inspire model status serving-abc-123

# List all model servings
inspire model list

# JSON output for scripting
inspire --json notebook list
inspire --json job list
```

Low-priority training should write checkpoints to persistent shared storage and
make its startup command resume the latest valid checkpoint. Platform fault
tolerance creates another run of the same job; it does not restore process
memory. Priorities 1–3 default to fault tolerance with 100 maximum runs. Pass
`--no-fault-tolerant` to opt out, or set `--fault-tolerance-max-retry 1..2000`
to control the limit explicitly.

## HPC and Ray Workloads

Inspect the platform's current workload-specific Scheduling Offers before
creating a profile or launch:

```bash
inspire hpc offers --workspace cpu
inspire ray offers --workspace cpu
inspire hpc availability --workspace cpu
inspire ray availability --workspace cpu
```

Create and dry-run use the same live resolution path. Workspace, project,
compute group, quota row, and image are resolved inside the selected account:

```bash
inspire hpc create \
  --name hpc-demo \
  --entrypoint 'srun python train.py' \
  --workspace cpu \
  --project my-project \
  --group 'CPU group' \
  --quota 0,4,16 \
  --image pytorch:latest \
  --dry-run

inspire ray create \
  --name ray-demo \
  --entrypoint 'python driver.py' \
  --workspace cpu \
  --project my-project \
  --group 'CPU group' \
  --quota 0,1,4 \
  --image pytorch:latest \
  --min-replicas 1 \
  --max-replicas 1 \
  --dry-run
```

Portable defaults belong in workload profiles. Profiles store intent such as
aliases and quota values, never resolved platform IDs:

```bash
inspire hpc profile set cpu-small --scope project \
  --value workspace=cpu \
  --value project=my-project \
  --value group='CPU group' \
  --value quota=0,4,16 \
  --value image=pytorch:latest

inspire hpc create --profile cpu-small --name hpc-demo --entrypoint 'srun hostname'
```

Batch files may be JSON or TOML. Matrices are Cartesian products and support
`{variable}` templates:

```json
{
  "defaults": {
    "workspace": "cpu",
    "project": "my-project",
    "group": "CPU group",
    "quota": "0,4,16",
    "image": "pytorch:latest"
  },
  "hpc": [
    {
      "name": "sweep-{seed}",
      "entrypoint": "srun python train.py --seed {seed}",
      "matrix": {"seed": [1, 2, 3]}
    }
  ]
}
```

```bash
inspire hpc batch sweep.json --dry-run
inspire hpc batch sweep.json
```

All rows are prepared before the first mutation. If a later submission fails,
the CLI stops and deletes successful rows from that batch in reverse order.
Pass `--keep-partial` only when partial results should remain on the platform.
JSON failures include submitted IDs, rollback results, and any retained IDs.

Observability is workload-specific:

```bash
inspire hpc events <job-id>
inspire hpc instances <job-id>
inspire hpc logs <job-id> --since 3600
inspire hpc metrics <job-id> --metric cpu_usage_rate --series

inspire ray events <ray-job-id>
inspire ray instances <ray-job-id>
inspire ray scaling-histories <ray-job-id>
inspire ray logs <ray-job-id> --since 3600
inspire ray metrics <ray-job-id> --metric memory_usage_rate --series
```

The current platform reports launchable images with `READY` or `SUCCESS`.
Unknown image states fail closed until their platform behavior is probed.

## Credential Handling

What ends up on your disk, and what the CLI does about it - stated plainly because some of it
was silently wrong for a while:

- `~/.config/inspire/config.toml` can hold platform credentials. It is written `0600`, and
  group/other bits are stripped on **every** rewrite. The writer used to "preserve the existing
  mode", which meant one loose file kept re-issuing its own `0664` forever.
- `~/.inspire/bridges*.json` holds bridge profiles including the notebook proxy URL, whose query
  string **is a live Jupyter token** - root inside that notebook. Older releases wrote these from
  the process umask, so installs from that era had them readable by every local user. Reading our
  state now repairs permissions in place: the state directory and subdirectories go to `0700` and
  state files to `0600`, including interrupted-save temporaries and quarantined copies.
- `rtunnel` - the binary exec'd to carry that token - is pinned to one release with a per-platform
  sha256 and verified before extraction. Pointing `rtunnel_download_url` at a mirror skips
  verification (we hold no reviewed digest for a source we do not ship) and logs the observed
  sha256; `inspire config check` reports that override so it cannot be set invisibly.
- While a tunnel is up, that token is visible in `ps` (in the ProxyCommand and in rtunnel's own
  argv). This is an upstream CLI limitation rather than a flag we can flip: hiding it fully needs
  rtunnel to accept the URL from the environment.
- `inspire storage probe` runs a tiny WebDAV create/read/delete cycle when uploads misbehave.

## Security Compliance Notice

> **Air-gapped GPU Cluster Security Policy**
>
> Air-gapped clusters enforce strict network isolation for sensitive GPU workloads and data processing. Physical separation between containers and the public internet is mandatory.
>
> **The following activities are strictly prohibited:**
> 1. **Prohibited:** Exposing cluster services to the public internet via port forwarding (e.g., `ssh -L`, `ssh -R`, or any reverse tunnel);
> 2. **Prohibited:** Using local proxies (e.g., `ssh -NR`, HTTP/SOCKS5 proxies) to grant air-gapped instances public internet access.
>
> `inspire-cli` uses `rtunnel` solely for legitimate development and operational needs (SSH access, file sync, remote command execution). Violations will result in permanent revocation of port forwarding privileges.

## SSH/SCP Reliability Notes

- There is no `inspire tunnel start` command. Create or refresh bridge profiles with `inspire notebook ssh <notebook-id> --save-as <name>` (or `inspire tunnel add` / `inspire tunnel update`), then validate with `inspire tunnel status`.
- `inspire bridge ssh` and `inspire bridge scp` validate `--bridge` names before connectivity checks. If a profile is missing, run `inspire tunnel list`.
- Use `inspire notebook download -n <notebook> <remote> <local>` for large notebook artifacts when the SSH tunnel path is too slow. Files under the Jupyter root stream through `/files/...`; absolute directories are tarred in place under a sibling `inspire-downloads/` directory and streamed through Jupyter `/proxy/...`, then cleaned up by default.
- `bridge exec`, `bridge scp`, and `sync` accept `--transport auto|ssh|jupyter`. `auto` retains SSH/rebuild for CPU, 4090, and legacy profiles; known H100/H200 notebook profiles fall back to Jupyter when the SSH ingress probe fails.
- Jupyter execution and transfer require a notebook-backed bridge profile with `notebook_id` plus valid web authentication. They do not require `/proxy/<custom-port>` ingress.
- `inspire bridge exec --stdin -- <remote command>` forwards local stdin over either supported execution transport.
- Saved notebook profiles now store the source notebook ID. Reusing `--save-as <name>` for a different notebook refreshes the tunnel instead of reusing stale tunnel state.
- `inspire bridge ssh`, `inspire bridge exec`, and interactive `inspire notebook ssh` auto-rebuild/reconnect dropped tunnels for notebook-backed profiles, using `tunnel.retries` / `tunnel.retry_pause` as retry controls.
- Non-notebook tunnel profiles (for example, manually added profiles without `notebook_id`) cannot be auto-rebuilt and still require manual tunnel recovery.
- `inspire notebook doctor` inspects the notebook itself over the Jupyter shell - which services
  listen on all interfaces, and whether our public key is still in `/root/.ssh/authorized_keys` -
  so it keeps producing useful facts even when the SSH path under test is the broken one. Notebook
  filesystems are ephemeral: a restarted notebook can come back without our key while the bridge
  profile still looks healthy, and that shows up as an authentication failure. Listener findings
  are reported, not fatal (`doctor` still exits 0), because the platform's own listeners
  (`jupyter-lab`, `code-server --auth none`) and our rtunnel listener are not fixable from inside.
- `inspire tunnel ssh-config` now writes shell-quoted `ProxyCommand` entries so proxy URLs with query parameters/tokens remain safe in `~/.ssh/config`.

## Configuration

The recommended project setup is an explicit account-scoped catalog refresh:

```bash
inspire --account lab init --discover --force --yes
```

This merge-updates the global Account Catalog and writes the current project's
account context. Platform observations are rebuilt on each refresh rather than
treated as durable config.

Config files are loaded in order (later overrides earlier):
1. Global: `~/.config/inspire/config.toml`
2. Project: `./.inspire/config.toml`
3. Environment variables

Accounts are keyed by local aliases, and each entry declares its platform
username explicitly. Account selection precedence is:

1. `--account <alias>`
2. `INSPIRE_ACCOUNT`
3. project `[context].account`
4. global `[context].account`

If an Account Catalog exists but no alias is selected, the CLI fails and lists
the available aliases. It never guesses from a username or scans accounts for
one that happens to work.

For the selected alias, project `[accounts.<alias>]` values override global
values. Credentials may come from `password`, `password_command`, or the
`INSPIRE_PASSWORD` fallback. Legacy `[auth].username` and `[auth].password`
remain supported only for username-only configurations.

Account Catalog editing does not reveal credentials:

```bash
inspire config account register lab \
  --username your_platform_username \
  --base-url https://your-inspire-platform.com
inspire config account list
inspire config account show lab
inspire config account doctor lab --live
inspire config account export lab
INSPIRE_ACCOUNT=lab inspire user whoami
```

Preview a live refresh without changing global config, project config, state,
sessions, or SSH files:

```bash
inspire --account lab --json init --discover --dry-run --force --yes
```

Discovery replaces successful observation scopes. If one scope fails, it
preserves last-known rows as stale diagnostic data, marks account state
`partial`, returns non-zero, and skips project/SSH writes.

`inspire init` probe-only options are effective only with `--discover --probe-shared-path`:
`--probe-limit`, `--probe-keep-notebooks`, `--probe-pubkey`/`--pubkey`, and `--probe-timeout`.
Without that combination, they are accepted but ignored.

Example `config.toml`:

```toml
[context]
account = "lab"

[accounts.lab]
username = "your_platform_username"
base_url = "https://your-inspire-platform.com"
# Store only the password as secret material, or use a command-backed provider.
# password = "your_password"
# password_command = "your-password-provider --account lab"  # pragma: allowlist secret

[api]
# Shared fallback when an account does not override base_url.
base_url = "https://your-inspire-platform.com"

[accounts.lab.workspaces.cpu]
name = "CPU Workspace"

[accounts.lab.workspaces.gpu]
name = "GPU Workspace"

[accounts.lab.projects.training]
workspace = "gpu"
name = "Training Project"

[bridge]
# Timeout in seconds for `inspire bridge exec`
action_timeout = 600

[defaults]
# Shared fallback settings for jobs and notebooks
# resource = "1xH200"
# image = "pytorch:latest"
# priority = 6
# shm_size = 32
# project_order = ["cq", "ci"]

[job]
# resource = "4xH200"
# project_id = "project-..."
# image = "pytorch:latest"
# priority = 6
# shm_size = 32

[notebook]
# resource = "1xH200"
# project_id = "project-..."
# image = "pytorch:latest"
# priority = 6
# shm_size = 32
# post_start = "bash /workspace/bootstrap.sh"

[remote_env]
# Exported before bridge exec, job commands, and notebook post-start commands/scripts.
# PIP_INDEX_URL = "https://mirror.example/simple"
# APT_MIRROR_URL = "http://nexus.example.com/repository/ubuntu/"

[remote_runtime]
# White-box remote runtime for bridge exec, bridge ssh, and job wrappers.
# cwd = "/inspire/hdd/global_user/your-user/openpi"
# path_prepend = ["~/.local/bin", ".venv/bin"]
# venv = "auto"   # auto | off | "/abs/path/to/venv" | ".venv-name"
#
# [remote_runtime.env]
# UV_LINK_MODE = "copy"

[ssh]
# For GPU notebooks (H100/H200) without internet:
# rtunnel_bin = "/inspire/shared/tools/rtunnel"
# Option A: APT mirror (simpler — no pre-placed debs needed)
# apt_mirror_url = "http://nexus.example.com/repository/ubuntu/"
# Option B: Pre-placed dropbear debs
# dropbear_deb_dir = "/inspire/shared/debs/dropbear"
```

Use `[remote_env]` for explicit remote environment variables such as `PIP_INDEX_URL`. Use `[remote_runtime]` for agent-visible execution primitives: `cwd`, `path_prepend`, `venv`, and `[remote_runtime.env]`. This keeps bridge and job wrappers debuggable without forcing shell-init hacks or opaque one-line commands. For notebook SSH bootstrap on offline GPU notebooks, `ssh.apt_mirror_url` remains the explicit setting, and `remote_env.APT_MIRROR_URL` is also accepted as a fallback.

Every path that runs something remotely applies `[remote_env]`: `bridge exec`, `bridge ssh`,
`job create`, `job exec`, `run`, notebook post-start, and `notebook exec`. The exports are prepended
with the rest of the runtime in one order - env, `path_prepend`, venv activation, then `cwd` - and
are never echoed back or included in `--json` output, because a value may be a secret;
`inspire runtime explain` shows each export together with the config section it came from. If the
configuration cannot be read, the command is refused rather than run without the runtime it was
supposed to have. `notebook exec` applies them to the script that starts the command, so the command
inherits them through its environment; they are deliberately not written into the exec's record directory,
which outlives the call. `notebook exec --attach` reads that record and loads
no remote environment at all.

`inspire config` and `inspire runtime` are intentionally different layers:

- `inspire config` manages durable defaults and persistence.
- `inspire runtime` manages the current execution state that Bridge/job/notebook wrappers will consume.

Use `inspire config set remote_runtime.*` when you want to change the repo default. Use `inspire runtime ... --scope session` when an agent needs a temporary override for debugging or one-off execution.

View current config:
```bash
inspire config show
inspire --json config show
inspire config check   # Validate config + API auth
inspire --json config check
inspire --json init --template --project --force
```

Inspect or change effective runtime state:
```bash
inspire runtime show
inspire --json runtime show
inspire runtime explain
inspire runtime set cwd /inspire/hdd/global_user/your-user/openpi --scope project
inspire runtime path add ~/.local/bin
inspire runtime env set HF_TOKEN "$HF_TOKEN"
inspire bridge exec --explain -- python train.py
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `INSPIRE_USERNAME` | Platform username |
| `INSPIRE_PASSWORD` | Platform password |
| `INSPIRE_BASE_URL` | API base URL |
| `INSPIRE_TARGET_DIR` | Shared filesystem path |
| `INSPIRE_DEFAULT_RESOURCE` | Shared default resource for jobs and notebooks |
| `INSPIRE_DEFAULT_IMAGE` | Shared default image for jobs and notebooks |
| `INSPIRE_DEFAULT_PRIORITY` | Shared default priority for jobs and notebooks |
| `INSPIRE_PROJECT_ORDER` | Project preference order for automatic selection |
| `INSPIRE_SHM_SIZE` | Shared default shared memory size |
| `INSPIRE_WORKSPACE_CPU_ID` | CPU workspace ID (default workspace) |
| `INSPIRE_WORKSPACE_GPU_ID` | GPU workspace ID (H100/H200) |
| `INSPIRE_WORKSPACE_INTERNET_ID` | Internet-enabled workspace ID (e.g. RTX 4090) |
| `INSPIRE_JOB_RESOURCE` | Job-specific default resource |
| `INSPIRE_PROJECT_ID` | Default project ID |
| `INSP_IMAGE` | Default Docker image |
| `INSP_PRIORITY` | Job priority (1-10) |
| `INSPIRE_NOTEBOOK_PROJECT_ID` | Notebook-specific default project ID |
| `INSPIRE_APT_MIRROR_URL` | APT mirror URL for offline notebook SSH bootstrap |

## Documentation

The public mirror ships the CLI, its built-in skills, and standalone regression tests. Run `inspire skills list` to find workflow guides.

### Restore a QZ browser session

From a source checkout, log in to QZ in Chrome and complete any requested verification. Export the browser storage state to a private file, then import it under the configured account alias.

```bash
uv run --frozen python scripts/debug_web_session.py import-state /path/to/state.json --account lab
uv run --frozen python scripts/debug_web_session.py refresh --account lab
```

The state export contains session cookies. Keep it out of version control and remove the export after import. The refresh command renews the existing session through SSO cookies. If the platform requires another login, complete it in the browser.

For anything command-specific, `--help` is the reference and is generated from the same tree you
installed: `inspire job --help`, then `inspire <command> --help`.
