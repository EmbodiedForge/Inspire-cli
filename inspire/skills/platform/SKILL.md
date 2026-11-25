---
name: platform
version: 1.3.2
description: "Inspire platform semantics that no command surface shows: workload-type selection, egress zones and internal mirrors, the storage pools and what a mount does not show, voucher billing, priority vs preemption, GPU-based idle reclamation, and the observability layering shared by every workload. Read after the shared skill, before any create/choose/where-do-I-put-it decision. Group skills (job, notebook, model, image, storage, bridge) apply these rules to their own surface."
metadata:
  requires:
    bins: ["inspire"]
  cliHelp: "inspire --help"
---

# platform

Assumes the shared skill (`inspire skills read shared`). Rules here are marked
**(platform docs)** — from the platform's own user documentation — or **(measured)** — from this
CLI's live probes. Platform behaviour drifts; a dry-run or a small probe beats a remembered rule.
When the installed CLI has not caught up with the console, `inspire api eval <JS>` supplies an
authenticated temporary Playwright page and otherwise stays out of the way: use async JavaScript,
fetch, custom headers/bodies, DOM reads, and multiple API calls directly. Some training Actions are
only mounted on the training UI; pass
`--url https://qz.sii.edu.cn/jobs/spacesOverview?spaceId=...`, not the chat root. Read the shared
skill for `@file`, stdin, timeout and redaction contracts. This is the general escape hatch, not a
new planner.
If the request contract is unknown, inspect DOM, resource URLs and frontend JavaScript with that
same eval command; there is no separate discovery command. Never guess an unobserved write schema.
Raw requests and eval have no read-only route guard or automatic `--yes` gate; neither is a dry run.
After a timeout or disconnect, read platform state before repeating a request.

## Workload types: choosing wrong fails quietly, not loudly

| Type | CLI group | It is | Use for |
|-|-|-|-|
| Notebook | `notebook` | a connectable, resident container | debugging, installing deps, short probes |
| Job | `job` (`run` is its deprecated shorthand) | fixed-scale GPU batch | training, batch inference, worker pools |
| HPC | `hpc` | CPU Slurm batch | preprocessing, evaluation, data pipelines |
| Ray | `ray` | head + scalable worker groups | streaming, daemons, elastic workers |
| Serving | `model` | a registered model behind HTTP | online inference (官方名「在线推理(自定义)」) |
| Sandbox | — | platform type added 2026-04; seen as `sandbox测试` groups in CPU spaces | not wrapped by this CLI yet — semantics unverified, probe before relying |

(platform docs) Picking the wrong type does not error — the task fails later in a way that is hard
to trace. Fixed-scale CPU batch belongs to HPC, not a 1-CPU job; fixed-scale training belongs to a
Job, not Ray. HPC specifics that have bitten every first-time user: the `-c` body needs an explicit
`srun` (platform docs) — without it Slurm reports SUCCEEDED with `Steps 0/0` and nothing ran
(measured) — and Slurm bills the submission pod (0.1 CPU / 128 MB) plus the slurm management pod
(0.2 CPU / 256 MB) on top of your workers (platform docs). cgroup memory is the `--quota` row's
memory regardless of `--memory-per-cpu`, and `nproc`/`free` inside the container report the *host*,
so concurrency must come from `SLURM_CPUS_PER_TASK`.

## Egress belongs to compute pools, and mirrors are the bridge

- **(platform docs)** Spaces offer internet-capable and internal compute pools, and the HPC space
  has no public egress. The difference is why "download it in the start command" works in one pool
  and hangs silently in another.
  Workspace and group names alone do not establish reachability. The **workspace** skill owns pool identity, evidence, and declarations.
- **(platform docs)** An internal pool still reaches SII internal mirrors
  (`nexus.sii.shaipower.online`: PyPI, conda, npm, apt, PyTorch wheels, maven, CUDA…), so "needs
  the public internet" and "needs an internal mirror" are different problems with different fixes.
  PyPI recipe: set both `global.index-url http://nexus.sii.shaipower.online/repository/pypi/simple/`
  and `global.trusted-host nexus.sii.shaipower.online`.
- **(platform docs)** No internal mirror fronts GitHub, raw.githubusercontent.com, or public object
  storage; the internal git service is `gitee.sii.edu.cn`. Assets from GitHub must be uploaded
  (storage/bridge) or baked into an image. This is platform design, not a gap this CLI can close.
- Standard flow: download in an internet-capable notebook → shared disk or image → consume from an
  internal GPU pool.
