---
name: live-test-sshd
description: Live test sshd failure detection on a GPU notebook with no internet. Creates an isolated config environment, provisions a notebook, and verifies the INSPIRE_SSHD_INSTALL_FAILED marker is detected.
allowed-tools: Bash(uv run inspire *), Bash(env *), Bash(mkdir *), Bash(ls *), Bash(rm *), Bash(cat *), Bash(grep *), Bash(sleep *), Read, Write, Edit
---

# Live Test: sshd Installation Failure Detection

End-to-end test that the `INSPIRE_SSHD_INSTALL_FAILED` marker is detected when a GPU notebook has no internet and no `[ssh]` config (no `apt_mirror_url` or `sshd_deb_dir`).

## Prerequisites

- Account credentials with access to a no-internet GPU workspace
- The account must have at least one project with GPU quota
- Playwright must be installed (`uv run playwright install chromium`)

## Environment Isolation

All commands run in an isolated config environment to avoid contaminating or being contaminated by the host's inspire config. Use `/tmp/inspire-live-test/` as the working directory.

**Critical:** You MUST strip ALL `INSPIRE_*` environment variables, not just a few. The host may have `INSPIRE_RTUNNEL_BIN`, `INSPIRE_DROPBEAR_DEB_DIR`, `INSPIRE_SETUP_SCRIPT`, etc. which cause the code to take the dropbear path instead of the default openssh path, making the sshd marker never fire.

Use `env -i` to start with a completely clean environment:

```bash
cd /tmp/inspire-live-test && \
env -i HOME="$HOME" PATH="$PATH" TERM="$TERM" LANG="${LANG:-en_US.UTF-8}" \
    SHELL="$SHELL" USER="$USER" \
    INSPIRE_GLOBAL_CONFIG_PATH=/tmp/inspire-live-test/global-config.toml \
    uv run inspire <command>
```

**Why `env -i` instead of `env -u`:** Using `env -u INSPIRE_USERNAME -u INSPIRE_PASSWORD ...` is fragile — it only unsets the vars you list. If ANY SSH-related `INSPIRE_*` var leaks through (e.g. `INSPIRE_DROPBEAR_DEB_DIR`), it changes which code path the script takes and the marker is never generated.

## Steps

### 1. Create isolated config directory

```bash
rm -rf /tmp/inspire-live-test && mkdir -p /tmp/inspire-live-test/.inspire
```

### 2. Write bootstrap config and run discovery

Read the account credentials and `[api]` section from the host's `~/.config/inspire/config.toml`. Write a minimal bootstrap config with ONLY:
- `[accounts.ACCOUNT_USERNAME]` with password
- `[api]` section (copy from host config)

**Important:** Do NOT include a `[ssh]` section. The test relies on sshd installation failing because there is no mirror/deb config.

Write the project config at `/tmp/inspire-live-test/.inspire/config.toml`:
```toml
[auth]
username = "ACCOUNT_USERNAME"
```

Then run discovery to populate workspaces, compute groups, and project catalog:

```bash
cd /tmp/inspire-live-test && \
env -i HOME="$HOME" PATH="$PATH" TERM="$TERM" LANG="${LANG:-en_US.UTF-8}" \
    SHELL="$SHELL" USER="$USER" \
    INSPIRE_GLOBAL_CONFIG_PATH=/tmp/inspire-live-test/global-config.toml \
    INSPIRE_PASSWORD='the-password' \
    uv run inspire init --discover -u ACCOUNT_USERNAME --force
```

After discovery, verify:
- Global config has `[workspaces]` with a `gpu` workspace (no internet)
- Global config has `[[compute_groups]]` with GPU entries
- Project config has `[context]` with account and project
- Neither config has a `[ssh]` section

### 3. Create a GPU notebook

Use the no-internet GPU workspace:

