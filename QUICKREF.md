# Inspire CLI (from laptop)

CLI to submit GPU training jobs to Inspire HPC platform remotely.

## Workflow

```bash
inspire sync                          # Push code to Bridge
inspire job create --name "exp" --resource "4xH200" --command "bash train.sh"
inspire job logs <job-id> --tail 100  # View logs
inspire job stop <job-id>             # Stop if needed
```

## Key Commands

- `inspire sync [--force]` — Sync code to Bridge
- `inspire job create --name X --resource "4xH200" --command "..."` — Submit job
- `inspire job status/logs/stop <job-id>` — Manage job
- `inspire job list` — List cached jobs
- `inspire resources list` — Show available GPUs
- `inspire bridge exec "cmd"` — Run shell command on Bridge

Use `--json` for machine-readable output.
