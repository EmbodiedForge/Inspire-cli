---
name: workspace
version: 1.0.0
description: "Inspire workspace identity, configured aliases, logical compute groups, physical compute pools, and explicit internet declarations. Use for workspace list, workspace pool set, resolving workspace names or aliases, and inspecting the cluster evidence behind an egress decision."
metadata:
  requires:
    bins: ["inspire"]
  cliHelp: "inspire workspace --help"
---

# workspace

Read the shared skill first (`inspire skills read shared`). This skill owns workspace
identity and compute-pool declaration rules; other skills link here instead of defining
their own egress identity rules.

## Identity and aliases

- `inspire workspace list` shows exact platform workspace IDs, display names, and the
  user-configured aliases that select each name. JSON carries these as `id`, `name`, and
  `aliases` on each item.
- Aliases come from `[accounts."<alias>".workspaces]`, where each entry contains only
  `name = "<exact platform display name>"`. An alias is a user-written selector, not
  evidence of GPU class, capacity, project, or internet reachability.
- Live inventory, quotas, budgets, and capacity are point-in-time observations. Re-measure
  with the relevant command; do not remember them as durable account facts.

## Logical groups and compute pools are different objects

- A logical compute group has an `lcg-` ID. Its parent physical compute pool has a `cg-`
  ID. Several logical groups can share one pool.
- Internet reachability belongs to the compute pool/cluster, not to the workspace or the
  logical group. One workspace can contain pools with different reachability.
- `inspire workspace list --pools` prints one row per logical group and includes
  `logic_compute_group_id`, parent `compute_group_id` (the `cg-` pool ID), `pool_name`,
  `cluster_id`, raw `cluster_name`, and that parent pool's `internet` declaration. Human
  output labels the same fields explicitly.
- `cluster_name` is data, not a verdict. The naming convention containing `nat` for an
  internet-capable cluster versus `int` for an internal one is only a heuristic, never a
  platform guarantee. Do not infer or store a declaration automatically. `compute_net` is
  a network ID and is not reachability evidence.

## Declaring reachability

```bash
inspire workspace pool set cg-<pool-id> --internet on --dry-run
inspire workspace pool set lcg-<logical-group-id> --internet off --dry-run
inspire workspace pool set '<exact pool or logical-group display name>' --internet on --dry-run
inspire workspace list --pools
```

All three reference forms resolve to the visible parent `cg-` ID. The stored declaration is
always keyed by that `cg-` ID and applies to every logical group sharing the pool. An
ambiguous name is an error; use an ID. An undeclared pool means unknown, not offline.

Use `--dry-run` to preview without writing. A real `workspace pool set` writes only the
explicit declaration under the selected account in the global config; it does not change
platform state. Re-read `workspace list --pools` after declaration changes and re-check
live evidence instead of treating old declarations as platform guarantees.
