"""v1-to-v2 browser API translation.

The platform retired the whole ``/api/v1/*`` REST tree on 2026-10-15 (#82): the gateway
answers ``404 Route Not Found`` for every v1 path, and ``/api/v2`` is an Action-style RPC
(``POST /api/v2/<service>?Action=<Name>`` with a JSON body), not a version bump of the same
paths.  The v2 envelope is also different::

    {"ResponseMetadata": {"Action": ..., "Service": ...}, "Result": {...}}          # success
    {"ResponseMetadata": {"Action": ..., "Error": {"Code": ..., "Message": ...}}}   # failure

and failures arrive as HTTP 200, so a status-code check alone would treat every v2 error as
a success with empty data.

This module owns the translation so no call site has to: :func:`resolve_v2_route` maps a
v1 REST endpoint (method + path) to a :class:`V2Route`, and :func:`call_v2_route` performs
the Action call and rewraps ``Result`` into the v1 envelope ``{"code": 0, "data": ...}``
that the browser_api modules already parse.  Routes whose request or response shape
differs carry small adapters; everything else passes the v1 body through unchanged,
which the protobuf-backed v2 backend accepts wherever the v1 fields survived.

Every mapping below was measured against the live gateway (qz.sii.edu.cn, 2026-10-16)
unless the docstring says otherwise.  Endpoints with no known v2 equivalent are listed in
:data:`UNMAPPED_V1_ENDPOINTS`: those calls keep their v1 URL and fail loudly with the
gateway's 404 rather than being silently mistranslated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from inspire.platform.web import session as web_session_module
from inspire.platform.web.session import WebSession
from inspire.platform.web.session.models import DEFAULT_WORKSPACE_ID


class V2RouteError(ValueError):
    """A v2 Action call failed (ResponseMetadata.Error, even at HTTP 200)."""


BodyAdapter = Callable[[dict[str, Any], dict[str, str]], dict[str, Any]]
ResultAdapter = Callable[[Any], Any]


@dataclass(frozen=True)
class V2Route:
    """One v1 REST endpoint -> one v2 Action call."""

    service: str
    action: str
    #: (v1_body, path_params) -> v2 request body. ``None`` keeps the v1 body verbatim.
    body_adapter: Optional[BodyAdapter] = None
    #: v2 ``Result`` -> the v1 ``data`` payload. ``None`` keeps Result verbatim.
    result_adapter: Optional[ResultAdapter] = None
    #: v1_body -> action name, when the action depends on the request (notebook operate).
    action_adapter: Optional[Callable[[dict[str, Any]], str]] = None
    #: v1_body -> service name, when the service depends on the request (task metrics).
    service_adapter: Optional[Callable[[dict[str, Any]], str]] = None
    #: Custom multi-call fetcher for endpoints that need more than one v2 request.
    fetch: Optional[Callable[..., dict[str, Any]]] = None
    doc: str = ""


def _keep_fields(*keep: str) -> BodyAdapter:
    """Body adapter that drops every top-level key except ``keep``.

    The v2 backend is protobuf-backed: an unknown JSON key is a hard
    ``InvalidParameter: proto: unknown field`` instead of being ignored.
    """

    def adapt(body: dict[str, Any], path_params: dict[str, str]) -> dict[str, Any]:
        return {key: value for key, value in (body or {}).items() if key in keep}

    return adapt


def _merge_path_id(key: str) -> BodyAdapter:
    """Body adapter that merges a path parameter into the body under ``key``."""

    def adapt(body: dict[str, Any], path_params: dict[str, str]) -> dict[str, Any]:
        merged = dict(body or {})
        value = path_params.get("id")
        if value:
            merged.setdefault(key, unquote(value))
        return merged

    return adapt


def _logs_filter_body(body: dict[str, Any]) -> dict[str, Any]:
    """Stringify the v1 log filter's timestamps, which the proto declares as ``string``.

    An int where the proto wants a string is an ``InvalidParameter``, so every log caller
    goes through here; only the sorter differs between the services (see the two callers).
    """
    adapted = dict(body or {})
    filtr = adapted.get("filter")
    if isinstance(filtr, dict):
        filtr = dict(filtr)
        for key in ("start_timestamp_ms", "end_timestamp_ms"):
            if key in filtr and filtr[key] is not None:
                filtr[key] = str(filtr[key])
        adapted["filter"] = filtr
    return adapted


def _logs_train_body(body: dict[str, Any], path_params: dict[str, str]) -> dict[str, Any]:
    """Adapt the v1 /logs/train body for train?Action=GetJobLog.

    Timestamps must be strings (the proto declares ``string``), and the sorter must be
    dropped: the v2 handler rejects every explicit sorter spelling we could construct
    ("time"/"log-id"/"logId", "descend"/"desc"/"asc", single or paired) with
    ``日志排序字段不合法，仅支持按时间 + log-id 排序``.  Without a sorter it answers in
    the same time-descending order the v1 default produced.
    """
    adapted = _logs_filter_body(body)
    adapted.pop("sorter", None)
    return adapted


def _logs_job_body(body: dict[str, Any], path_params: dict[str, str]) -> dict[str, Any]:
    """Adapt a v1 job-log body for hpc?Action=GetJobLog / inference_serving?Action=GetServingLog.

    Same timestamp rule as :func:`_logs_train_body` but the sorter stays: the console sends
    the two-part ``time`` + ``log-id.keyword`` sorter on these two services and the v2
    handler accepts that pair (measured live 2026-11-05, see #108) - which is exactly what
    ``日志排序字段不合法，仅支持按时间 + log-id 排序`` says it supports.
    """
    return _logs_filter_body(body)


def _image_list_service(body: dict[str, Any]) -> str:
    visibility = ((body or {}).get("filter") or {}).get("visibility")
    return "image_plaza" if visibility == "VISIBILITY_PUBLIC" else "image"


def _image_list_body(body: dict[str, Any], path_params: dict[str, str]) -> dict[str, Any]:
    v1 = body or {}
    page = v1.get("page", 1)
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    if _image_list_service(v1) == "image_plaza":
        source = (v1.get("filter") or {}).get("source") or "SOURCE_PUBLIC"
        image_filter = {"source": source}
    else:
        image_filter = dict(v1.get("filter") or {})
    return {
        "page": page,
        "page_size": v1.get("page_size", -1),
        "filter": image_filter,
    }


def _image_list_result(result: Any) -> Any:
    if not isinstance(result, dict):
        return {"images": [], "total": 0}
    return {
        "images": result.get("images", result.get("items", [])),
        "total": result.get("total", result.get("total_count", 0)),
    }


def _serving_list_body(body: dict[str, Any], path_params: dict[str, str]) -> dict[str, Any]:
    """v1 /inference_servings/list body -> inference_serving?Action=ListServings body.

    v2 rejects the v1 ``filter_by`` key outright and wants 1-based pages: the v1 REST
    endpoint accepted ``page: 0`` as the first page, the Action answers
    ``InvalidParameter: page or page_size invalid`` for it.
    """
    v1 = body or {}
    page = v1.get("page", 1)
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    return {
        "page": page,
        "page_size": v1.get("page_size", 20),
        "workspace_id": v1.get("workspace_id"),
    }


def _operate_notebook_action(body: dict[str, Any]) -> str:
    operation = str((body or {}).get("operation") or "").strip().upper()
    action = {"STOP": "StopNotebook", "START": "StartNotebook"}.get(operation)
    if not action:
        raise V2RouteError(f"Unsupported notebook operation: {operation!r}")
    return action


def _operate_notebook_body(body: dict[str, Any], path_params: dict[str, str]) -> dict[str, Any]:
    return {"notebook_id": (body or {}).get("notebook_id")}


def _schedule_body(body: dict[str, Any], path_params: dict[str, str]) -> dict[str, Any]:
    ws = (
        path_params.get("id") or path_params.get("workspace_id") or (body or {}).get("workspace_id")
    )
    return {"workspace_id": ws or DEFAULT_WORKSPACE_ID}


def _workdir_result(result: Any) -> Any:
    # GetJobWorkdir answers Result: {"data": "<path>"}; the v1 REST body was the bare string.
    if isinstance(result, dict):
        return result.get("data")
    return result


_METRIC_SERVICE_BY_TASK_TYPE = {
    "hpc_job": "hpc",
    "ray_job": "ray",
    "inference_serving_customize": "inference_serving",
    "inference_serving_exclusive": "inference_serving",
    # `GetTaskMetric` is one Action on every workload service, and a notebook's is on `notebook`:
    # `assets/interactiveModel.7ubd16HF.js` spells it `${n}?Action=GetTaskMetric` with
    # `const n="/api/v2/notebook"` at offset 55 and the builder at offset 3193, and the notebook
    # detail page calls it with `taskType:"interactive_modeling"` (`assets/InteractiveDetail.
    # B9Bbw0_y.js`, offset 47330).  Falling through to `train` was not a harmless default: the
    # train service answers `InvalidParameter: 参数错误` for that task type (measured 2026-11-06),
    # so `notebook metrics` had no service to reach even after its run-index route was fixed.
    "interactive_modeling": "notebook",
}

# Sub-paths that must never be captured by the /notebook/{id}, /model/{id} or
# /inference_servings/{id} catch-alls below.
_NOTEBOOK_SUBPATHES = (
    r"list$|create$|operate$|schedule|compute_groups$|lab/|code/|event|open/|proxy/"
)
_MODEL_SUBPATHES = r"list$|detail$|projects$|tags$|users$|inference_serv"
_ISERVING_SUBPATHES = r"list$|create$|scale$|update$|instances|events"


def _metric_service(body: dict[str, Any]) -> str:
    task_type = str(((body or {}).get("filter") or {}).get("task_type") or "").strip()
    return _METRIC_SERVICE_BY_TASK_TYPE.get(task_type, "train")


# ---------------------------------------------------------------------------
# Route table: (v1 method, v1 endpoint pattern, route)
# ---------------------------------------------------------------------------

_ROUTES: list[tuple[str, re.Pattern[str], V2Route]] = [
    # user service
    (
        "GET",
        re.compile(r"^/user/detail$"),
        V2Route("user", "GetUserDetail", body_adapter=lambda b, p: {}),
    ),
    (
        "GET",
        re.compile(r"^/user/routes/(?P<id>[^/]+)$"),
        V2Route(
            "user",
            "GetRoutes",
            body_adapter=lambda b, p: {"workspace_id": unquote(p.get("id") or "") or "default"},
        ),
    ),
    (
        "GET",
        re.compile(r"^/user/permissions/(?P<id>[^/]+)$"),
        V2Route(
            "user",
            "GetPermissions",
            body_adapter=lambda b, p: {"workspace_id": unquote(p.get("id") or "")},
        ),
    ),
    # The inference-serving API keys *are* the userCenter API keys: the serving console
    # lists them with GetMyAPIList and reads one with GetAPIKeyPlaintext (see
    # `inferenceDetailService.js` in the console bundle).  Both v1 paths below routed
    # nowhere - the gateway answered `404 page not found` - and nothing noticed, because
    # the only caller is the scheduled live review, which nobody ran before #106.
    # Measured against the live gateway 2026-11-05: GetMyAPIList takes no body and answers
    # `Result.items[] = {key_id, name, value, created_at}`; GetAPIKeyPlaintext answers
    # `ResourceNotFound` for an unknown `api_key_id`, which is the answer that proves the
    # field name and its placement (a wrong one would be an `InvalidParameter`).
    (
        "GET",
        re.compile(r"^/user/my-api-key/list$"),
        V2Route("user", "GetMyAPIList", body_adapter=lambda b, p: {}),
    ),
    (
        "GET",
        re.compile(r"^/user/my-api-key/show/(?P<id>[^/]+)$"),
        V2Route("user", "GetAPIKeyPlaintext", body_adapter=_merge_path_id("api_key_id")),
    ),
    # notebook service
    ("POST", re.compile(r"^/notebook/list$"), V2Route("notebook", "ListNotebooks")),
    ("POST", re.compile(r"^/notebook/create$"), V2Route("notebook", "CreateNotebook")),
    (
        "POST",
        re.compile(r"^/notebook/operate$"),
        V2Route(
            "notebook",
            "StopNotebook",
            body_adapter=_operate_notebook_body,
            action_adapter=_operate_notebook_action,
            doc="action picked from body.operation (STOP/START)",
        ),
    ),
    (
        "POST",
        re.compile(r"^/notebook/compute_groups$"),
        V2Route(
            "workspace",
            "ListLogicComputeGroups",
            body_adapter=lambda b, p: {
                "filter": {"workspace_id": (b or {}).get("workspace_id")},
                "page_num": 1,
                "page_size": 100,
            },
            result_adapter=lambda result: (
                (result or {}).get("logic_compute_groups", []) if isinstance(result, dict) else []
            ),
            doc="v1 answered a bare list; the v2 Result nests it under logic_compute_groups",
        ),
    ),
    (
        "GET",
        re.compile(r"^/notebook/schedule(?:/(?P<id>[^/?]+))?$"),
        V2Route(
            "notebook",
            "GetNotebookScheduleConfig",
            body_adapter=_schedule_body,
        ),
    ),
    (
        "GET",
        re.compile(rf"^/notebook/(?!{_NOTEBOOK_SUBPATHES})(?P<id>[^/?]+)$"),
        V2Route(
            "notebook",
            "GetNotebook",
            body_adapter=_merge_path_id("notebook_id"),
        ),
    ),
    (
        "PUT",
        re.compile(rf"^/notebook/(?!{_NOTEBOOK_SUBPATHES})(?P<id>[^/?]+)$"),
        V2Route(
            "notebook",
            "UpdateNotebook",
            body_adapter=_merge_path_id("notebook_id"),
        ),
    ),
    (
        "DELETE",
        re.compile(rf"^/notebook/(?!{_NOTEBOOK_SUBPATHES})(?P<id>[^/?]+)$"),
        V2Route(
            "notebook",
            "DeleteNotebook",
            body_adapter=_merge_path_id("notebook_id"),
        ),
    ),
    # images: personal/official catalog and public plaza are separate v2 services
    (
        "POST",
        re.compile(r"^/image/list$"),
        V2Route(
            "image",
            "ListImages",
            body_adapter=_image_list_body,
            result_adapter=_image_list_result,
            service_adapter=_image_list_service,
        ),
    ),
    ("POST", re.compile(r"^/image/create$"), V2Route("image", "CreateImage")),
    (
        "POST",
        re.compile(r"^/image/robot$"),
        V2Route("image", "CreateImageRegistryRobot"),
    ),
    ("POST", re.compile(r"^/image/sync$"), V2Route("image", "SyncImage")),
    ("POST", re.compile(r"^/image/preheat$"), V2Route("image", "PreheatImage")),
    (
        "DELETE",
        re.compile(r"^/image/(?P<id>[^/]+)$"),
        V2Route("image", "DeleteImage", body_adapter=_merge_path_id("image_id")),
    ),
    ("POST", re.compile(r"^/mirror/save$"), V2Route("notebook", "SaveMirror")),
    # The notebook run indexes (measured 2026-11-06).  `POST /run_index/list` kept its v1 URL and met the
    # gateway's `404 page not found`, which is what made `notebook metrics` fail before it ever
    # reached a metric.  The console reads the same list through its notebook service module
    # (`assets/interactiveModel.7ubd16HF.js`, offset 1599: `async function v(o){return e(
    # `${n}?Action=ListRunIndex`,{method:t.POST,params:{notebook_id:o}})}` with `const n=
    # "/api/v2/notebook"` at offset 55), and the live Action answers the v1 body unchanged with
    # `Result: {list: [{index, start_time, end_time}], total}` (measured 2026-11-06 on a running
    # notebook) - the shape the caller already parses, so neither body nor result is adapted.  An
    # empty body answers `InvalidParameter: NotebookId is required`, which is the platform naming
    # the one field the v1 body already sent: the Action is alive and it wants `notebook_id`.
    (
        "POST",
        re.compile(r"^/run_index/list$"),
        V2Route("notebook", "ListRunIndex"),
    ),
    # compute resources: v1 group-resource detail (live gpu_used / low-priority usage)
    (
        "GET",
        re.compile(r"^/compute_resources/logic_compute_groups/(?P<id>[^/]+)$"),
        V2Route(
            "workspace",
            "GetLogicComputeGroupResource",
            body_adapter=lambda b, p: {
                "logic_compute_group_id": unquote(p.get("id") or ""),
                "workspace_id": (
                    p.get("workspace_id") or (b or {}).get("workspace_id") or DEFAULT_WORKSPACE_ID
                ),
            },
            doc=(
                "v2 moved this onto the workspace service; it keeps gpu_total/gpu_used/"
                "gpu_low_priority_used, which the old note claimed no action carried"
            ),
        ),
    ),
    # project service
    ("POST", re.compile(r"^/project/list$"), V2Route("project", "ListProjects")),
    # file service
    (
        "POST",
        re.compile(r"^/file/sftpgo/connection_info$"),
        V2Route("file", "GetSftpgoConnectionInfo"),
    ),
    (
        "POST",
        re.compile(r"^/file/get_system_storage_type_list$"),
        V2Route("file", "GetSystemStorageTypeList"),
    ),
    ("POST", re.compile(r"^/file/dir/list$"), V2Route("file", "GetDirList")),
    # train service
    (
        "POST",
        re.compile(r"^/train_job/create$"),
        V2Route(
            "train",
            "CreateJobConsole",
            doc="console action; the OpenAPI CreateJob drops dataset_info (#80)",
        ),
    ),
    ("POST", re.compile(r"^/train_job/instance_list$"), V2Route("train", "ListJobInstances")),
    (
        "POST",
        re.compile(r"^/train_job/job_event_list$"),
        V2Route(
            "train",
            "ListJobEvents",
            body_adapter=lambda b, p: {
                "pageNum": -1,
                "pageSize": 200,
                "filter": {"object_ids": [(b or {}).get("jobId")], "object_type": "job"},
                "sorter": [{"field": "last_timestamp", "sort": "ascend"}],
            },
            doc="v2 uses the console's paginated job object filter instead of the v1 jobId",
        ),
    ),
    (
        "POST",
        re.compile(r"^/train_job/users$"),
        V2Route(
            "train",
            "ListJobCreators",
            body_adapter=_keep_fields("workspace_id"),
            doc="v2 rejects the v1 pagination keys",
        ),
    ),
    (
        "POST",
        re.compile(r"^/train_job/workdir$"),
        V2Route(
            "train",
            "GetJobWorkdir",
            result_adapter=_workdir_result,
            doc="v2 wraps the path string in Result.data",
        ),
    ),
    (
        "POST",
        re.compile(r"^/logs/train$"),
        V2Route(
            "train",
            "GetJobLog",
            body_adapter=_logs_train_body,
            doc="string timestamps; sorter dropped (rejected server-side)",
        ),
    ),
    # hpc and ray job services.  The v1 paths here were still being called after the v1 tree
    # retired (measured live 2026-11-05, see #108): each one answered the gateway's
    # `404 page not found`, and the Actions below are the ones the console itself calls
    # (`assets/hpcService.TbdH5hUr.js` and `assets/constant.Cl3Yx5fQ.js`, read out of the
    # bundles).  Every v1 body these routes receive was accepted by its Action - only the
    # domain answer differed ("job not found"/AccessForbidden for jobs this account may not
    # read), which is what makes the mapping a measurement rather than a guess.
    (
        "POST",
        re.compile(r"^/hpc_jobs/events/list$"),
        V2Route(
            "hpc",
            "ListJobEvents",
            doc="same body as the console sends (pageNum/pageSize/filter.object_ids object_type=HPC_JOB)",
        ),
    ),
    (
        "POST",
        re.compile(r"^/hpc_jobs/instances/list$"),
        V2Route(
            "hpc",
            "ListJobInstances",
            doc="invalid without job_id: `InvalidParameter: JobId is required`",
        ),
    ),
    (
        "DELETE",
        re.compile(r"^/hpc_jobs/(?P<id>[^/]+)$"),
        V2Route(
            "hpc",
            "DeleteJob",
            body_adapter=_merge_path_id("job_id"),
            doc="the console calls DeleteJob with {job_id}; this path was a 404 for the same reason as the rest",
        ),
    ),
    (
        "POST",
        re.compile(r"^/logs/hpc$"),
        V2Route(
            "hpc",
            "GetJobLog",
            body_adapter=_logs_job_body,
            doc="pod names identify the job; the console also sends job_id",
        ),
    ),
    (
        "POST",
        re.compile(r"^/ray_job/events/list$"),
        V2Route(
            "ray",
            "ListJobEvents",
            doc="accepts the v1 ray_job_id body (measured: `ResourceNotFound: ray job not found`)",
        ),
    ),
    (
        "POST",
        re.compile(r"^/ray_job/instances/list$"),
        V2Route("ray", "ListJobInstances", doc="accepts ray_job_id/page_num/page_size"),
    ),
    # metrics
    (
        "POST",
        re.compile(r"^/cluster_metric/resource_metric_by_time$"),
        V2Route(
            "train",
            "GetTaskMetric",
            service_adapter=_metric_service,
            doc="service picked from filter.task_type; Result keeps the time_seris_metric_groups typo",
        ),
    ),
    # Both dimension views live on the workspace service in v2.  The console's node view
    # sends `workspace?Action=ListNodeDimension` and its task view
    # `workspace?Action=ListTaskDimension` (measured live 2026-11-20); the `cluster` service
    # answers `AccessForbidden: Access denied` to an ordinary account for both.
    (
        "POST",
        re.compile(r"^/cluster_metric/list_node_dimension$"),
        V2Route("workspace", "ListNodeDimension"),
    ),
    (
        "POST",
        re.compile(r"^/cluster_metric/list_task_dimension$"),
        V2Route("workspace", "ListTaskDimension"),
    ),
    (
        "POST",
        re.compile(r"^/resource_prices/logic_compute_groups/?$"),
        V2Route(
            "resource-price",
            "GetLogicComputeGroupResourceSpecPrices",
            doc="same body (schedule_config_type distinguishes TRAIN/DSW/SERVE); Result keeps lcg_resource_spec_prices",
        ),
    ),
    # workspace service
    (
        "POST",
        re.compile(r"^/workspaces/list$"),
        V2Route(
            "workspace",
            "ListWorkspaces",
            body_adapter=lambda b, p: {},
            doc="account-wide enumeration; Result.items carries workspace metadata",
        ),
    ),
    (
        "POST",
        re.compile(r"^/logic_compute_groups/list$"),
        V2Route("workspace", "ListLogicComputeGroups"),
    ),
    # model hub
    ("POST", re.compile(r"^/model/list$"), V2Route("model-hub", "ListModels")),
    ("POST", re.compile(r"^/model/detail$"), V2Route("model-hub", "GetModelDetails")),
    (
        "GET",
        re.compile(rf"^/model/(?!{_MODEL_SUBPATHES})(?P<id>[^/?]+)$"),
        V2Route(
            "model-hub",
            "GetModelDetails",
            body_adapter=_merge_path_id("model_id"),
        ),
    ),
    (
        "GET",
        re.compile(rf"^/model/(?!{_MODEL_SUBPATHES})(?P<id>[^/?]+)/versions$"),
        V2Route(
            "model-hub",
            "GetAllVersionModelsById",
            body_adapter=_merge_path_id("model_id"),
        ),
    ),
    (
        "GET",
        re.compile(r"^/model/projects$"),
        V2Route("model-hub", "ListProjects", body_adapter=lambda b, p: {}),
    ),
    (
        "POST",
        re.compile(r"^/model/inference_servings$"),
        V2Route("model-hub", "GetModelRelatedServings"),
    ),
    (
        "POST",
        re.compile(r"^/model/inference_serving/pending$"),
        V2Route("model-hub", "GetHasModelPendingServing"),
    ),
    # model plaza
    ("POST", re.compile(r"^/model_plaza/list$"), V2Route("model_plaza", "ListModels")),
    # inference servings (v2 service name is the singular inference_serving)
    (
        "POST",
        re.compile(r"^/inference_servings/list$"),
        V2Route(
            "inference_serving",
            "ListServings",
            body_adapter=_serving_list_body,
            doc="v2 rejects the v1 filter_by key and wants 1-based pages",
        ),
    ),
    (
        "POST",
        re.compile(r"^/inference_servings/create$"),
        V2Route("inference_serving", "CreateServingConsole"),
    ),
    (
        "POST",
        re.compile(r"^/inference_servings/scale$"),
        V2Route("inference_serving", "ScaleServing"),
    ),
    (
        "POST",
        re.compile(r"^/inference_servings/update$"),
        V2Route("inference_serving", "UpdateServing"),
    ),
    (
        "GET",
        re.compile(rf"^/inference_servings/(?!{_ISERVING_SUBPATHES})(?P<id>[^/?]+)$"),
        V2Route(
            "inference_serving",
            "GetServing",
            body_adapter=_merge_path_id("inference_serving_id"),
        ),
    ),
    (
        "DELETE",
        re.compile(rf"^/inference_servings/(?!{_ISERVING_SUBPATHES})(?P<id>[^/?]+)$"),
        V2Route(
            "inference_serving",
            "DeleteServing",
            body_adapter=_merge_path_id("inference_serving_id"),
        ),
    ),
    (
        "POST",
        re.compile(r"^/inference_servings/(?P<id>[^/]+)/stop$"),
        V2Route(
            "inference_serving",
            "StopServing",
            body_adapter=_merge_path_id("inference_serving_id"),
        ),
    ),
    (
        "POST",
        re.compile(r"^/inference_servings/(?P<id>[^/]+)/start$"),
        V2Route(
            "inference_serving",
            "StartServing",
            body_adapter=_merge_path_id("inference_serving_id"),
        ),
    ),
    (
        "POST",
        re.compile(r"^/inference_servings/(?P<id>[^/]+)/validate_custom_domain$"),
        V2Route(
            "inference_serving",
            "ValidateCustomDomain",
            body_adapter=_merge_path_id("inference_serving_id"),
        ),
    ),
    (
        "POST",
        re.compile(r"^/inference_servings/instances/list$"),
        V2Route("inference_serving", "ListServingInstances"),
    ),
    (
        "POST",
        re.compile(r"^/inference_servings/events/list$"),
        V2Route("inference_serving", "ListServingEvents"),
    ),
    (
        "GET",
        re.compile(r"^/inference_servings/(?P<id>[^/]+)/versions$"),
        V2Route(
            "inference_serving",
            "ListServingVersions",
            body_adapter=_merge_path_id("inference_serving_id"),
            doc="the console calls ListServingVersions with {inference_serving_id}; the v1 sub-path was a 404 (see #108)",
        ),
    ),
    (
        "POST",
        re.compile(r"^/logs/inference_serving$"),
        V2Route(
            "inference_serving",
            "GetServingLog",
            body_adapter=_logs_job_body,
            doc="needs inference_serving_id in the body; without it the service answers InternalError",
        ),
    ),
]

#: v1 endpoints with no v2 mapping (measured 2026-10-16, extended 2026-11-05).  Calls keep
#: their v1 URL and surface the gateway 404 instead of being mistranslated, which is the
#: honest outcome - but it is also silent from the CLI's side, so a path belongs here only
#: as a decision someone wrote down.  `tests/contract/test_endpoint_coverage.py` enforces
#: that every v1 path the shipped code asks for is either mapped or declared here.
#:
#: `POST /run_index/list` stood here claiming that "notebook metrics moved to
#: GetRealtimeNotebookMetric".  It had not: the notebook detail page fetches its history through
#: `notebook?Action=GetTaskMetric`, and `ListRunIndex` is the successor of this path.  A
#: declaration that names an Action nobody calls is as misleading as no declaration at all, which
#: is why this one was deleted rather than corrected - the notebook history call resolves through
#: `ListRunIndex` today (as `notebook resource --since`), and the
#: coverage test fails naming this path if the route ever goes back to being declared dead.
UNMAPPED_V1_ENDPOINTS: dict[str, str] = {
    "POST /model/tags": "no model-hub action found (GetTags/ListTags/GetModelTags all InvalidAction)",
    "POST /model/users": "no model-hub action found (measured 2026-10-16); no successor tried since",
    # The date is the point of the note: an undated "no action found" reads as settled and stops the
    # next person looking, which is the mistake LESSONS.md records for ``POST /run_index/list``.
    # Re-checked 2026-11-06 against the console's own discovery report of 2026-11-05, which names no
    # Action containing ssh or key at all - so this is still an open search, and ``user ssh-keys``
    # answers 404 until somebody finds what the console calls today.
    "GET /ssh/list": "no v2 action found on user/cluster/workspace services (measured 2026-11-06); no successor tried since",
    "GET /notebook/{id}/events": "cluster?Action=ListNodeEvents is node-scoped, not notebook-scoped",
    # Six endpoints stood here on 2026-11-05 (see #108) and now carry a route mapping above;
    # two more were found by teaching the coverage test to read templated paths.  The
    # declarations were deleted rather than reworded, so a regression is a red test.
    "POST /ray_job/scaling_histories/list": "no ray action found (measured 2026-10-16); no successor tried since",
}


def _strip_prefix(path: str, prefixes: tuple[str, ...]) -> str:
    for prefix in prefixes:
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


def resolve_v2_route(
    method: str,
    path: str,
    *,
    prefixes: tuple[str, ...] = ("/api/v1", "/api/v2"),
) -> tuple[V2Route, dict[str, str]] | None:
    """Resolve a (possibly prefixed) browser API path to its v2 route.

    Returns ``(route, path_params)`` or ``None`` when the endpoint has no mapping.
    Exact routes are listed before the ``{id}`` catch-alls in :data:`_ROUTES`, and the
    catch-alls carry negative lookaheads so sub-paths fall through untouched.
    """
    split = urlsplit(path)
    endpoint = _strip_prefix(split.path, prefixes)
    query_params = {key: values[0] for key, values in parse_qs(split.query).items()}

    method_upper = method.upper()
    for route_method, pattern, route in _ROUTES:
        if route_method != method_upper:
            continue
        match = pattern.match(endpoint)
        if not match:
            continue
        params = {key: unquote(value) for key, value in match.groupdict().items() if value}
        params.update(query_params)
        return route, params
    return None


def _action_result(payload: Any) -> dict[str, Any]:
    """Decode a v2 Action envelope: Result on success, raise on ResponseMetadata.Error."""
    if not isinstance(payload, dict):
        raise V2RouteError(f"v2 Action response is not an object: {payload!r}")
    metadata = payload.get("ResponseMetadata")
    if isinstance(metadata, dict):
        error = metadata.get("Error")
        if isinstance(error, dict):
            code = error.get("Code") or "Error"
            message = error.get("Message") or "unknown error"
            raise V2RouteError(f"API error {code}: {message}")
    result = payload.get("Result")
    if result is None:
        return {}
    return result


def call_v2_route(
    session: WebSession,
    route: V2Route,
    path_params: dict[str, str],
    *,
    body: dict[str, Any] | None,
    base_url: str,
    referer: str,
    timeout: int = 30,
    rate_limit_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one v2 Action call and rewrap the answer into the v1 envelope."""
    rate_limit_options = rate_limit_options or {}
    if route.fetch is not None:
        return route.fetch(
            session=session,
            body=dict(body or {}),
            base_url=base_url,
            referer=referer,
            timeout=timeout,
            rate_limit_options=rate_limit_options,
        )

    service = (
        route.service_adapter(body or {}) if route.service_adapter is not None else route.service
    )
    action = route.action_adapter(body or {}) if route.action_adapter is not None else route.action

    if route.body_adapter is not None:
        v2_body = route.body_adapter(dict(body or {}), path_params) or {}
    else:
        v2_body = dict(body or {})

    payload = web_session_module.request_json(
        session,
        "POST",
        f"{base_url}/api/v2/{service}?Action={action}",
        headers={"Referer": referer},
        body=v2_body,
        timeout=timeout,
        **rate_limit_options,
    )
    result = _action_result(payload)
    data = route.result_adapter(result) if route.result_adapter is not None else result
    return {"code": 0, "data": data, "message": ""}


def action_url(base_url: str, service: str, action: str) -> str:
    """Build a v2 Action URL for call sites that live outside the route table."""
    return f"{base_url.rstrip('/')}/api/v2/{service}?Action={action}"


def decode_action_response(payload: Any) -> dict[str, Any]:
    """Decode a v2 Action envelope for direct call sites; returns the Result object."""
    return _action_result(payload)


__all__ = [
    "UNMAPPED_V1_ENDPOINTS",
    "V2Route",
    "V2RouteError",
    "action_url",
    "call_v2_route",
    "decode_action_response",
    "resolve_v2_route",
]
