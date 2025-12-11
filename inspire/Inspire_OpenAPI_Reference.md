---
title: Inspire Platform OpenAPI Reference
language_tabs:
  - shell: Shell
  - http: HTTP
  - javascript: JavaScript
  - ruby: Ruby
  - python: Python
  - php: PHP
  - java: Java
  - go: Go
toc_footers: []
includes: []
search: true
code_clipboard: true
highlight_theme: darkula
headingLevel: 2
generator: "@tarslib/widdershins v4.0.30"
---

# Inspire Platform OpenAPI

Base URL: **https://qz.sii.edu.cn**

This document summarizes the Inspire distributed training OpenAPI in English. The original auto-generated reference (in Chinese) has been rewritten for easier consumption by English-speaking contributors.

## Authentication

### POST `/auth/token`
Obtain an access token that is required for all subsequent requests.

#### Request body
```json
{
  "username": "string",
  "password": "string"
}
```

#### Response body
```json
{
  "access_token": "string",
  "expires_in": 0,
  "token_type": "string"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| username | string | yes | Inspire account username |
| password | string | yes | Inspire account password |

---

## Distributed Training Jobs

### POST `/openapi/v1/train_job/create`
Create a distributed training job. The API accepts either the low-level spec IDs or the simplified natural-language flags exposed by `inspire_api_control.py`.

#### Request body excerpt
```json
{
  "name": "string",
  "command": "python train.py",
  "framework": "pytorch",
  "logic_compute_group_id": "string",
  "task_priority": 8,
  "framework_config": [
    {
      "spec_id": "string",
      "instance_count": 1,
      "shm_gi": 200
    }
  ],
  "envs": [
    {"name": "ENV_KEY", "value": "value"}
  ],
  "auto_fault_tolerance": true,
  "enable_notification": true,
  "enable_troubleshoot": true
}
```

| Field | Location | Type | Required | Description |
|-------|----------|------|----------|-------------|
| name | body | string | yes | Human-readable job name |
| command | body | string | yes | Command executed on Inspire workers |
| framework | body | string | no | Training framework label (`pytorch`, `tensorflow`, …) |
| logic_compute_group_id | body | string | yes | Compute group ID (maps to data hall + GPU type) |
| framework_config | body | array | yes | One or more spec definitions (spec ID, image, shm, etc.) |
| envs | body | array | no | Environment variables injected into the job |
| auto_fault_tolerance | body | boolean | no | Retry failed runs automatically |
| enable_notification | body | boolean | no | Send Inspire console notifications |
| enable_troubleshoot | body | boolean | no | Enable extra diagnostics |

#### Successful response excerpt
```json
{
  "job_id": "string",
  "status": "PENDING",
  "created_at": "string",
  "priority": 8,
  "node_count": 4,
  "command": "python train.py",
  "framework": "pytorch"
}
```

### POST `/openapi/v1/train_job/detail`
Return the latest status snapshot for a given job ID.

#### Request body
```json
{
  "job_id": "string"
}
```

#### Response body excerpt
```json
{
  "job_id": "string",
  "status": "RUNNING",
  "sub_status": 0,
  "sub_msg": "string",
  "timeline": {
    "created": "1732272000000",
    "resource_prepared": "1732275600000",
    "run": "1732275900000",
    "finished": null
  },
  "node_count": 4,
  "priority": 8,
  "running_time_ms": "5400000"
}
```

### POST `/openapi/v1/train_job/stop`
Stop a running distributed training job.

#### Request body
```json
{
  "job_id": "string"
}
```

#### Response
`200 OK` with an empty body indicates the stop request has been accepted.

---

## Data Models

| Model | Description |
|-------|-------------|
| `pkg_openapi_controller_train.Job` | Complete job metadata returned by the detail endpoint. Includes timeline, node info, environment variables, and pricing metadata. |
| `pkg_openapi_controller_train.JobOpenapi` | Simplified request payload accepted by `/train_job/create`. |
| `resource_price.InstanceSpecPriceInfo` | Pricing structure describing CPU/GPU quotas and per-hour rates. |
| `train.FrameworkConfigOpenAPI` | Minimal framework configuration required by the OpenAPI (spec ID, image, instance count, shared memory). |
| `common.MountPath` | Mount definitions for shared storage volumes (mostly unused in current workflows). |

Each of these models matches the canonical JSON schema used by the Inspire backend. When using the higher-level scripts (`inspire_api_control.py`, `train_debug.sh`), these objects are populated automatically—you only need to call these OpenAPI endpoints directly when debugging or building custom tooling.

---

## Usage tips

1. **Authenticate once per session.** Cache `access_token` and reuse it until the server reports `401 Unauthorized`.
2. **Prefer smart helpers.** The repository already exposes wrappers that convert natural-language resource descriptors into `spec_id` + `logic_compute_group_id`. Use them instead of hand-editing IDs.
3. **Record job IDs.** The `/train_job/create` response returns `job_id`. Persist it so that you can call `detail`, `stop`, and attach logs/telemetry to PR comments.
4. **Watch timelines.** The timeline block (`created`, `resource_prepared`, `run`, `finished`) is the authoritative source for diagnosing queue delays on Inspire.
5. **Enable troubleshooting flags.** Set `enable_troubleshoot` and `enable_notification` when running in CI so the monitor script receives richer sub-status messages.

For additional details, consult the auto-generated JSON schema definitions within the Inspire backend or the original Chinese-language reference provided by the platform vendor.