- **(measured)** The platform's auto-injected pip index varies by image and is broken as injected
  on some no-egress images; overriding both the index and trusted host is the fix.

## Storage: pools, tiers, and what a mount does not show

- **(platform docs)** Pools under `/inspire/<pool>/…`: `ssd` (small, fast; training hot path,
  checkpoints), `hdd` (general; check free space before writing), and the `qb-ilm`/`qb-ilm2`
  auto-tiering family (cache hit ≈ SSD speed, miss ≈ HDD — "qb-ilm2 is newest and roomiest" is our
  observation, not a doc promise). Docs also mention `sj-ssd`/`sj-ssd2` (Songjiang) and private OSS
  buckets, which this account does not see.
- **(measured 2026-10-01)** `inspire storage roots` returns six `storage` keys here: the four pools
  plus `share-hdd`/`share-qb-ilm2` (the `…/inspire_shared/{mount,copy}` trees inside ordinary pool
  paths), and `global_user`/`global_public` roots only under `hdd`. Two path layouts coexist
  (platform docs): `/inspire/<pool>/project/<project>/<user>` and
  `/inspire/hdd/ws-<uuid>/public-project/<user>/`. Roots are per-workspace — most workspaces return
  the same set, but a workspace without a mount simply omits those roots.
- **(measured)** Mount isolation: an instance mounts only its own project's fileset, so another
  project's path does not exist in the container — `ls: No such file` there means *not mounted*,
  not *permission denied*. This is the observed default, not a documented guarantee: the console
  offers configurable read-only mounts of other users' directories.
- Writes under `global_user/<account>` are billed to that account's storage quota. Which account
  pays for growth under `project/*` and `global_public` is an open platform question — treat shared
  trees as collectively owned and never delete outside your own subtree.
- Paths are identical from every pod and from `inspire storage` — write once, reference by absolute
  path in the training command.

## Billing (点券)

