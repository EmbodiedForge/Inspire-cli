---
name: shared
version: 1.3.2
description: Cross-cutting rules for driving the inspire CLI from an agent - JSON envelopes, exit codes, risk levels and the --yes gate, --dry-run, deprecated entry points, ID conventions, and the `inspire api` escape hatch. Read this before any job, notebook, model, image, storage, bridge, or config work; the per-domain skills assume it and do not repeat it.
metadata: {cliHelp: "inspire --help", requires: {bins: ["inspire"]}}
---

# inspire: shared rules

Every `inspire` command follows one contract, pinned by the CLI's own test suite: deviating from it in
a script is a bug in the script, not a quirk of the CLI.

## Global flags

```
inspire [--json] [--account ALIAS] [--debug] <group> <command> [args]
```

- `--json` is **global and goes before the group**: `inspire --json job list`, never
  `inspire job list --json`.
- `--account ALIAS` picks a configured account for that one invocation.
- `--debug` adds diagnostics on stderr; the shape of stdout does not change.

## Envelopes

Success — stdout only:

```json
{ "success": true, "data": ... }
```

Failure — stderr only, stdout empty:

```json
{ "success": false, "error": { "type": "ConfigError", "code": 10, "message": "...", "hint": "..." } }
```

- Parse stdout **only** when the exit code is 0; on non-zero, read stderr for the error.
- `hint`, when present, is the next action to take — usually the flag to add. It is not an explanation.
- Credential-shaped fields and known session secrets come back as `"<redacted>"`. This is not a
  full safety projection: `inspire api` returns the platform's complete records, which can include
  commands, environment values, mounts, and creator fields. Never print or retain a whole raw payload.
- Progress, spinners, and logs go to stderr in both modes, so stdout stays parseable.

## Exit codes

| Code | Typical `error.type` | Meaning and what to do |
|---|---|---|
| 0 | — | Success. |
| 1 | anything | General/unspecified failure; read `error.message`. |
| 2 | click's own name | Usage error (unknown option, missing argument). Re-read `inspire schema <path>`; do not retry unchanged. |
| 10 | `ConfigError` | No or invalid configuration. Run `inspire init`. |
| 11 | `AuthenticationError` | Session dead. Re-authenticate, then retry once. |
| 12 | `ValidationError`, `AmbiguousID` | Bad input value, a missing `--yes` on a destructive call, or a partial ID that matched nothing or several. |
| 13 | `APIError`, `Blind` | The platform rejected the call (its code and message are echoed in `error.message`), or a wait/follow loop lost the ability to observe — see BLIND below. |
| 14 | `Timeout` | The deadline passed and the work may still be running platform-side. Re-check state instead of re-issuing a write. |
| 15 | — | Logs for that resource do not exist (yet). |
| 16 | `NotFound`, `JobNotFound` | That resource does not exist. |

## List payloads

List commands answer the canonical object, never a bare array, and the CLI filters nothing — pipe it
to `jq`: `inspire --json job list | jq -r '.data.items[].job_id'`.

```json
{ "success": true, "data": { "items": [...], "total": N } }
```

Documented exceptions: `resources list` answers `{"availability": [...], "warnings": [...]}`;
`tunnel list` and `bridge list` answer `{"bridges": [...], "default": ...}` from local config;
`job list --watch --json` streams one event object per tick. On a *partial* platform failure a command
answers success with the gap named in `data.warnings` and exit 0 — check `warnings` before concluding
"nothing there". A *total* failure is an error envelope.

A workspace plan (`job availability`) carries `coverage` and `failures` beside `warnings`: `coverage`
counts the rows it actually read against the ones that failed, `failures` names each gap. A group whose
numbers could not be read is a gap, never a row of zeros and never an empty table.

## Deprecated entry points

A deprecated command still works, stays registered, and keeps its compatibility behaviour; it is simply
not the thing to choose. `--help` tags it `[deprecated]` and names the replacement, `inspire schema`
carries the same text as `deprecated_note`, and invoking it warns once on stderr — reading it
(`--help`, `inspire schema`) stays warning-free. No note claims a removal version, because none has been
decided.

- `inspire run` → sync with `inspire sync`, then submit with `inspire job create` (the job skill carries
  the plan → dry-run → submit sequence).
- `inspire job quota` → `inspire job offers`, which reads the same Scheduling Offer rows.

Under `--json`, a deprecation warning and a following command error are separate JSON envelopes on
stderr (warning first), separated by whitespace. Do not parse the whole stream with one `json.loads`;
use the replacement command instead of building new automation on a deprecated entry point.

## Risk levels and `--yes`

Every command carries one risk level, visible in `--help` and in `inspire schema`:

- `read` — nothing changes.
- `write` — creates or modifies (create, update, upload, start).
- `high-risk-write` — destroys or interrupts (delete, stop, scale to zero): needs `--yes` (alias
  `--force`) unless a real TTY is there to confirm in.

`--json` mode **never prompts**: a destructive command without `--yes` fails with exit 12 and a hint
naming the flag. That is the gate working, not a bug to argue with — add `--yes` only when destroying
that resource is the plan. Declining an interactive prompt also exits non-zero.

## `--dry-run`

`write` commands that create resources accept `--dry-run`: the exact platform request is printed
(secrets redacted) and nothing is sent, exit 0. Check a generated payload before spending GPU on it.

## Escape hatch

The CLI supplies the authenticated session; the caller supplies the request or browser script. This
is the route when the console has moved past a wrapper, not a reason to invent fields or a parallel
planner. Prefer a real command when one covers the operation.

**One raw JSON request**

