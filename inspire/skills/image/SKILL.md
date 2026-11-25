---
name: image
version: 1.0.0
description: "Inspire platform rules for Docker images: what --source and --visibility actually mean, why an image can be usable but not listable or not deployable, READY vs SUCCESS, what a notebook snapshot does and does not capture, and what sync/preheat do that a job cannot see. Use when choosing an image, persisting a custom environment, publishing one, or debugging image-not-found / stuck-build reports. NOT for running jobs (see job skill) or the notebook lifecycle itself (see notebook skill)."
metadata:
  requires:
    bins: ["inspire"]
  cliHelp: "inspire image --help"
---

# image

Assumes the shared skill (`inspire skills read shared`) for the envelope, exit codes, the `--yes` gate and `--dry-run`.

## Platform Rules (what --help cannot tell you)

1. **`--source` is provenance and `--visibility` is exposure — two axes that combine freely.** Behind the CLI the platform filters `filter.source_list` *and* `filter.visibility`; the ordinary outcome of saving a notebook is provenance "public" with visibility "private". A row's printed `visibility` is the view you asked for, never the row's property — so the CLI stamps it **only** where the platform pinned the view server-side (`--source public|personal-visible` or an explicit `--visibility`), emits no key otherwise, and reports the scope separately as `visibility_of_query` (issue #58). Audit exposure by **comparing views**: an image is institution-readable iff it appears under `--source public`. Never read the column. (measured 2026-09-28, earlier live image probes)

2. **`--source private` is a dead view, and an empty view is not an inventory.** `--source private` sends `source=SOURCE_PRIVATE` with no visibility filter, which can resolve to no rows even when saved images exist. Custom/saved images live under `--source personal-visible` (visibility-PRIVATE view) or `--source all`. The default view is `official`, so a monitor polling the default can report a healthy saved image as missing; the CLI prints the scope on an empty result and answers `empty_because_scope` in JSON. Re-run the relevant view rather than remembering an earlier count.

3. **Usable means `READY` *or* `SUCCESS`, and neither means the image works.** `SUCCESS` is what the live list reports, while `READY` appears in other responses. Accepting only one spelling can turn a healthy image into a false timeout. The status is a *build/registration* state: acceptance is booting from the image and running the thing, not a green row.

4. **Listable, pickable and usable-by-you are three different questions.** `search`/`describe`/`info`/`inspect` (all one picker query, workspace-scoped through `registry_hint`) answer what the create forms can pick **in that workspace**; `detail` answers what one record says. A workspace switch changes the first answer and not the second. Separately, image permission is **account**-scoped: after an account switch, a still-configured private image fails at submit with `framework_config[0]: no permission to use private image`, and `job create --dry-run` resolves the image without checking permission, so it looks healthy right up to the failed submission. (earlier live image probes)

5. **`describe`/`info`/`inspect` are aliases of `search`, not of `detail`.** They take a free-text query matched as a substring over id/name/url/version/description; `detail` takes an id. Passing an id to `describe` "works" only if the substring happens to match, and an empty table is not evidence the image is missing.

6. **A saved image is the container's writable layer and nothing else.** Everything under the mounted shared storage (`/inspire/hdd/**`, `/inspire/ssd/**`, `/etc/inspire`) is absent from the image, so a venv built on shared disk produces an image that only "works" on nodes that re-mount it. Bake environments at a container-local path (`/opt/<name>/venv`), set `PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright` *before* `playwright install` (the default cache gets cleaned out of the image), stop anything that writes before saving, and remember shell history, `~/.netrc`, credential files and Jupyter tokens ride along in the layer. A snapshot is for discovering the recipe; the Dockerfile is the artifact. (earlier live image probes, measured 2026-09-28)

7. **A tag never belongs in `--name`, and only `save` tells you so locally.** The platform composes `name:version` itself and rejects a colon with `400001 invalid image name or version`, which names neither field nor fix; `save` fails locally and shows the split, `register`/`create`/`push` forward it and take the platform's word. Name charset is lowercase letters, digits, `.`/`_`/`-`. Reusing a `name:version` that is already READY/SUCCESS fails with `ImageExists` — bump the version rather than deleting, since the old record may be in someone's config.

8. **`sync` re-triggers the platform's own detection, and only for images you own.** Registration is not complete when the record is created — the platform has to observe the push, and a stalled observation looks exactly like a bad push. `sync` reads the record's name+version and re-posts `/image/sync`; `push` already does this itself, so `sync` is for the register-then-push-yourself path. On an **official** image it is refused platform-side (`API error 100004: 暂无权限`, live probe 2026-06-22).

9. **`preheat` is per logic compute group and invisible from the job side.** The call names one `logic_compute_group_id` and returns `preheat_requested` — an acknowledgement, with no progress feedback and no completion signal, and jobs carry no pull/container-start stamps or preheat state (issue #42). So preheating the group a job does *not* land on buys nothing and nothing will tell you that is what happened; it is a latency optimisation, never a scheduling guarantee. Get the id from `inspire resources list` or `model offers` (shape `lcg-<uuid>`), not from the group's display name.

10. **Image ids are `image-<uuid>` and a fragment will not resolve.** `detail`/`sync`/`preheat`/`delete` pass a full id straight through, and their partial-id path only prefix-matches a bare hex fragment against whole ids — a fragment including the `image-` prefix is not recognised. A **name is not resolved** either. Take the id from `image list`/`search` output. `image save`'s notebook argument is the reverse case: `/mirror/save` demands a notebook UUID and answers the generic `100003 记录不存在` to a name, so the CLI resolves notebook names for you.

11. **Deleting is irreversible on a shared account and the CLI does not check what you pointed at.** `delete` sends `DELETE /image/<id>` for whatever the id resolved to, including an official or team-owned record; confirm the row's `source`, `status` and `name` from a `--source all` listing first, and remember an image referenced by `[job].image`/`[notebook].image` or a colleague's config becomes a submit-time failure, not a warning.

12. **The registry you push to is not the registry nodes pull from, and architecture is never validated.** Custom pushes go to `docker-qb.sii.edu.cn/inspire-studio/…`, while nodes pull official images from `docker.sii.shaipower.online`, which is unreachable from a dev machine — so a local build cannot `FROM` a platform image and must start from a public base. Nothing checks architecture: the docker path checks nothing and the skopeo fallback pins `--override-arch amd64`, so building for the wrong arch fails at run time on the node, not at submit time. Build single-arch amd64; this is earlier live evidence, not a platform validation guarantee.

## Applying the rules

```bash
# Rule 1+2: exposure by comparing views, and never trust the default view for your own images.
inspire --json image list --source public         | jq '.data.total'   # institution-readable set
inspire --json image list --source personal-visible \
  | jq -r '.data.items[] | "\(.name):\(.version) \(.status)"'          # where saved images actually live

# Rule 3+4+5: what a create form could pick here now, versus what one record says.
inspire image search pytorch
inspire image detail <image-id>                    # ids only; READY or SUCCESS both mean usable

# Rule 6+7: snapshot a notebook, with the tag in the right field, after checking what is on disk.
inspire image save <nb-id> -n my-env -v v2 --dry-run
inspire image save <nb-id> -n my-env -v v2 --wait   # --accessible personal is the default and the narrow one

# Rule 8+12: local build -> record, push, sync; or a slot you push into yourself.
inspire image push myimg:v3 -n myimg -v v3          # needs local docker, else skopeo (amd64-pinned)
inspire image register -n myimg -v v4 --wait        # then docker tag/push the printed target; sync if it stalls
inspire image sync <image-id>                       # your own image only; official -> 100004

# Rule 9: pre-cache on the group the run will actually land on.
inspire resources list
inspire image preheat <image-id> --lcg-id lcg-<uuid>

# Rule 11: keep the next create from inheriting a deleted or foreign-account default.
inspire image set-default --job myimg:v3 --notebook my-env:v2
inspire image delete <image-id> --yes
```

## Boundaries

- **NOT for launching anything** — submit/monitor/debug a run: job skill; interactive box: notebook skill. The image side ends at a usable `name:version`.
- **NOT for inference servings**: `model deploy` resolves images by name against the official and institution-public views only, so a personal-visible image must be made public before a serving can use it — see the model skill.
- **NOT for moving data** — files in and out of shared storage: storage skill; a snapshot is not a backup of `/inspire/hdd/**` (rule 6).
- **NOT a cloud build service**: there is no Dockerfile-build endpoint on the platform at all; every route is commit-a-notebook, register-a-slot, or push-from-local (rule 12).
- Envelope, exit codes, `--yes`, `--json` placement, ID/`--dry-run` conventions: shared skill, not repeated here.