**(platform docs)** Per-card hourly rates: H100/H200 = 1 点券, RTX 4090 = 0.33, Ascend 910B = 0.35
— and **low-priority (preemptible) tasks do not consume vouchers at all**. 独立空间 tasks settle
daily at 23:59; 公共空间 tasks are charged by usage. **(measured 2026-10-01)** Live offer payloads
price only the GPU term (`cpu_price`/`memory_price` = 0, CPU-only rows cost 0) and match those
rates exactly. Billing scales per *instance* (a 2-node × 8-卡 job bills 16/hour) — the offer price
is per row, so the instance multiplier is inferred, not directly probed. The project page's
`My Budget` (what *you* can still spend — decides whether your next task starts) and
`Project Budget` (the whole project's remainder) can differ by orders of magnitude. A project at zero/negative budget is a live, reachable
state and a plausible cause of long `Unschedulable` stretches (measured).

## Priority and preemption

- **(platform docs)** A task's priority may not exceed its project's priority, and priorities 1–3
  run preemptibly — higher-priority work can take their resources. Quota specs can be bound to
  allowed priorities. That is all the docs promise.
- **(measured 2026-10-01)** The platform's own labels are `4 → NORMAL` and `10 → HIGH`; no record
  labelled LOW has been observed, and the per-row `allowed_priority_levels` field exists but came
  back empty on every quota row probed (H-card, 4090, CPU, 910B). An empty declaration is **not** an
  open range: the create endpoint restricts priorities by its own rule and says so only in the
  rejection.
- **(measured 2026-11-08)** Fair-scheduling spaces do restrict, and the range is theirs to set. In
  a fair-scheduling workspace at `1xH100`, priorities **1, 2 and 3 were
  accepted and 4, 5, 6, 7 and 10 were refused** with `InvalidParameter:
  任务优先级不在该资源规格允许的范围内` — identically under a NORMAL project and a HIGH one, so this
  is the resource spec speaking, not the project. Its scheduling rows declare
  `allowed_priority_levels: ["low"]`, and `low` is the band 1–3: **the declaration states the range the
  create endpoint enforces**, so the highest priority the offer you pick takes is readable before submitting.
  Where that number comes from, and where it does not: it comes from a **train schedule `quota` row**
  (`get_notebook_schedule`) that covers the offer you picked, in this order — the exact group+quota row; a
  generic quota row naming no group; a row in the same group with the identical `gpu,cpu,mem` shape; the
  group's rows, and only when they all declare the same levels. The declaring rows carry quota ids of their
  own while the offers for the same space carry different ones, so an offer can resolve no level of its own
  while the space still refuses 4+ — and that reading is `unknown`, which means *no ceiling is known*, never
  *unrestricted*. Declarations that disagree come back as `conflict` and nothing is capped on their account.
  `inspire job availability` reports the resolution per offer in `priority_evidence` (`status`, `scope`,
  `levels`, `declaration_ids`) beside `priority_effective` and `priority_ceiling`, and `inspire job offers`
  prints the `--group`/`--quota` selector each row belongs to. A space-wide union of every declared level is
  a diagnostic at best: never widen a group-specific declaration across the space, and never let another
  group's `low` cap the H200 offer you selected. `inspire job quota` is a deprecated compatibility view of
  the same rows and is no longer where a ceiling is read.
  An older note claiming fair spaces take *only* 1 or 4 was wrong in detail (2 and 3 are taken, 4 is
  not) and in kind: read the range off the offer you are about to select, never off memory. Where the
  selected offer resolves no declaration at all, the range is genuinely unknown in advance and an empty
  declaration is **not** an open range.
- **(measured)** The CLI caps `--priority` (1–10, default from config or 6) at the project's own
  priority and reports both numbers — trust the reported value (job skill, rule 10). For a fair space the
  ceiling is the top of the highest priority level declared **for the selected offer** (`low` → 3,
  `normal` or `medium` → 6, `high` → 10; several declared → the highest of them), and a request above it
  is **capped to it in one submit** — a `--priority` you typed included, which is why `--priority 7` on
  an offer declaring `low` creates the job at 3 instead of failing. Every cap names the value asked
  for, the ceiling, the scope and the declaration it came from; nothing is capped silently. Where the
  offer declares nothing, the ceiling is the highest priority this CLI has watched that exact group+quota
  accept; where neither exists, the CLI submits the number asked for and reports the platform's refusal
  (`PriorityRefused`)
  rather than inventing one — after a refusal it steps down at most once, to a value the offer has
  evidence for, and a refusal about anything other than the priority never earns another create.
  Fault tolerance auto-enables for a LOW-labelled project or priority < 4 (job skill, rule 9) and is
  judged on the priority that actually goes out, so a request capped into 1–3 gets the restart loop.
  Read the reported priority.
- **(measured)** Capacity planning follows the *effective* priority after that cap: 1–3 can use only idle
  capacity; 4+ can also use capacity held by preemptible priority 1–3 work. The latter is eligibility to
  preempt, not a scheduling guarantee, so the new workload may still queue. For multi-instance requests,
  compare `per-instance GPUs × nodes` against capacity in each individual compute group, never against a sum
  across groups. `preemptible_usable` says whether the current request may include the low-priority pool in
  this calculation; it does **not** say whether the current workload itself can be preempted. With no
  priority requested it is `null` — the plan does not pick a priority to answer with.
- **(platform docs, 2026-07 release note)** Task-count quotas (任务配额) exist on top of GPU
  quotas: per workspace, per-user default, and per-user override, each split by task type and
  priority class, with two numbers — 运行数量 (max running at once) and 总提交数量 (max in-flight).
  **(measured 2026-10-01)** The task-count numbers are admin-side only: no user-visible API field
  exposes them, and no error code distinguishes "task-count cap hit" from "no GPUs free" — both
  present as plain queueing. **(measured 2026-11-18)** *project resource quota* is visible:
  `GetProjectQuotaJobs` / `inspire job quota-usage --project ...` returns used and limit for GPU,
  CPU and memory; `-1` means unlimited, while `0` or `used >= limit` means no room. A successful comparison holds the workspace, compute group and workload shape constant. The console maps create codes 301029/301030 to 「配额不足提醒，任务无法提交」
  and its queue tooltip names space resources or project/personal quota as the alternatives.
  `job status` / `notebook status` now consult this block and expose `pending_hint`; compare active
  workloads and try another authorized project before blaming nodes. A 1×H200 notebook is the
  minimum 1 GPU / 20 CPU / 200 GiB shape, not a whole node; a 4×H200 job needs 80 CPU / 900 GiB and
  an 8×H200 gang needs 160 CPU, so free GPU count alone proves neither fit nor admission. Ask the
  space admin only when every authorized project is blocked.

## Idle reclamation is a rule, not a fault

**(platform docs)** Reclamation is judged by *GPU utilization*, not by whether anyone is connected,
and spaces/projects can be configured exempt. Query the current workspace with
`inspire resources policy --workspace <name>` (or `--workspace-id <id>`); by default it covers
notebook, job, HPC, Ray, and serving, and `--workload` selects one. **(verified live 2026-10-29)**
The current policy for 分布式训练空间: notebooks are reclaimed below 15% GPU for 3 h or after 18 h
up; training jobs below 40% GPU for 3 h. The policy is per-workspace — re-read it for the
workspace you are in instead of trusting these numbers elsewhere. The consequence stands regardless:
a long CPU-side phase (big preprocessing, evaluation, waiting on an external service) inside a GPU
workload can be silently collected — move it to a CPU space. Where a workspace's policy row shows
`-` it means *no policy declared*, which is **not** the same as *no limit*; the two readings are
opposite.

**(measured 2026-09, three long-up low-GPU jobs)** All three were policy-stopped at ~3 h up, which
matches the 3 h rule above. What the three cases could *not* settle is which clock the 3 h runs
on — creation time or container start (one lived 3.33 h with ~3.1 h of image-pull dwell, so under a
start-based clock its true runtime was ~0.2 h). Pinning that down needs a dedicated ~3.5 h probe
job; **decided 2026-10-08 not to spend GPU quota on it** — the academy policy text is treated as
authoritative, and the safe posture under either clock is the same: keep pre-run dwell short.

## Quota rows and shared memory

**(platform docs)** Memory is quoted in GB (the CLI renders GiB), rows with 0 GB memory exist, and
multi-node specs must be 8 cards per node (single-node specs may be 1/2/4/8). **(measured)** The
`gpu,cpu,mem` triple must be copied as a whole row together with the compute group from that same
row — the same triple legitimately appears under several groups, and mixing rows fails resolution.
`resources specs` is a cache, not a live offer catalogue: it can be stale or omit an entire GPU-shape
category, so use the workload's live `offers` command when choosing a group and quota. `--refresh`
updates the cache but still says nothing about current capacity. Pure-CPU rows exist (`0,4,16` and
larger) but `inspire resources specs` shows **no rows at all for CPU workspaces** — read them from
`notebook offers` / `hpc offers` instead (measured 2026-10-01; earlier notes citing a `0,4,32` row are
stale). Per-instance `/dev/shm` is a creation-time parameter (`shm_gi` for jobs, `shm_size` →
`shared_memory_size` for notebooks), separately capped by the row's memory — PyTorch DataLoader with
many workers needs it raised explicitly. `job create` always sends `shm_gi` (default 200 GiB capped to
the row memory; override with `--shm`), while the platform's own default varies wildly (64 MB on a
job pod 2026-09-29; 800 GB 2026-10-08 on H100-cuda13.2), so check `df -h /dev/shm` at exec time instead
of trusting a remembered number; remount from inside is refused (job skill, rule 20).

