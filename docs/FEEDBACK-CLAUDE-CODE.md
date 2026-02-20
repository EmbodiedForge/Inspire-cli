# Inspire CLI Feedback from Claude Code Usage

> Feedback collected across multiple extended sessions using `inspire-cli` with Claude Code (2026-01 to 2026-02)

## Executive Summary

After 100+ job submissions, notebook operations, syncs, and log fetches across profiling, training, and multi-notebook management sessions, here are the key friction points and improvement suggestions.

---

## Pain Points

### 1. ~~Command Quoting / Shell Compatibility~~ (Fixed)

**Status**: Resolved in v0.2.4

Commands are now automatically wrapped in `bash -c '...'`. No manual wrapping needed.

---

### 2. No `bridge scp` for File Transfer (High Priority)

**Problem**: There is no way to transfer files between local and remote notebooks via the bridge SSH tunnel. When `inspire sync` fails (e.g., Codeberg unreachable), there's no fallback to push code updates to the remote.

**Real scenario**: `inspire sync` hung because Codeberg was down. The workaround requires manual steps that `bridge exec` can't handle:

```bash
# Current workaround (manual, fragile):
git bundle create /tmp/jit.bundle HEAD~5..HEAD    # local
# Need to somehow get this file to the remote...
# `inspire bridge exec "cat > /tmp/jit.bundle" < /tmp/jit.bundle` doesn't work
# (stdin piping not supported through bridge exec)
```

**Proposed**:

```bash
# Upload file to notebook:
inspire bridge scp /tmp/jit.bundle /tmp/jit.bundle

# Download file from notebook:
inspire bridge scp --download /remote/path/results.tar.gz ./results.tar.gz

# Shorthand (detect direction by path prefix):
inspire bridge scp local:./file remote:/tmp/file
```

**Why this matters**:
- `inspire sync` depends on Codeberg being reachable from the CPU bridge. When it's down (happens regularly), there's no way to push code changes
- Transferring checkpoints, logs, or analysis results from notebooks requires workarounds
- The SSH tunnel already exists (`inspire notebook ssh`), so SCP should be straightforward to add

---

### 3. No Quick Script Execution (High Priority)

**Problem**: Running a simple Python script requires 6 steps:

```bash
# Current workflow:
vim scripts/test.py           # 1. Write script
git add && git commit         # 2. Commit
inspire sync                  # 3. Sync
inspire job create ...        # 4. Create job (complex command)
inspire job wait <id>         # 5. Wait
inspire job logs <id>         # 6. View logs
```

**Proposed**:

```bash
inspire run scripts/test.py --resource 1xH100
# Auto: commit pending changes, sync, create job, wait, stream logs
```

**Options**:
- `--no-commit` to skip auto-commit
- `--background` to not wait
- `--torchrun 8` for distributed

---

### 4. Log Fetching Latency (Medium Priority)

**Problem**: Every log fetch goes through Gitea workflow.

```
Fetching remote log via Gitea workflow (first fetch may take ~10-30s)...
```

**Impact**: When debugging a failed job, waiting 10-30s per log fetch adds up. In sessions with 10+ failed jobs, this is 5+ minutes of idle waiting.

**Suggestions**:
- Direct SSH/file access for logs when possible
- Cache recent logs locally
- Background pre-fetch for active jobs
- True streaming with `--stream` flag

---

### 5. Missing Job Templates (Medium Priority)

**Problem**: Repeated boilerplate for common patterns.

```bash
# Typed this 20+ times:
inspire job create --name "X" --resource "8xH100" \
  --command 'bash -c "cd /inspire/.../JiT && source .venv/bin/activate && torchrun --nproc_per_node=8 ..."'
```

**Suggestions**:
- Project-level templates in `.inspire/templates.yaml`
- `inspire job create --template training --script train.py`
- Save last job as template: `inspire job save-template <id> training`

Example template config:
```yaml
# .inspire/templates.yaml
templates:
  training:
    resource: 8xH100
    setup: |
      cd {project_dir}
      source .venv/bin/activate
    command: torchrun --nproc_per_node=8 {script}

  profile:
    resource: 1xH100
    setup: |
      cd {project_dir}
      source .venv/bin/activate
    command: python {script}
```

---

### 6. Job History / Comparison (Low Priority)

**Problem**: Hard to compare results across multiple profiling runs.

**Suggestions**:
- `inspire job history --name "sprint-*"` - list jobs by pattern
- `inspire job diff <id1> <id2>` - compare outputs
- `inspire job stats` - GPU utilization, duration trends

---

## What Works Well

| Feature | Notes |
|---------|-------|
| `inspire sync` | Reliable SSH tunnel path, good uncommitted changes warning |
| `inspire notebook create` | Clean flow: create, wait for RUNNING, auto-keepalive |
| `inspire notebook ssh --save-as` | Tunnel naming makes multi-notebook management practical |
| `inspire bridge exec` | Fast SSH path, good working dir default |
| `inspire job wait` | Handles connection errors gracefully, clean progress display |
| Resource matching | `8xH200`, `4xH100` auto-resolves to correct spec ID |
| Job status display | Clear, informative output |
| Error recovery | Timeouts/connection errors handled with retries |
| `inspire notebook status` | Shows uptime, node, priority — useful for debugging preemptions |

---

## Priority Summary

| Priority | Issue | Effort | Impact | Status |
|----------|-------|--------|--------|--------|
| ~~P0~~ | ~~Auto bash wrapper~~ | ~~Low~~ | ~~High~~ | **Done** |
| P0 | `bridge scp` file transfer | Low | High | **Done** |
| P0 | `inspire run` one-liner | Medium | High | |
| P1 | Faster log access | Medium | Medium | Partial (SSH fast-path exists) |
| P1 | Job templates | Medium | Medium | |
| P2 | Job history/comparison | High | Low | |

---

## Session Stats (cumulative)

- **Jobs created**: 100+
- **Notebooks managed concurrently**: 8
- **Sync failures (Codeberg down)**: 3
- **Failed due to shell issues**: 5 (pre-fix)
- **Time spent on log fetching**: ~15 min
- **Repeated boilerplate commands**: 30+
