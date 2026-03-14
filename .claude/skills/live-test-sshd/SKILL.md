---
name: live-test-sshd
description: Live test sshd failure detection on a GPU notebook with no internet. Creates an isolated config environment, provisions a notebook, and verifies the INSPIRE_SSHD_INSTALL_FAILED marker is detected.
allowed-tools: Bash(uv run inspire *), Bash(env *), Bash(mkdir *), Bash(ls *), Bash(rm *), Bash(cat *), Read, Write, Edit
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

### 2. Write minimal global config

Write `/tmp/inspire-live-test/global-config.toml` with just credentials and API settings:

```toml
[accounts.ACCOUNT_USERNAME]
password = "the-password"

[api]
auth_endpoint = "/auth/token"
base_url = "https://platform.example.com"
browser_api_prefix = "/api/v1"
docker_registry = "docker.example.com"
openapi_prefix = "/openapi/v1"
```

Discovery (step 4) will populate project catalog, workspaces, and compute groups.

**Important:** Do NOT include a `[ssh]` section. The test relies on sshd installation failing because there is no mirror/deb config.

### 3. Run discovery

Run `inspire init --discover` to populate the config. Pass the password via env var since `--discover` prompts interactively:

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
- Project config has `[auth]` with username
- Neither config has a `[ssh]` section

### 4. Create a GPU notebook

Use the no-internet GPU workspace:

```bash
cd /tmp/inspire-live-test && \
env -i HOME="$HOME" PATH="$PATH" TERM="$TERM" LANG="${LANG:-en_US.UTF-8}" \
    SHELL="$SHELL" USER="$USER" \
    INSPIRE_GLOBAL_CONFIG_PATH=/tmp/inspire-live-test/global-config.toml \
    uv run inspire notebook create --workspace gpu -r 1xH200 -i ubuntu-inspire-base:22.04 --no-wait
```

Wait for the notebook to reach `running` state:

```bash
cd /tmp/inspire-live-test && \
env -i HOME="$HOME" PATH="$PATH" TERM="$TERM" LANG="${LANG:-en_US.UTF-8}" \
    SHELL="$SHELL" USER="$USER" \
    INSPIRE_GLOBAL_CONFIG_PATH=/tmp/inspire-live-test/global-config.toml \
    uv run inspire notebook status <NOTEBOOK_ID>
```

### 5. Trigger SSH connection (the actual test)

```bash
cd /tmp/inspire-live-test && \
env -i HOME="$HOME" PATH="$PATH" TERM="$TERM" LANG="${LANG:-en_US.UTF-8}" \
    SHELL="$SHELL" USER="$USER" \
    INSPIRE_GLOBAL_CONFIG_PATH=/tmp/inspire-live-test/global-config.toml \
    INSPIRE_RTUNNEL_TIMING=1 \
    uv run inspire notebook ssh <NOTEBOOK_ID>
```

### 6. Evaluate results

**Success (marker detected via WebSocket):**
The command raises a `RuntimeError` with a message containing:
- "SSH server (sshd) could not be installed on the notebook"
- "apt_mirror_url"
- "sshd_deb_dir"

This means the WS terminal path successfully captured the `INSPIRE_SSHD_INSTALL_FAILED` marker from the shell script output.

**Known limitation (WS unavailable):**
If the WebSocket terminal path fails entirely (can't create a terminal), the browser-automation fallback sends the script but cannot capture stdout. In this case, the error degrades to a generic proxy/SSH timeout. This is the expected behavior for the known limitation documented in the codebase.

**Diagnostic tips:**
- Add `INSPIRE_RTUNNEL_TIMING=1` to see per-step timing
- Add `-v` or `-vv` to the inspire command for debug logging
- Check `/tmp/notebook_terminal_debug.png` for a screenshot of the terminal state

### 7. Cleanup

Stop the notebook to free GPU resources:

```bash
cd /tmp/inspire-live-test && \
env -i HOME="$HOME" PATH="$PATH" TERM="$TERM" LANG="${LANG:-en_US.UTF-8}" \
    SHELL="$SHELL" USER="$USER" \
    INSPIRE_GLOBAL_CONFIG_PATH=/tmp/inspire-live-test/global-config.toml \
    uv run inspire notebook stop <NOTEBOOK_ID>
```

Optionally remove the test directory:

```bash
rm -rf /tmp/inspire-live-test
```

## Notes

- GPU notebooks cost compute resources — always clean up after testing
- The test only validates the default openssh path (no `[ssh]` config). The dropbear path is not tested here because it requires `apt_mirror_url` or `dropbear_deb_dir` to be configured
- If you need to test with a specific account, update the global config accordingly
- The `ubuntu-inspire-base:22.04` image does NOT have openssh-server pre-installed, making it suitable for this test
- The `[[compute_groups]]` entries are auto-populated by `--discover` and match the account's available resources