## Capacity evidence and degraded visibility

Every live count, budget, timestamp, inventory, and policy value in these skills is a dated sample,
not durable state. **Re-measure with the named command; do not remember the number.** Availability in
`job availability` and create dry-run is an **advisory**, point-in-time signal, not a reservation.
`placement_basis` says what was actually checked: `node-gpu-count` is a node-derived group total;
`full-free-nodes` counts whole free nodes; `request-fit-nodes` also checks the request's GPU, CPU
and memory per node. `aggregate-gpu-count` is retired from the CLI capacity path: aggregate data
cannot prove node fit or schedulability, even when its count covers the request.

- Workload `offers` and `availability` require an explicit `--workspace` or `--workspace-id`. Their
  first line names the workspace and the JSON envelope carries `workspace_id`, so an empty table is
  evidence about that workspace only. In contrast, `resources list` and `resources nodes` are
  account-wide; they accept `--group` / `--resource`, not `--workspace-id`. Use
  `resources list --resource 4090` to find a resource class account-wide.
- `job availability` is the Job planning view: one workspace-scoped plan pairing those capacity signals
  with each offer's priority evidence. `job offers` is the same rows without capacity, and prints a
  copyable `--group`/`--quota` selector per row. Both name what they could not read — `coverage` counts
  the groups that answered against the ones that failed, `failures` names each gap, and a group that
  failed to answer is never presented as empty capacity, while a group you named by hand that cannot be
  priced is an error rather than an empty table. `job quota` remains as a deprecated compatibility entry
  on that same planner.
- Workspace aliases are user-written names for exact platform display names; inspect them with
  `workspace list`. GPU class comes from each logical group's live resource detail, never its name.
