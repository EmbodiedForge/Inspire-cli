# Inspire platform
- Inspire platform is a cloud computing platform, we use `inspire` this cli tool to interactive with it.

# Before You start
- run `inspire config check` to check the env config
- if lacks some env, report to user or find if there are some .env and .env.paths file exist

# Normal workflow
- modify the code
- git commit
- use `inspire sync` to sync the code at the inspire platform
- launch train task

# For Downloading
- we use Bridge machine to download the ckpt or configure the venv (for python venv, we always use uv)

# Inspire CLI – Agent Quickstart

- Run with `inspire`; add `--json` for machine output and `--debug` for verbose logs.
- Env: `INSPIRE_USERNAME`, `INSPIRE_PASSWORD`; for sync/bridge/logs also `INSPIRE_TARGET_DIR`, `INSP_GITHUB_REPO`, `INSP_GITHUB_TOKEN` (or `gh auth token`).
- Happy path:
  - `inspire config check`
  - `inspire resources list`
  - `inspire sync` (use `--remote`, `--force`, `--no-wait` as needed)
  - `inspire job create --name <n> --resource <spec> --command "<cmd>" [--priority 1-10 --max-time <hrs> ...]`
  - `inspire job status <id>` / `inspire job wait <id> --timeout <s>` / `inspire job stop <id>`
  - `inspire job logs <id> [--tail 100 | --path | --refresh]`
  - `inspire bridge exec "<cmd>" [--artifact-path <p> --download <dir> --denylist <pat> --timeout <s> --no-wait]`
  - Optional: `inspire nodes list`
- Need more detail? Run `inspire --help` or `inspire <command> --help`.
