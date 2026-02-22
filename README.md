# Inspire CLI

Command-line interface for the Inspire HPC training platform.

## Installation

```bash
# Via SSH (recommended)
uv tool install git+ssh://git@github.com/EmbodiedForge/Inspire-cli.git

# Or via HTTPS
uv tool install git+https://github.com/EmbodiedForge/Inspire-cli.git
```

### Local Development

```bash
uv tool install -e .
inspire --help
```

## Quick Start

### 1. Set Credentials

```bash
export INSPIRE_USERNAME="your_username"
export INSPIRE_PASSWORD="your_password"
export INSPIRE_TARGET_DIR="/path/to/shared/filesystem"
```

### 2. Initialize Config

```bash
inspire init
```

### 3. Check Resources

```bash
inspire resources list
```

## Commands

| Command | Description |
|---------|-------------|
| `inspire job create` | Submit a training job |
| `inspire job status/logs/list` | Monitor and manage jobs |
| `inspire job stop/wait` | Stop or wait for a job |
| `inspire run "<cmd>"` | Quick job with auto resource selection |
| `inspire sync` | Sync code to shared filesystem (via SSH tunnel) |
| `inspire bridge exec "<cmd>"` | Run command on Bridge runner |
| `inspire bridge ssh [--bridge <name>]` | Interactive SSH shell to a Bridge profile |
| `inspire bridge scp <source> <destination>` | Upload/download files via Bridge tunnel |
| `inspire notebook list/create` | List or create notebook instances |
| `inspire notebook start/stop` | Start or stop a notebook |
| `inspire notebook ssh <id>` | SSH into notebook (sets up tunnel) |
| `inspire notebook top` | Show GPU utilization/memory for tunnel-backed notebooks |
| `inspire image list/detail` | Browse Docker images |
| `inspire image save/register` | Save or register custom images |
| `inspire tunnel add/list/status` | Manage SSH tunnels to Bridge |
| `inspire tunnel ssh-config` | Generate SSH config for direct access |
| `inspire project list` | View projects and GPU quota |
| `inspire resources list/nodes` | View GPU availability |
| `inspire config show/check` | Inspect and validate configuration |
| `inspire init` | Initialize configuration |

## Examples

```bash
# Submit a training job
inspire job create --name "train-v1" --resource "4xH200" --command "bash train.sh"

# Quick run with auto-selected resources, sync code and follow logs
inspire run "python train.py --epochs 100" --sync --watch

# Sync code and verify
inspire sync && inspire bridge exec "git log -1"

# Set up SSH tunnel to a notebook
inspire notebook ssh <notebook-id> --save-as mybridge
ssh mybridge

# Check live GPU usage for all saved notebook tunnels
inspire notebook top
inspire notebook top --bridge mybridge --watch

# Copy files through a configured bridge profile
inspire bridge scp ./model.py /tmp/model.py --bridge mybridge
inspire bridge scp -d /tmp/checkpoints/ ./checkpoints/ -r --bridge mybridge

# Check GPU availability and project quota
inspire resources list
inspire project list
```

## SSH/SCP Reliability Notes

- There is no `inspire tunnel start` command. Create or refresh bridge profiles with `inspire notebook ssh <notebook-id> --save-as <name>` (or `inspire tunnel add` / `inspire tunnel update`), then validate with `inspire tunnel status`.
- `inspire bridge ssh` and `inspire bridge scp` validate `--bridge` names before connectivity checks. If a profile is missing, run `inspire tunnel list`.
- Saved notebook profiles now store the source notebook ID. Reusing `--save-as <name>` for a different notebook refreshes the tunnel instead of reusing stale tunnel state.
- `inspire bridge ssh`, `inspire bridge exec`, and interactive `inspire notebook ssh` auto-rebuild/reconnect dropped tunnels for notebook-backed profiles, using `tunnel.retries` / `tunnel.retry_pause` as retry controls.
- Non-notebook tunnel profiles (for example, manually added profiles without `notebook_id`) cannot be auto-rebuilt and still require manual tunnel recovery.
- `inspire tunnel ssh-config` now writes shell-quoted `ProxyCommand` entries so proxy URLs with query parameters/tokens remain safe in `~/.ssh/config`.

## Configuration

Config files are loaded in order (later overrides earlier):
1. Global: `~/.config/inspire/config.toml`
2. Project: `./.inspire/config.toml`
3. Environment variables

Account password lookup follows the same layered model:
1. `[accounts."<username>"].password` from global config
2. `[accounts."<username>"].password` from project config (overrides global for same username)
3. `INSPIRE_PASSWORD` (fallback only if no account password was found)

Run `inspire init` to generate a starter config, or `inspire config show` to inspect the merged result.

`inspire init` probe-only options are effective only with `--discover --probe-shared-path`:
`--probe-limit`, `--probe-keep-notebooks`, `--probe-pubkey`/`--pubkey`, and `--probe-timeout`.
Without that combination, they are accepted but ignored.

Example `config.toml`:

```toml
[auth]
username = "your_username"

[accounts."your_username"]
# Optional: supports multi-account setups in global and/or project config
password = "your_password"

[api]
base_url = "https://your-inspire-platform.com"

[bridge]
# Timeout in seconds for `inspire bridge exec`
action_timeout = 600

[workspaces]
# cpu = "ws-..."       # Default workspace (CPU jobs / notebooks)
# gpu = "ws-..."       # GPU workspace (H100/H200 jobs)
# internet = "ws-..."  # Internet-enabled GPU workspace (e.g. RTX 4090)
# special = "ws-..."   # Custom alias (use with --workspace special)

[[compute_groups]]
name = "H100 Cluster"
id = "lcg-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
gpu_type = "H100"

[ssh]
# Optional: use a pre-cached rtunnel binary
# rtunnel_bin = "/inspire/shared/bin/rtunnel"
# Optional: custom rtunnel download URL
# rtunnel_download_url = "https://..."
# Optional: use dropbear packages from shared storage
# dropbear_deb_dir = "/inspire/shared/debs/dropbear"
# Required when dropbear_deb_dir is set
# setup_script = "/inspire/shared/scripts/setup_ssh_dropbear.sh"
```

View current config:
```bash
inspire config show
inspire config show --json
inspire config check   # Validate config + API auth
inspire --json config check
inspire config check --json
inspire init --json --template --project --force
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `INSPIRE_USERNAME` | Platform username |
| `INSPIRE_PASSWORD` | Platform password |
| `INSPIRE_BASE_URL` | API base URL |
| `INSPIRE_TARGET_DIR` | Shared filesystem path |
| `INSPIRE_WORKSPACE_ID` | Default workspace ID |
| `INSPIRE_WORKSPACE_CPU_ID` | CPU workspace ID (default workspace) |
| `INSPIRE_WORKSPACE_GPU_ID` | GPU workspace ID (H100/H200) |
| `INSPIRE_WORKSPACE_INTERNET_ID` | Internet-enabled workspace ID (e.g. RTX 4090) |
| `INSPIRE_PROJECT_ID` | Default project ID |
| `INSP_IMAGE` | Default Docker image |
| `INSP_PRIORITY` | Job priority (1-10) |