- Pool identity and reachability declarations belong to the **workspace** skill. `workspace list
  --pools` exposes each logical group's parent `cg-` pool and raw `cluster_name`. The `nat`/`int`
  naming convention is a heuristic only; the CLI never derives or stores a declaration from it.
  `compute_net` is only a network ID, not reachability evidence.
- Idle capacity and low-priority usage are separate observations. Free GPUs come from the per-node
  view; `Low Pri` / `gpu_low_priority_used` is the console's advisory current field
  (`low_priority_source: aggregate_advisory`), not capacity evidence. It is `n/a`/`null` when the
  advisory lookup fails, never invented as 0. Low-priority usage is not idle capacity and neither
  figure guarantees placement or reclaimability.
- `resources nodes` reads `workspace?Action=ListNodeDimension`, the same per-node view the console
  uses. There is no aggregate fallback: a denial or transient failure sets `availability_source`
  to `unavailable` and emits JSON `null` / human `n/a` for node counts and capacity, rather than
  presenting an aggregate-derived number as free capacity.
- **(measured 2026-11-18)** The aggregate `GetLogicComputeGroupResource` can be stale: it reported
  `gpu_available: 8` for 开发区-H200-3号机房 while the console node view showed `GPU 160/160` and
  every node at 0 free. Aggregate capacity is retired from the CLI capacity path; node provenance
  and `unavailable` are the only outcomes. An aggregate difference is not evidence of a free card.
- `/api/v2/job?Action=ListNodeJobs` is the **job service only**. A node with 0 training jobs can
  still hold a notebook, serving, or other task; the console node view / `ListNodeDimension` is the
  workload-inclusive source. Do not infer "node free" from `ListNodeJobs` alone.
- The source field names differ by surface. Job create dry-run uses `selection_source` and
  `fallback_reason` inside `availability`; `resources nodes` uses `availability_source` and
  `fallback_reason` per row. `job availability` answers the Job resource plan — `scope`, `coverage`,
  `failures`, `offers[]` beside the shared `signals[]`. `source` and `error` occur only on each
  `signals[]` row, on every workload. There is no envelope-level `source` or `error` on
  `job availability`.
- `job list --active` is a client-side filter. Its JSON `total` describes the filtered result, not
  the pre-filter platform count. `job list` lists the training service `ListJobs` history, not
  notebooks, inference servings, or the console live task view. `job list --all-users` remains a
  compatibility no-op. `notebook list --all` is rejected with an `UnsupportedOption` error:
  `ListNotebooks` is user-scoped and cannot add other users' notebooks. The console's all-user
  live view is `workspace?Action=ListTaskDimension`, which the CLI does not wrap yet.
- `used_gpus > total_gpus` can come from stale or oversubscribed platform data. Never interpret the
  negative remainder as hidden idle capacity or use it to make an insufficient request look feasible.

## Names are the addressing scheme — duplicates are your problem

**(platform docs)** The platform does not reject duplicate names, but every CLI here addresses by
name; after a duplicate, every command becomes an ambiguity. Keep names unique per workspace
yourself. Conversely: three decisions an agent must **ask the human, never infer** — which platform
project a repo belongs to, which non-public workspaces it may use, and the storage-pool/path
convention. Guessing from list order or name similarity fails silently and downstream.

## Observability: one question per layer

- `events` — why the scheduler will not place it / why a start failed. Controller and per-pod
  events tell different halves of the story; admission failures produce **no logs at all**, so
  "no logs" means "look at events", not "healthy". **(measured 2026-10-01)** The first event lands
  ~35 s after create — an empty events list in the first minute is a race, not absence.
- `logs` — what the program itself said. **(measured 2026-10-01)** Proven from the console's own
  traffic: its 日志 tab calls the same pod-keyed log endpoint (`GetJobLog`) and its 事件 tab is
  byte-identical to `job events` — there is no separate scheduling-log stream. Logs are keyed by
  pod name, so per-instance collection merged into one timeline is the only shape that exists.
- `metrics` — whether GPU/mem/CPU/IO are actually working; on multi-pod runs, whether the workers
  are in step.
- `instances` — rank ↔ node mapping; the only way to find the straggler in a multi-node run.
- Order for "it never came up": **events → instances → logs**. Inverting it wastes an hour on an
  empty log store.

## Enforced elsewhere

Egress consequences for `exec`/SSH (bridge skill), snapshot/registry/visibility rules (image
skill), priority caps and fault-tolerance interaction at create time (job skill), serving image
visibility and endpoint rules (model skill), pool mechanics over WebDAV (storage skill).