```bash
inspire api POST /api/v2/train?Action=ListJobs --data '{"page_num": 1, "page_size": 10, "workspace_id": "ws-..."}'
inspire api PUT /api/v2/example --data @request.json
inspire --json api DELETE /api/v2/example --data @- < request.json
```

`GET`, `POST`, `PUT` and `DELETE` use the CLI's web login, auth refresh and bounded 429 retries. A
full `/api/...` path is sent unchanged; a shorthand is placed under the configured browser-API
prefix. `--params` takes an object. `--data` takes an object or array, including an empty array;
both options accept `@file` or `@-` for stdin. A raw request returns the response body as `.data`
under `--json`. Replace the `ws-...` placeholder with the selected workspace's full ID. Required
Action fields must be in `--data`; for example `ListJobs` needs `workspace_id`, `page_num` and
`page_size`, and returns `.data.Result.jobs` / `.data.Result.total`. Raw records are the full
platform rows, not the narrowed field set printed by the wrapped `job list`.

**An authenticated console page, with arbitrary JavaScript**

```bash
inspire --json api eval '(async () => {
  const response = await fetch("/api/v2/train?Action=ListJobs", {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"},
    body: JSON.stringify({page_num: 1, page_size: 10, workspace_id: "ws-..."})
  });
  return {http_status: response.status, body: await response.json(), title: document.title};
})()'
inspire --json api eval @script.js
inspire --json api eval @- < script.js
inspire api eval --url https://qz.sii.edu.cn/jobs --timeout 60 @script.js
# Training Actions are mounted on the training UI, not the chat SPA:
inspire --json api eval --url 'https://qz.sii.edu.cn/jobs/spacesOverview?spaceId=ws-...' @script.js
```

`eval` opens a temporary Playwright page carrying the CLI login state. The script can use arbitrary
`fetch` methods, headers and bodies, read the DOM, and combine several requests; return a
JSON-serializable value, which becomes `.data`. Promises are awaited up to `--timeout` seconds. The
initial `--url` must belong to the configured console origin, and the temporary browser closes after
the invocation. Some `/api/v2` Actions are served only from the training UI: pass
`--url 'https://qz.sii.edu.cn/jobs/spacesOverview?spaceId=ws-...'`; the chat SPA at `/` can 404 the
same Action. Install the `browser` extra and its Chromium runtime before using this mode.

For an installed `uv` tool, install Chromium in the tool's own environment:

```bash
"$(uv tool dir)/inspire-cli/bin/playwright" install chromium
```

If the tool lacks Playwright, first reinstall it from the canonical Git source:

```bash
uv tool install --force --refresh 'inspire-cli[browser] @ git+https://codeberg.org/cyteena/inspire-cli.git'
```

A wheel upgrade needs the same extra; without it the tool is left with the base dependencies only:

```bash
uv tool install --force --reinstall --with playwright <wheel>
```

From a source checkout:

```bash
uv sync --extra browser
uv run playwright install chromium
```

**Explore an unknown contract with the same eval command**

There is no separate discovery step or command. Inspect the authenticated page directly instead of
guessing a request:

```bash
inspire --json api eval '({
  title: document.title,
  dom: document.body ? document.body.innerText.slice(0, 2000) : "",
  scripts: [...document.scripts].map(script => script.src).filter(Boolean),
  api_resources: performance.getEntriesByType("resource")
    .map(entry => entry.name).filter(url => url.includes("/api/")).slice(0, 50)
})'
```

Fetch a relevant script URL from this page and return a bounded excerpt around an Action or request
builder; use `@script.js` for a larger source walk. To observe subsequent page behaviour, wrap
`window.fetch` or XHR in caller JavaScript before the DOM interaction and return the request
arguments needed as evidence. This is best-effort console evidence, not a formal schema: absence
from one page or asset does not prove an API is unavailable, and it never justifies trial mutations.

There is no separate read-only capture mode. Raw requests and `api eval` have **no read-only route
guard**: both can create, modify, stop or delete resources, and neither is a dry run. Raw `DELETE`
requests and eval writes have no automatic `--yes` confirmation gate. Keep exploration to intended
reads and run writes only with explicit intent. After a timeout or disconnect, requests may already
have been sent; inspect platform state before repeating them. Raw responses and eval return values
redact credential-shaped fields and known session secrets, but the caller owns what a script returns.

## IDs, fields, statuses

- Field names are `snake_case`; timestamps are ISO 8601 strings.
- IDs are full platform IDs: `job-<uuid>`, `nb-<uuid>`, `serving-<uuid>`. Wherever an ID is accepted,
  an **unambiguous prefix** works; a prefix matching nothing or several is an `AmbiguousID` error
  listing the candidates — never a guess.
- Status fields carry the platform's own spelling (e.g. `job_stopped`). Do not string-match raw
  statuses; compare against the documented set or canonicalize first.

## BLIND: unobserved is not quiet

Exit 13 with `error.type: "Blind"` means a `--watch` / `--follow` loop lost the ability to observe.
The window it covers is **unknown**, not empty: the job did not "stay quiet", you stopped seeing it.
Never read a blind stretch as a passing check: re-poll `job status` / `job logs` before concluding
anything from absence.

## Discovering the rest

- `inspire schema` — every command, its risk level, its parameters; `inspire schema job list` for one.
- `inspire skills list` — the skill docs shipped with this CLI version.
- `inspire skills read platform` — platform semantics (workload types, egress zones, storage
  pools, billing, priority contracts, idle reclamation) that the group skills assume and do not
  repeat. Read it before any create/choose/where-do-I-put-it decision.
