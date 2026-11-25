---
name: storage
version: 1.2.0
description: "Inspire shared storage over WebDAV: pool selection, mount isolation and why another project's path does not exist, who gets billed for which tree, and the collision/deletion protections on a filesystem others can see. Use for reading or writing cluster files you own (checkpoints, your own corpora, shared code) without SSH. NOT for a pod's private disk (job/notebook upload), local-to-Bridge sync (bridge skill), or the published data-square catalogue and its mounts (dataset skill)."
metadata:
  requires:
    bins: ["inspire"]
  cliHelp: "inspire storage --help"
---

# storage

**Read the shared skill first (`inspire skills read shared`)**: envelope, exit codes, the `--yes`
gate. The pool semantics below are stated once in the **platform** skill and applied here.

## Platform Rules (what --help cannot tell you)

1. **Pick the pool before the path.** **(platform docs)** `/inspire/ssd/…` is the small fast
   training hot path; `/inspire/hdd/…` is general-purpose (check free space first);
   `/inspire/qb-ilm/…` and `/inspire/qb-ilm2/…` are one auto-tiering family (cache hit ≈ SSD,
   miss ≈ HDD — "qb-ilm2 is newest and roomiest" is our observation, not a doc promise). Two path
   layouts coexist: `/inspire/<pool>/project/<project>/<user>` and
   `/inspire/hdd/ws-<uuid>/public-project/<user>/`.
   `inspire storage roots` shows which (workspace, storage, category) roots actually exist for
   your account — pools you have no mount for do not appear, and the listing can also carry
   `share-hdd`/`share-qb-ilm2` roots (the `…/inspire_shared/{mount,copy}` trees) plus
   `global_user`/`global_public` categories that so far only appear under `hdd` (measured
   2026-10-01).

2. **Mount isolation means "not there", not "not allowed".** **(measured)** An instance mounts
   only its own project's fileset, so a path under another project returns `No such file` inside
   the pod. This is the observed default, not a documented guarantee — the console offers
   configurable read-only mounts of other users' directories. Over WebDAV the *listing* may still
   show more than a pod would see — an empty-looking `ls` on a project root can be a permission
   view, not emptiness; confirm with `stat`.

3. **Who pays depends on the category — and the docs do not settle it.** Treat
   `global_user/<account>` growth as billed to that account's personal storage quota; which account
   pays for `project/*` and `global_public` growth is an unanswered platform question
   — so treat shared trees as collectively owned: write under your own
   subtree, and never `rm` outside it. This surface shows other people's work by default.

4. **The path is the contract.** The absolute path you write via `storage put` is the same string a
   job/notebook/serving sees in its container. Write once here, reference the absolute path in the
   training command — no upload step in the workload at all.

5. **There is no trash and no undo.** `storage rm` on a directory is gone permanently; that is why
   it is the group's only high-risk-gated verb. `mv` refuses an existing destination without
   `--overwrite` — collision protection on shared paths, deliberate.

6. **WebDAV is the medium; size decides the route.** Configs, logs and medium artifacts are fine
   here. Large checkpoints are faster over the Bridge SSH path (`bridge scp`, `sync`) or written
   in-cluster by the workload itself. `put`/`get` are single-file verbs — for trees, loop or switch
   transport.

7. **Prove the channel before blaming the path.** `storage probe` (classified *write*: it creates
   and deletes a throwaway object) separates auth/network failure from a bad path in one step;
   `storage connection-info` prints the SFTPGo/WebDAV endpoint, secrets redacted, always fetched
   live (`--refresh` is a no-op compatibility flag).

8. **A published dataset is not a directory you own.** Two portals, one cluster: WebDAV is your
   view of the storage *pools* (`/inspire/<pool>/…`), which you read and write; the **data square**
   (`aip.sii.edu.cn`) is a published catalogue whose versions a workload mounts at
   `/inspire/dataset/<code>/<version>` — a path family that belongs to neither pool and is not
   yours to edit. Browse and check those with `inspire dataset list|versions|check` and attach them
   with `--dataset` on `job create`/`notebook create` (**dataset** skill); do not go looking for
   them under a pool path with `storage ls`. A workload may of course copy from a mount into a
   pool — from then on it is an ordinary directory with no version, no provenance and no updates
   from the catalogue.

## Applying the rules

**Find where things live (1, 2):**

```bash
inspire --json storage roots | jq -r '.data.roots[] | "\(.storage)  \(.category)  \(.path)"'
inspire storage ls /inspire/hdd/project/<p>/public --long
inspire --json storage stat /inspire/hdd/project/<p>/public/data.tar
```

**Write without clobbering (3, 5):**

```bash
inspire storage mkdir -p --exist-ok /inspire/hdd/project/<p>/public/exp1
inspire storage put ./config.yaml /inspire/hdd/project/<p>/public/exp1/config.yaml
inspire storage mv <src> <dest>                 # refuses an existing dest without --overwrite
inspire storage rm /inspire/hdd/.../exp1/old.yaml --yes
```

**Channel vs. path (7):**

```bash
inspire storage probe && inspire storage connection-info
```

## Boundaries

- NOT for a pod's container-local disk — `notebook upload/download` or `job upload` (exec channel,
  small files only).
- NOT for local↔runner trees or git-based code sync — **bridge** skill (`sync`, `bridge scp`).
- NOT for the data-square catalogue or a mounted dataset version (`/inspire/dataset/…`) — **dataset**
  skill; those are platform-managed, not WebDAV paths (rule 8).
- Pool choice, mount isolation and billing background — **platform** skill.
