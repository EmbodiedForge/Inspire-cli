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
| `inspire job status <id>` | Check job status |
| `inspire job logs <id>` | View job logs |
| `inspire job list` | List cached jobs |
| `inspire notebook list` | List notebook instances |
| `inspire notebook create` | Create interactive notebook |
| `inspire notebook ssh <id>` | SSH into notebook |
| `inspire notebook start <id>` | Start stopped notebook |
| `inspire resources list` | View GPU availability |
| `inspire run "<cmd>"` | Quick job with auto resource selection |
| `inspire sync` | Sync code to shared filesystem |
| `inspire bridge exec "<cmd>"` | Run command via Git Actions |

## Examples

```bash
# Submit a training job
inspire job create --name "train-v1" --resource "4xH200" --command "bash train.sh"

# Quick run with auto-selected resources
inspire run "python train.py --epochs 100"

# Check GPU availability
inspire resources list

# SSH into a notebook
inspire notebook ssh <notebook-id>

# Sync and run remotely
inspire sync && inspire bridge exec "cd /target && bash train.sh"
```

## Configuration

Config files are loaded from:
- Global: `~/.config/inspire/config.toml`
- Project: `./.inspire/config.toml`

Download the official config template (internal network only):
```bash
curl https://nc.sii.e-forge.org/public.php/dav/files/b42pj6oxfY7ikrM -o config.toml.example
```

Example `config.toml`:

```toml
[auth]
username = "your_username"

[api]
base_url = "https://your-inspire-platform.com"

[[compute_groups]]
name = "H100 Cluster"
id = "lcg-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
gpu_type = "H100"
```

View current config:
```bash
inspire config show
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `INSPIRE_USERNAME` | Platform username |
| `INSPIRE_PASSWORD` | Platform password |
| `INSPIRE_BASE_URL` | API base URL |
| `INSPIRE_TARGET_DIR` | Shared filesystem path |
| `INSPIRE_WORKSPACE_ID` | Default workspace ID |
| `INSPIRE_PROJECT_ID` | Default project ID |
| `INSP_IMAGE` | Default Docker image |
| `INSP_PRIORITY` | Job priority (1-10) |
