---
name: live-test-notebook-hop
description: Run local live tests for notebook SSH on Inspire using isolated temp config and existing running notebooks. Use when Codex needs to verify or debug cold tunnel setup, local -> CPU notebook SSH, CPU notebook -> GPU notebook SSH, random rtunnel port selection, temp config discovery, or best-effort cleanup of only the test-launched rtunnel listener.
---

# Live Test: Notebook SSH Hop

Use this skill for real local platform checks of the notebook SSH path.

This workflow is:
- local, not hosted CI
- cold on the client side
- warm on the notebook side

That means:
- use a fresh temp `HOME`, temp project dir, temp global config, temp SSH key, and temp local `rtunnel`
- reuse existing running notebooks instead of creating new ones
- treat each run as a first-time tunnel build from the client point of view

## When to use it

Use this skill when you need to:
- verify `inspire notebook ssh` against a running CPU or GPU notebook
- verify the two-hop path:
  - local -> CPU notebook
  - CPU notebook -> GPU notebook
- reproduce browser/rtunnel/SSH failures on the real platform
- test mirror or `rtunnel_bin` config on an actual notebook

Do not use this skill for pure code review or unit-test-only work.

## Core rules

1. Run the cold live test locally.
- Do not assume a hosted runner can reach the Inspire platform or VPN-only endpoints reliably.

2. Use isolated temp config every time.
- Strip inherited `INSPIRE_*` variables with `env -i`.
- Set `INSPIRE_GLOBAL_CONFIG_PATH` to a temp file.
- Set the shell cwd to the temp project dir before running `inspire init --discover`.
- Never run `inspire init --discover` from the repo root unless you intentionally want to rewrite that repo's `./.inspire/config.toml`.
- Make sure no parent directory above the temp project has its own `.inspire/config.toml` layer.

3. Reuse existing running notebooks.
- The cold part is the local tunnel/bootstrap state, not notebook creation.

4. Use a fresh random rtunnel port per run.
- Reusing fixed ports on long-running notebooks causes `bind: address already in use`.

5. After a successful test, clean up only the rtunnel listener created by that test.
- Kill only the process listening on that test port if it is actually `rtunnel`.
- Remove the saved temp bridge profile.
- Do not kill unrelated `rtunnel` listeners.

## Preferred local layout

Use a dedicated temp root per run, for example:

```bash
/tmp/inspire-cold-cpu-XXXXXX
/tmp/inspire-cold-gpu-XXXXXX
```

Inside each root:

```text
home/
project/
```

Parent-layer guardrail:
- Config loading walks up parent directories for `./.inspire/config.toml`.
- If `/tmp/.inspire/config.toml` or another parent-level project config exists, it will contaminate the temp test.
- Before the run, verify the temp root's parents do not contain a stray `.inspire` layer you did not intend to test.

Important temp paths:
- global config: `HOME/.config/inspire/config.toml`
- bridge config: `HOME/.inspire/bridges-<account>.json`
- local SSH key: `HOME/.ssh/id_ed25519`
- local rtunnel: `HOME/.local/bin/rtunnel`

## Local bootstrap pattern

Use a clean environment for every local Inspire command:

```bash
env -i \
  HOME=/tmp/inspire-cold-.../home \
  PATH=/usr/local/bin:/usr/bin:/bin:/home/ubuntu/.local/bin \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  TERM=xterm \
  PLAYWRIGHT_BROWSERS_PATH=/home/ubuntu/.cache/ms-playwright \
  INSPIRE_GLOBAL_CONFIG_PATH=/tmp/inspire-cold-.../home/.config/inspire/config.toml \
  INSPIRE_USERNAME=<account> \
  INSPIRE_PASSWORD='<password>' \
  uv run --project /home/ubuntu/Coding/inspire-cli inspire <command>
```

Bootstrap steps:

1. Create temp directories.

```bash
mkdir -p "$HOME/.config/inspire" "$PROJECT_DIR"
```

2. Run discovery.

```bash
cd "$PROJECT_DIR"
uv run --project /home/ubuntu/Coding/inspire-cli \
  inspire init --discover --force --username <account> --base-url https://qz.sii.edu.cn
```

Important:
- `init --discover` writes the project config to `./.inspire/config.toml` relative to the current working directory.
- If you run it from `/home/ubuntu/Coding/inspire-cli` by mistake, it will rewrite that repo's local `.inspire/config.toml`.
- If that happens, stop and inspect/restore the repo-local file before continuing. Do not repeat the mistake and create more conflicting writes.

3. Generate a temp SSH key.

```bash
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
ssh-keygen -t ed25519 -N "" -f "$HOME/.ssh/id_ed25519" -C cold-test
```

4. Ensure temp local `rtunnel` exists.
- Prefer copying a known-good local binary into `HOME/.local/bin/rtunnel`.
- Do not rely on downloading it mid-test if avoidable.

```bash
mkdir -p "$HOME/.local/bin"
cp /home/ubuntu/.local/bin/rtunnel "$HOME/.local/bin/rtunnel"
chmod +x "$HOME/.local/bin/rtunnel"
```

## Cold local -> notebook SSH test

Use notebook ID directly when possible.

