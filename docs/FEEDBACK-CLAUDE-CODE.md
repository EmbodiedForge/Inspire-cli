# Inspire CLI Feedback from Claude Code Usage

> Feedback collected during an extended SPRINT profiling session using `inspire-cli` with Claude Code (2026-01-10)

## Executive Summary

After 50+ job submissions, syncs, and log fetches during a deep learning profiling session, here are the key friction points and improvement suggestions.

---

## Pain Points

### 1. ~~Command Quoting / Shell Compatibility~~ (Fixed)

**Status**: Resolved in v0.2.4

Commands are now automatically wrapped in `bash -c '...'`. No manual wrapping needed:

```bash
# Now works directly:
inspire job create --command "cd /path && source .env && python script.py"
```

Commands already wrapped in `bash -c` or `sh -c` are detected and not double-wrapped.

---

### 2. Log Fetching Latency (Medium Priority)

**Problem**: Every log fetch goes through Gitea workflow.

```
Fetching remote log via Gitea workflow (first fetch may take ~10-30s)...
```

**Impact**: When debugging a failed job, waiting 10-30s per log fetch adds up quickly. In a session with 10+ failed jobs, this was 5+ minutes of waiting.

**Suggestions**:
- Direct SSH/file access for logs when possible
- Cache recent logs locally
- Background pre-fetch for active jobs
- True streaming with `--stream` flag

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

**Suggestion**: One-liner execution:

```bash
# Proposed:
inspire run scripts/test.py --resource 1xH100

# Auto: commit pending changes, sync, create job, wait, stream logs
```

**Bonus features**:
- `--no-commit` to skip auto-commit
- `--background` to not wait
- `--torchrun 8` for distributed

---

### 4. Missing Job Templates (Medium Priority)

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

### 5. Job History / Comparison (Low Priority)

**Problem**: Hard to compare results across multiple profiling runs.

**Suggestions**:
- `inspire job history --name "sprint-*"` - list jobs by pattern
- `inspire job diff <id1> <id2>` - compare outputs
- `inspire job stats` - GPU utilization, duration trends

---

## What Works Well

| Feature | Notes |
|---------|-------|
| `inspire sync` | Reliable, good uncommitted changes warning |
| `inspire job wait` | Handles connection errors gracefully, clean progress display |
| Resource matching | `8xH100` auto-resolves to correct spec ID |
| Job status display | Clear, informative output |
| Error recovery | Timeouts/connection errors handled with retries |

---

## Priority Summary

| Priority | Issue | Effort | Impact | Status |
|----------|-------|--------|--------|--------|
| ~~P0~~ | ~~Auto bash wrapper~~ | ~~Low~~ | ~~High~~ | **Done** |
| P0 | `inspire run` one-liner | Medium | High | |
| P1 | Faster log access | Medium | Medium | |
| P1 | Job templates | Medium | Medium | |
| P2 | Job history/comparison | High | Low | |

---

## Session Stats

- **Jobs created**: 50+
- **Failed due to shell issues**: 5
- **Time spent on log fetching**: ~10 min
- **Repeated boilerplate commands**: 20+

---

## Appendix: Example Session Flow

```bash
# What I did 10+ times:
git add . && git commit -m "Add profiling script"
inspire sync
inspire job create --name "sprint-profile-v3" --resource "1xH100" \
  --command 'bash -c "cd /inspire/.../JiT && source .venv/bin/activate && python scripts/profile.py"'
inspire job wait job-xxxxx
inspire job logs job-xxxxx --tail 100

# What I wish I could do:
inspire run scripts/profile.py --resource 1xH100 --sync
```
