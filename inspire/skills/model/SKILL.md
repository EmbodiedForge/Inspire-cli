---
name: model
version: 1.0.0
description: "Inspire platform rules for model inference servings: what deploy --wait actually waits for, why RUNNING is not the same as serving, the triage order for a serving that never becomes ready, how the GPU shape is really chosen, and how the endpoint and token are exposed. Use when serving a model, debugging a serving that will not come up, resizing one, or wiring up its endpoint. NOT for training jobs (see job skill), interactive notebooks (see notebook skill), or publishing the image a serving boots (see image skill)."
metadata:
  requires:
    bins: ["inspire"]
  cliHelp: "inspire model --help"
---

# model

Assumes the shared skill (`inspire skills read shared`) for the envelope, exit codes, the `--yes` gate and `--dry-run`.

## Platform Rules (what --help cannot tell you)

1. **`deploy --wait` waits for a pod, not for a model.** The waiter polls the serving detail and returns the first time the raw status is exactly `RUNNING`; it never opens the endpoint, and weight loading happens after that. Prove the endpoint with `model status --test`, which sends a real `POST /v1/chat/completions`. The waiter matches literal `RUNNING`, while `model wait` normalises aliases such as `SERVING_RUNNING`.

2. **The name the endpoint answers to comes from your launch command, not from the platform.** `--test` derives the model name out of `--command` (`--served-model-name` wins, else the basename of `--model-path`/`--model`, else the serving name) and authenticates with your own `auth.api_key`/`INSPIRE_API_KEY`; the platform `model_id` is metadata only. A model-not-found body from `--test` is therefore a naming bug in your command, not a dead serving. When the result says `attempted: false`, nothing was sent.

3. **The name a serving is deployed with is not the name it serves.** Two separate platform ids ride on the create call (`model_source` + `model_id`), and neither is what clients ask for. `--test`, and any client you write from the endpoint, must use the command-derived name from rule 2.

4. **A serving that never becomes ready usually has no logs.** Admission failures (no node fits, image pull) produce no container output at all, so the cause exists only in events. Triage in this order — **events → instances → logs**: `model events`, then `model instances`, then `model logs --instance <pod>`. Uninformative logs are not proof the serving is healthy.

5. **`logs` and `exec` silently pick one replica.** Without `--instance`, logs come from the alphabetically-first instance and exec picks the first *RUNNING* one (and fails by naming the rest). Multi-replica debugging means looping over the names from `model instances`. An exec with `exit_code: null` lost its channel; re-read state before re-issuing anything that writes.

6. **`--gpu-type` is a substring and `--gpu-count` is a floor.** The requested type is substring-matched in *both* directions against the platform's own shape strings, so `NVIDIA_H200` matches `NVIDIA_H200_SXM_141G`. The instance spec has exactly the requested GPU count, else the first with more. Billing uses the nested `resource_spec_price`; read it back from `--dry-run`'s `platform_request` or `model status`.

7. **Too little capacity is a warning, not a refusal.** When fewer GPUs are free than requested, deploy says so and creates the serving anyway. `--dry-run` surfaces the same shortfall while it still costs nothing. Re-measure capacity rather than relying on an earlier count.

8. **Two non-GPU preconditions gate a deploy.** (a) `--image` resolves by exact, case-insensitive name against the **official** and **institution-public** image views only, so an image under `--source personal-visible` is invisible here — publish it or use an official image (see image skill). (b) Model metadata source must match the id shape (`mp-…` → `MODEL_PLAZA`, anything else → `MODEL_HUB`). With no `--model-id` the CLI picks a model owned by the chosen project; an empty result means pass `--model-id` or change project.

9. **The workspace is chosen before you are asked.** A real web-session workspace beats config; explicit workspace selection or `INSPIRE_WORKSPACE_ID` pins the target when it matters. Workspace aliases are user naming only and do not imply GPU class or internet access. `model list --all` is still scoped to one workspace, not a cross-workspace inventory.

10. **stop / scale / delete act on different things and the status field will not tell you which.** `stop` interrupts replicas and keeps the record; `scale` changes only replica count; `delete` removes the record and is irreversible. A detail endpoint can still answer `STOPPED` after deletion — `model list` proves removal, and `model instances`/`available_replicas` reports compute.

11. **Serving ids are `sv-<uuid>`, and `model` resolves nothing locally.** The id string goes into the API path verbatim — no prefix matching or name lookup, unlike `job`/`image`. Copy the id out of `deploy`/`list` output instead of constructing one.

12. **Endpoint and token are display-filtered, not one-time.** Without `--reveal-token` both the token *and the endpoint URL* come back redacted, because the endpoint embeds the token path. Read them with `model status --reveal-token` when needed. Some custom servings carry no token; the credential that authenticates an inference request is your platform API key (`model api-keys list`). Treat revealed output as secret material.

## Applying the rules

```bash
# Rule 6+7+8: spend nothing before the shape, capacity and metadata are right.
inspire model deploy --name glm5-serve -i sglang:v0.5.10-cu130 \
  -c "python -m sglang.launch_server --model-path /inspire/hdd/…/glm-5.1-fp8 --served-model-name glm-5.1-fp8 --tp 8 --port 8000" \
  --gpu-type NVIDIA_H200 --gpu-count 8 --dry-run     # read .resolved + .warnings, incl. gpu shortfall

# Rule 1+2: create, then prove it answers — status alone only says a pod exists.
inspire model deploy --name glm5-serve -i sglang:v0.5.10-cu130 -c "…" --gpu-type NVIDIA_H200 --gpu-count 8 --timeout 1800
inspire --json model status <sv> | jq -r '.data | "\(.status) \(.available_replicas)/\(.gpu_count)"'
inspire model status <sv> --test                     # real chat request; model name derived from -c

# Rule 4: not ready — events, then instances, then that one replica's logs.
inspire --json model events <sv> | jq '.data.events // .data.items'
inspire model instances <sv>                         # name, status, node per replica
inspire model logs <sv> --instance <pod> -n 200

# Rule 5: every replica, not the one the CLI happened to pick. Text output has NO header row — `NR>1` would skip the first replica.
for pod in $(inspire model instances <sv> | awk 'NF{print $1}'); do
  inspire model exec <sv> --instance "$pod" -- /bin/sh -lc 'nvidia-smi -L'
done

# Rule 10: shrink, release, and prove it with list rather than with status.
inspire model scale <sv> -r 2                        # never the current count (150015)
inspire model stop <sv> --yes && inspire model wait <sv> --until stopped
inspire model delete <sv> --yes && inspire model list

# Rule 6: what you are allowed to ask for right now, per group and quota.
inspire model offers
inspire model availability
```

## Boundaries

- **NOT for training runs** — batch jobs, queuing reasons, per-attempt logs: job skill. **NOT for interactive boxes**: notebook skill.
- **NOT for producing the image** a serving boots (save/push/register, READY-vs-SUCCESS, the two visibility axes): image skill. Rule 8a is where image identity bites servings.
- **NOT the Model Hub catalog**: `inspire model-hub` is a different surface; `model models` only lists models the serving surface can bind metadata to.
- **NOT a general request client**: `model update --payload-json` posts a raw full-body `/inference_servings/update` payload and omitted fields are not preserved by the platform — for replicas use `scale`, and build any body from a detail read rather than from scratch.
- Envelope, exit codes, `--yes`, `--json` placement, ID/`--dry-run` conventions: shared skill, not repeated here.