CPU example:

```bash
uv run --project /home/ubuntu/Coding/inspire-cli \
  inspire --debug notebook ssh <cpu-notebook-id> \
  --save-as ci-cold-cpu \
  --port <random-port> \
  --command 'echo CI_COLD_CPU_OK && hostname && whoami'
```

GPU example:

```bash
INSPIRE_APT_MIRROR_URL=http://nexus.sii.shaipower.online/repository/ \
INSPIRE_RTUNNEL_BIN=/inspire/qb-ilm/project/cq-scientific-cooperation-zone/public/_tools/ssh-deps/rtunnel \
INSPIRE_RTUNNEL_UPLOAD_POLICY=never \
uv run --project /home/ubuntu/Coding/inspire-cli \
  inspire --debug notebook ssh <gpu-notebook-id> \
  --save-as ci-cold-gpu \
  --port <random-port> \
  --command 'echo CI_COLD_GPU_OK && hostname && whoami'
```

Expected success shape:
- setup script sent via Jupyter terminal WebSocket
- bridge added
- command prints marker, hostname, and `root`

## Two-hop test: local -> CPU -> GPU

After the first-hop CPU bridge works:

1. Use `bridge exec` for CPU-side prep.

```bash
uv run --project /home/ubuntu/Coding/inspire-cli \
  inspire bridge exec -b <cpu-bridge> '<remote command>'
```

2. On the CPU notebook, sync the repo clone if needed.

Typical remote clone:

```bash
/inspire/qb-ilm/project/cq-scientific-cooperation-zone/w26212/inspire-cli
```

3. Ensure CPU-side config is correct.
- set `INSPIRE_GLOBAL_CONFIG_PATH` for the CPU-side temp config
- add missing project-specific GPU workspace aliases if required
- set:
  - `apt_mirror_url = "http://nexus.sii.shaipower.online/repository/"`
  - `rtunnel_bin = ".../rtunnel"`
  - `rtunnel_upload_policy = "never"`

4. Run the second hop from inside the CPU notebook.

```bash
/tmp/.../.venv/bin/inspire --debug notebook ssh <gpu-notebook-id> \
  --save-as <gpu-bridge> \
  --port <random-port> \
  --command 'echo CPU_TO_GPU_OK && hostname && whoami'
```

## Random port rule

Always choose a fresh high port per run, for example `39000-48999`.

Reason:
- repeated cold tests on warm notebooks leave prior listeners around
- fixed ports like `39016` or `39017` collide easily
- the failure shape is:
  - `/tmp/rtunnel-server.log`
  - `listen tcp 0.0.0.0:<port>: bind: address already in use`

If that specific collision still happens:
- rerun with a different port
- treat it as a test harness issue first, not immediately a product regression

## Cleanup rule

After a successful test:

1. Remove the temp saved bridge locally.
2. Best-effort kill only the remote listener created for the test port.

Safe cleanup shape:
- inspect the PID listening on the chosen port
- only kill it if the process name is `rtunnel`

Do not:
- kill all `rtunnel` processes
- clean up unrelated listeners on other ports

## How to interpret noisy HTTP probe output

HTTP proxy readiness is advisory only.

You may still see:
- `404 page not found`
- `ECONNREFUSED`

If the final SSH command succeeds, treat the test as successful.

Authoritative signals:
- `inspire notebook ssh ... --command ...` succeeds
- or `inspire tunnel test -b <bridge>` succeeds
- or `inspire bridge exec -b <bridge> ...` succeeds

## Known failure classes

### Port collision

Symptoms:
- setup completes
- doctor/log shows `bind: address already in use`

Action:
- rerun with a new random port

### Missing local rtunnel in fresh temp home

Symptoms:
- local side stalls or tries to download `rtunnel`

Action:
- pre-seed `HOME/.local/bin/rtunnel`

### Wrong account due to leaked env

Symptoms:
- wrong project, workspace, or target dir
- notebook/account mismatch errors

Action:
- rerun with `env -i`
- confirm `inspire config show`

### Discovery run from the wrong working directory

Symptoms:
- `init --discover` reports success, but writes project config under the repo you launched from instead of the temp project dir
- for example, `/home/ubuntu/Coding/inspire-cli/.inspire/config.toml` changes unexpectedly

Action:
- always `cd "$PROJECT_DIR"` before running `init --discover`
- inspect and restore the mistakenly written repo-local `.inspire/config.toml` before continuing
- do not rerun until the working directory is corrected

### Missing project-specific GPU workspace alias

Symptoms:
- `notebook list --workspace gpu` shows nothing
- direct notebook ID still works

Action:
- add the project-specific workspace alias in temp config

### Missing Playwright browser on the machine that drives browser automation

Symptoms:
- `BrowserType.launch: Executable doesn't exist`

Action:
- install Chromium with Playwright in the environment used for the test

## Completion criteria

The live test is successful when:
- the cold local client path starts from empty temp config/state
- local -> notebook SSH succeeds for the intended notebook
- for hop tests, CPU -> GPU SSH also succeeds
- the command output includes the expected marker, hostname, and user
- only the test-specific bridge and test-specific rtunnel listener are cleaned up afterward
