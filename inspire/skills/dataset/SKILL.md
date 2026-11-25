---
name: dataset
version: 1.0.0
description: "SII data-square datasets: which portal knows what exists versus what may mount, why a catalogue hit proves nothing about a workspace, the second 24-hour session that has no token endpoint, and the container path a mount actually appears at. Use to answer 'does the cluster already have this dataset', to pick a version, or to attach one to a job or notebook. NOT for cluster file I/O (storage skill), NOT for publishing or approving a dataset (the web portal only)."
metadata:
  requires:
    bins: ["inspire"]
  cliHelp: "inspire dataset --help"
---

# dataset

**Read the shared skill first (`inspire skills read shared`): envelope, exit codes, ID resolution.**
Every command here is classified *read* — none can change cluster state.

## Platform Rules (what --help cannot tell you)

1. **Two portals, two sessions, and they answer different questions.** The catalogue is the
   **data square** (`aip.sii.edu.cn`); whether a version may be attached in a workspace is decided
   by the **compute platform** (`qz.sii.edu.cn`). `list`/`versions`/`get` read the first, `check`
   reads the second. A catalogue hit proves existence only — the mount verdict is per workspace and
   per version, and catalogue rows carrying `hasPermission=false` (levels S1/S2) exist precisely
   because visibility and mountability are different facts.

2. **The data-square session cannot renew itself.** It is one `datasets-session` JWT cookie that
   only that portal's login page issues: no token endpoint or refresh call. A compute-platform session
   re-authenticates through CAS on its own; a data-square miss exits 11, and its error hint gives the
   browser-login procedure for the installed environment. Follow that current hint rather than a saved
   command. The data-square cache (`datasquare_session-<account>.json`) is deliberately separate from
   `web_session*.json`, and cookies never cross hosts. Note which session each verb needs:
   `list`/`versions`/`get` want the data-square one, `check` wants the compute-platform one
   (`inspire init`) — a healthy `inspire dataset list` says nothing about `check`, and vice versa.

3. **"May I mount this" is answered per item, and silence is not a pass.** `ValidateDataset`
   returns one verdict per pair; a rejected version is a *result*, not a transport error. A pair
   with no verdict in the answer is reported as a failure, never as success
   (`the platform returned no verdict for this pair`). Under `--json`, a rejected item carries
   separate `error_code` and `error_message` fields — for example `2001` and `版本不存在` — so there
   is nothing to read off an HTTP status here. Do not treat those codes as a complete diagnosis: `2001`
   can mean a misspelled version, a dataset registered without versions, or a registered version with
   no replica yet. `dataset check` keeps the platform wording and, when a cached data-square session
   is available, adds a catalogue diagnosis such as missing access, no created version, or an
   inactive version; follow that diagnosis rather than the code alone.

4. **The platform is case-insensitive about a dataset code and exact about a version.**
   `PointWorld@v1` and `pointworld@v1` are the same dataset; `v1` is not `V1`. Do not normalise a
   version code when you build a reference.

5. **The field named `dataset_id` carries the code, not the numeric id.** In `ValidateDataset`
   bodies and in a create request's `dataset_info`, `dataset_id` holds the human code
   (`pointworld`). The catalogue's own `dataset_id` is an integer — copying it into a create body
   yields a "dataset not found" verdict, not a type error.

6. **The container path is a platform convention, not a choice.** A mounted version appears at
   `/inspire/dataset/<code>/<version>` in a job *and* in a notebook; you cannot pick a mount point.
   The platform's storage location for that version (the `path` a check reports) is a different
   string and is not what you `cd` into. `inspire dataset mount-path` computes the container path
   locally, with no session at all — safe to use inside a script that has neither portal's cookie.

7. **Create-time order is validate → create → read back, and a refusal costs you nothing.**
   `job create --dataset` / `notebook create --dataset` validate the whole set first and submit
   nothing if any single version is rejected (exit 13, carrying the platform's own wording),
   because an instance running on half its data is worse than one that never started. The job path
   additionally reads the created job back with `GetJob`: if the platform accepted `dataset_info`
   and attached none of it, that is reported as `DatasetMountNotApplied` (exit 13) rather than left
   to surface as a FileNotFoundError hours later. **(measured 2026-10-13: the v1 create endpoints
   do persist the field — the read-back guards against a regression, not a known drop.)**

8. **`check` reports, `create` refuses.** `dataset check` exits 0 even when a version is refused —
   read `all_ok` / each item's `ok` in the JSON. Only the create commands turn a refusal into a
   non-zero exit. Do not wrap `check` in "exit code means mountable".

9. **This CLI browses; it does not publish.** Our account's dataset permissions are
   `ValidateDataset` + read — there is no write surface, so nothing here creates, uploads or
   approves a dataset. Gated datasets (levels S1/S2) show `has_permission=false` and have to be
   requested through the data-square web UI first; after approval, re-run `check`.

## Applying the rules

**"Does the cluster already have this dataset?" (1, 3, 5, 9):**

```bash
inspire dataset list pointworld                    # catalogue search (code + description)
inspire dataset get pointworld                     # versions, sizes, storage paths, permission
inspire dataset versions pointworld                # just the codes, with their container paths
inspire --json dataset check pointworld@behavior --workspace-id <ws> | jq '.data.all_ok'
```

**Attach it to a run (1, 6, 7, 8):**

```bash
inspire dataset check pointworld@behavior --workspace-id <ws>   # verdict + backend path, before you spend
inspire job create -n exp-1 -r 1xH200 -c "python train.py" \
  --dataset pointworld@behavior --dry-run                       # the mount is in the previewed body
inspire job create -n exp-1 -r 1xH200 -c "python train.py" --dataset pointworld@behavior
# in the container: /inspire/dataset/pointworld/behavior
```

Repeat `--dataset` for several versions, on either `job create` or `notebook create`.

**Session dead (2):**

```bash
inspire dataset list  # exit 11 includes the current browser-login procedure in its hint
```

## Boundaries

- NOT for cluster files you own — checkpoints, logs, your own corpora live on the shared
  filesystem: **storage** and **bridge** skills. A data-square version is mounted read from the
  platform's storage; you do not browse it with `storage ls`.
- NOT for publishing, uploading, versioning or approving a dataset — data-square web UI only (rule 9).
- NOT for the workspace/quota/image decisions around the run itself — **job** / **notebook** /
  **platform** skills; this skill covers only the mount.
- NOT for envelope shape, exit codes, `--dry-run` semantics — **shared** skill.