```bash
cd /tmp/inspire-live-test && \
env -i HOME="$HOME" PATH="$PATH" TERM="$TERM" LANG="${LANG:-en_US.UTF-8}" \
    SHELL="$SHELL" USER="$USER" \
    INSPIRE_GLOBAL_CONFIG_PATH=/tmp/inspire-live-test/global-config.toml \
    uv run inspire notebook create --workspace gpu -r 1xH200 -i ubuntu-inspire-base:22.04 --no-wait
```

Poll until the notebook reaches `RUNNING` state (typically 1-3 minutes):

```bash
cd /tmp/inspire-live-test && \
env -i HOME="$HOME" PATH="$PATH" TERM="$TERM" LANG="${LANG:-en_US.UTF-8}" \
    SHELL="$SHELL" USER="$USER" \
    INSPIRE_GLOBAL_CONFIG_PATH=/tmp/inspire-live-test/global-config.toml \
    uv run inspire notebook status <NOTEBOOK_ID>
```

### 4. Trigger SSH connection (the actual test)

```bash
cd /tmp/inspire-live-test && \
env -i HOME="$HOME" PATH="$PATH" TERM="$TERM" LANG="${LANG:-en_US.UTF-8}" \
    SHELL="$SHELL" USER="$USER" \
    INSPIRE_GLOBAL_CONFIG_PATH=/tmp/inspire-live-test/global-config.toml \
    INSPIRE_RTUNNEL_TIMING=1 \
    uv run inspire notebook ssh <NOTEBOOK_ID>
```

### 5. Evaluate results

**Success (marker detected):**
The command raises a `RuntimeError` with a message containing:
- "SSH server (sshd) could not be installed on the notebook"
- "apt_mirror_url"
- "sshd_deb_dir"

This means the `INSPIRE_SSHD_INSTALL_FAILED` marker was captured from the shell script output. Two paths can detect this:

1. **Pure WS path:** The WebSocket terminal successfully sends the script and captures stdout. This is the fast path (typically 1-3 seconds).
2. **Hybrid browser fallback:** If the WS terminal creation fails, the browser-automation fallback sends the script via keyboard while a read-only WS output listener captures stdout markers. Look for `Attached WS output listener for marker detection.` in stderr to confirm this path was used.

**Failure (no marker detected):**
If both the WS path and the hybrid listener fail to capture stdout, the error degrades to a generic proxy/SSH timeout. In this case:
- Check stderr for `[ws-diagnostics]` lines to see where the WS connection failed
- Check `/tmp/notebook_terminal_debug.png` for a screenshot of the terminal state
- Check if `wsConnected=False` in diagnostics — this indicates the Jupyter WebSocket endpoint is unreachable

**Diagnostic tips:**
- `INSPIRE_RTUNNEL_TIMING=1` enables per-step timing
- `-v` or `-vv` enables debug logging
- `/tmp/notebook_terminal_debug.png` shows the terminal state at the time of the screenshot

### 6. Cleanup

Stop the notebook to free GPU resources:

```bash
cd /tmp/inspire-live-test && \
env -i HOME="$HOME" PATH="$PATH" TERM="$TERM" LANG="${LANG:-en_US.UTF-8}" \
    SHELL="$SHELL" USER="$USER" \
    INSPIRE_GLOBAL_CONFIG_PATH=/tmp/inspire-live-test/global-config.toml \
    uv run inspire notebook stop <NOTEBOOK_ID>
```

Remove the test directory:

```bash
rm -rf /tmp/inspire-live-test
```

## Notes

- GPU notebooks cost compute resources — always clean up after testing
- The test validates the default openssh path (no `[ssh]` config). When `apt_mirror_url` or `sshd_deb_dir` is configured, the code takes the dropbear/mirror path and the sshd marker is never emitted — that is the expected happy path
- The `ubuntu-inspire-base:22.04` image does NOT have openssh-server pre-installed, making it suitable for this test
- The `[[compute_groups]]` and `[workspaces]` entries are auto-populated by `--discover`
