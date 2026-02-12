---
name: check-ci
description: Check Codeberg Forgejo Actions CI status for this repo
allowed-tools: Bash
---

# Check Codeberg CI Status

Check the latest Forgejo Actions CI runs for the inspire-cli repo on Codeberg.

## Steps

### 1. Fetch recent workflow runs

```bash
curl -sL 'https://codeberg.org/api/v1/repos/cyteena/inspire-cli/actions/tasks?limit=10' 2>&1
```

Parse the JSON response. Each entry in `workflow_runs` has: `id`, `name` (job name), `status`, `workflow_id`, `display_title`, `run_number`, `url`, `created_at`.

Group runs by `run_number` and report the most recent run first.

### 2. Get per-step details for failed jobs

For each failed run, fetch the run page and extract the embedded state JSON:

```bash
curl -sL 'https://codeberg.org/cyteena/inspire-cli/actions/runs/{RUN_NUMBER}/jobs/0' 2>&1
```

The page HTML contains a `data-` attribute with an embedded JSON blob. Extract it with:

```python
import re, html as htmlmod, json
match = re.search(r'&#34;currentJob&#34;:\{(.*?)&#34;logs&#34;', raw_html)
decoded = htmlmod.unescape(match.group(0))
```

This JSON contains `currentJob.steps[]` with `summary`, `status`, and `duration` for each step.

To get steps for additional jobs in the same run, increment the job index (`/jobs/1`, `/jobs/2`, etc.).

### 3. Report results

Present a clear summary:

```
Run #N: <commit title> (<status>)
  lint (<duration>):
    ✓ Set up job (3s)
    ✓ checkout (1s)
    ✓ Install uv (3s)
    ✗ Lint (11s)          <-- highlight failures
    · Check formatting    <-- skipped
  test (<duration>):
    ✓ Set up job (3s)
    ...
```

If $ARGUMENTS contains a run number, show details for that specific run.
Otherwise show the latest run, plus a summary line for the 2-3 runs before it.

## Notes

- The Codeberg Forgejo API does not expose job logs publicly (no auth token configured). Step-level pass/fail and duration is the most detail available.
- Run pages require JavaScript to render fully; the embedded JSON in the initial HTML is the reliable data source.
- Workflow files: `ci.yml` (lint + test), `release.yml` (tag validation), `deps-check.yml` (weekly).
