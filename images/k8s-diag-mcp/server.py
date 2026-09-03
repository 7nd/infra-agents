"""k8s-diag-mcp — узкий read-only MCP-тул для n8n ops-агента.

Единственные разрешённые операции: чтение состояния (поды/деплойменты/
сервисы/ingress/логи в namespace managed, статус Flux-объектов в
flux-system) и flux_reconcile (форс-синхронизация уже закоммиченного в git
— патчит только аннотацию reconcile.fluxcd.io/requestedAt, тот же приём,
что делает `flux reconcile` CLI). Никакого create/update/delete рабочих
ресурсов — ни здесь в коде (allow-list на kind), ни на уровне RBAC
ServiceAccount'а, под которым запущен под (см. infra/managed-stands/rbac.yaml
и flux-rbac.yaml в unitum-demo-k8s-infra).
"""

import datetime
import json
import logging
import time

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("k8s-diag-mcp")

config.load_incluster_config()
core_v1 = client.CoreV1Api()
apps_v1 = client.AppsV1Api()
net_v1 = client.NetworkingV1Api()
custom = client.CustomObjectsApi()

DIAG_NAMESPACE = "managed"
FLUX_NAMESPACE = "flux-system"
BASE_DOMAIN = "managed.hightps.online"

_K8S_KINDS = {"pods", "deployments", "services", "ingresses", "events"}

_FLUX_GVR = {
    "Kustomization": ("kustomize.toolkit.fluxcd.io", "v1", "kustomizations"),
    "GitRepository": ("source.toolkit.fluxcd.io", "v1", "gitrepositories"),
    "HelmRelease": ("helm.toolkit.fluxcd.io", "v2", "helmreleases"),
}

mcp = FastMCP("k8s-diag", host="0.0.0.0", port=8080)


def _err(msg: str) -> str:
    log.warning(msg)
    return f"ERROR: {msg}"


# --------------------------------------------------------------------------
# internal structured helpers (dict-returning) — shared by the individual
# read-only tools below AND finalize_deploy, so finalize_deploy doesn't have
# to string-parse "ERROR: ..." out of the tool-facing string responses.
# --------------------------------------------------------------------------


def _k8s_get_dict(kind: str, namespace: str = DIAG_NAMESPACE, name: str | None = None) -> dict:
    kind = kind.lower()
    if kind not in _K8S_KINDS:
        return {"ok": False, "error_code": "k8s.unknown_kind", "message": f"kind must be one of {sorted(_K8S_KINDS)}, got {kind!r}"}
    try:
        if kind == "pods":
            api = core_v1.read_namespaced_pod(name, namespace) if name else core_v1.list_namespaced_pod(namespace)
        elif kind == "deployments":
            api = apps_v1.read_namespaced_deployment(name, namespace) if name else apps_v1.list_namespaced_deployment(namespace)
        elif kind == "services":
            api = core_v1.read_namespaced_service(name, namespace) if name else core_v1.list_namespaced_service(namespace)
        elif kind == "ingresses":
            api = net_v1.read_namespaced_ingress(name, namespace) if name else net_v1.list_namespaced_ingress(namespace)
        elif kind == "events":
            api = core_v1.list_namespaced_event(namespace)
        sanitized = client.ApiClient().sanitize_for_serialization(api)
        items = sanitized.get("items", [sanitized]) if isinstance(sanitized, dict) else [sanitized]
        return {"ok": True, "items": items, "raw": sanitized}
    except ApiException as e:
        code = "k8s.not_found" if e.status == 404 else "k8s.api_error"
        return {"ok": False, "error_code": code, "message": f"{e.status}: {e.reason}"}


def _k8s_logs_dict(pod: str, namespace: str = DIAG_NAMESPACE, container: str | None = None, tail_lines: int = 200) -> dict:
    try:
        logs = core_v1.read_namespaced_pod_log(
            name=pod, namespace=namespace, container=container, tail_lines=min(tail_lines, 2000)
        )
        return {"ok": True, "logs": logs}
    except ApiException as e:
        code = "k8s.not_found" if e.status == 404 else "k8s.api_error"
        return {"ok": False, "error_code": code, "message": f"{e.status}: {e.reason}"}


def _flux_get_dict(kind: str, name: str) -> dict:
    if kind not in _FLUX_GVR:
        return {"ok": False, "error_code": "flux.unknown_kind", "message": f"kind must be one of {sorted(_FLUX_GVR)}, got {kind!r}"}
    group, version, plural = _FLUX_GVR[kind]
    try:
        obj = custom.get_namespaced_custom_object(group, version, FLUX_NAMESPACE, plural, name)
        return {"ok": True, "status": obj.get("status", {})}
    except ApiException as e:
        code = "flux.not_found" if e.status == 404 else "flux.api_error"
        return {"ok": False, "error_code": code, "message": f"{e.status}: {e.reason}"}


def _flux_reconcile_dict(kind: str, name: str) -> dict:
    if kind not in _FLUX_GVR:
        return {"ok": False, "error_code": "flux.unknown_kind", "message": f"kind must be one of {sorted(_FLUX_GVR)}, got {kind!r}"}
    group, version, plural = _FLUX_GVR[kind]
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    patch = {"metadata": {"annotations": {"reconcile.fluxcd.io/requestedAt": now}}}
    try:
        custom.patch_namespaced_custom_object(group, version, FLUX_NAMESPACE, plural, name, patch)
        return {"ok": True, "requested_at": now}
    except ApiException as e:
        code = "flux.not_found" if e.status == 404 else "flux.api_error"
        return {"ok": False, "error_code": code, "message": f"{e.status}: {e.reason}"}


# --------------------------------------------------------------------------
# tool-facing wrappers (unchanged external string contract)
# --------------------------------------------------------------------------


@mcp.tool()
def k8s_get(kind: str, namespace: str = DIAG_NAMESPACE, name: str | None = None) -> str:
    """Read-only: список или один объект в кластере.

    kind — один из: pods, deployments, services, ingresses, events.
    namespace жёстко ограничен на "managed" со стороны RBAC — другой
    namespace просто вернёт 403 от API-сервера, не от этой функции.
    """
    r = _k8s_get_dict(kind, namespace, name)
    if not r["ok"]:
        return _err(r["message"])
    return json.dumps(r["raw"], ensure_ascii=False, default=str)


@mcp.tool()
def k8s_logs(pod: str, namespace: str = DIAG_NAMESPACE, container: str | None = None, tail_lines: int = 200) -> str:
    """Read-only: последние строки логов пода (по умолчанию 200)."""
    r = _k8s_logs_dict(pod, namespace, container, tail_lines)
    if not r["ok"]:
        return _err(r["message"])
    return r["logs"]


@mcp.tool()
def flux_get(kind: str, name: str) -> str:
    """Read-only: статус Flux-объекта (Kustomization/GitRepository/HelmRelease)
    в namespace flux-system — в первую очередь смотри .status.conditions,
    там причина, если реконсиляция не прошла."""
    r = _flux_get_dict(kind, name)
    if not r["ok"]:
        return _err(r["message"])
    return json.dumps(r["status"], ensure_ascii=False, default=str)


@mcp.tool()
def flux_reconcile(kind: str, name: str) -> str:
    """Форсирует немедленную реконсиляцию Kustomization/GitRepository/
    HelmRelease вместо ожидания штатного interval — эквивалент
    `flux reconcile <kind> <name>`. НЕ создаёт, не меняет и не удаляет
    ничего, кроме собственной аннотации на этом Flux-объекте; реально
    применяемые манифесты берутся из git."""
    r = _flux_reconcile_dict(kind, name)
    if not r["ok"]:
        return _err(r["message"])
    return f"reconcile requested for {kind}/{name} at {r['requested_at']}"


@mcp.tool()
def finalize_deploy(name: str, wait_seconds: int = 20) -> str:
    """Один вызов вместо ручной последовательности из двух flux_reconcile +
    паузы + flux_get/k8s_get/k8s_logs. Вызывай ПОСЛЕ успешного
    kustomize_build (helm-mcp) и ПОСЛЕ того, как register_app_in_control_repo
    подтвердил запись apps/<name>.yaml + обновление apps/kustomization.yaml
    в control-репо — этот тул не пишет ничего в git, только форсирует
    реконсиляцию уже закоммиченного и опрашивает результат.

    name — имя приложения (репозиторий ops/<name>, GitRepository/
    Kustomization "app-<name>" в flux-system, под-префикс "<name>-",
    хост <name>.managed.hightps.online — та же конвенция, что
    apps/_template.yaml control-репо).

    Возвращает {"status": "ready"|"pending"|"failed", "url": "...",
    "flux_status": {...}, "pods": [...], "errors": [...]}.
    status="pending" значит: реконсиляция запущена, но за wait_seconds
    Kustomization ещё не дошла до Ready:True — это НЕ провал, вызови
    finalize_deploy ещё раз через немного времени вместо того, чтобы
    сразу сообщать пользователю об ошибке. status="failed" — реальная
    проблема (см. errors[].error_code), не таймаут."""
    git_repo_name = f"app-{name}"
    kustomization_name = f"app-{name}"
    errors: list[dict] = []

    for kind, obj_name in (("GitRepository", git_repo_name), ("Kustomization", kustomization_name)):
        r = _flux_reconcile_dict(kind, obj_name)
        if not r["ok"]:
            errors.append({"error_code": r["error_code"], "message": f"reconcile {kind}/{obj_name}: {r['message']}"})

    time.sleep(max(0, min(wait_seconds, 60)))

    flux_status_result = _flux_get_dict("Kustomization", kustomization_name)
    ready = False
    still_progressing = False
    flux_status = {}
    if flux_status_result["ok"]:
        flux_status = flux_status_result["status"]
        conditions = flux_status.get("conditions", [])
        ready_cond = next((c for c in conditions if c.get("type") == "Ready"), None)
        if ready_cond and ready_cond.get("status") == "True":
            ready = True
        elif ready_cond and ready_cond.get("status") == "False" and ready_cond.get("reason") == "Progressing":
            still_progressing = True
        elif ready_cond and ready_cond.get("status") == "False":
            errors.append({
                "error_code": "flux.kustomization_not_ready",
                "message": ready_cond.get("message", ""),
                "reason": ready_cond.get("reason", ""),
            })
        else:
            still_progressing = True  # no Ready condition yet — object just created
    else:
        still_progressing = flux_status_result.get("error_code") == "flux.not_found"
        if not still_progressing:
            errors.append(flux_status_result)

    _HARD_FAIL_WAITING_REASONS = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError"}

    pods_summary = []
    pods_result = _k8s_get_dict("pods", DIAG_NAMESPACE)
    if pods_result["ok"]:
        for pod in pods_result["items"]:
            pod_name = pod.get("metadata", {}).get("name", "")
            if not pod_name.startswith(f"{name}-"):
                continue
            phase = pod.get("status", {}).get("phase")
            container_statuses = pod.get("status", {}).get("containerStatuses") or []
            waiting_reasons = {
                cs["state"]["waiting"]["reason"]
                for cs in container_statuses
                if cs.get("state", {}).get("waiting", {}).get("reason")
            }
            pods_summary.append({"name": pod_name, "phase": phase, "waiting_reasons": sorted(waiting_reasons)})
            hard_fail = phase == "Failed" or bool(waiting_reasons & _HARD_FAIL_WAITING_REASONS)
            # Pending/ContainerCreating while the Kustomization is still
            # progressing is normal startup, not a failure — only flag it
            # once reconciliation itself has settled (ready or a hard error
            # already reported) or the pod is showing an actual crash/pull
            # loop reason regardless of progress state.
            if hard_fail or (phase not in ("Running", "Succeeded") and not still_progressing):
                logs_result = _k8s_logs_dict(pod_name, DIAG_NAMESPACE, tail_lines=50)
                errors.append({
                    "error_code": "pod.crash_loop" if waiting_reasons & _HARD_FAIL_WAITING_REASONS else "pod.not_running",
                    "pod": pod_name,
                    "phase": phase,
                    "waiting_reasons": sorted(waiting_reasons),
                    "logs_tail": logs_result.get("logs") or logs_result.get("message", ""),
                })
    else:
        errors.append(pods_result)

    if ready and not errors:
        status = "ready"
    elif errors:
        status = "failed"
    else:
        status = "pending"  # still_progressing, no hard errors yet

    return json.dumps({
        "status": status,
        "url": f"https://{name}.{BASE_DOMAIN}",
        "flux_status": flux_status,
        "pods": pods_summary,
        "errors": errors,
    }, ensure_ascii=False)


# plain HTTP passthrough for the deterministic graph (see helm-mcp's
# server.py for the full rationale) — same tools, same validation, second
# transport for non-agentic callers that have no MCP session.


@mcp.custom_route("/call/{tool_name}", methods=["POST"])
async def call_tool_route(request: Request) -> JSONResponse:
    tool_name = request.path_params["tool_name"]
    try:
        arguments = await request.json()
    except Exception:
        arguments = {}
    try:
        result = await mcp.call_tool(tool_name, arguments)
    except Exception as e:
        return JSONResponse({"ok": False, "error_code": "mcp.call_failed", "message": str(e)})
    # FastMCP.call_tool(..., convert_result=True) returns
    # (list[ContentBlock], structured_content_dict) for a str-returning
    # tool — structured_content is {"result": "<the str the tool returned>"}.
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict) and "result" in result[1]:
        text = result[1]["result"]
        try:
            return JSONResponse(json.loads(text))
        except (json.JSONDecodeError, TypeError):
            return JSONResponse({"raw": text})
    if isinstance(result, dict):
        return JSONResponse(result)
    content_list = result[0] if isinstance(result, tuple) else result
    try:
        texts = [getattr(item, "text", None) for item in content_list if getattr(item, "text", None) is not None]
    except TypeError:
        return JSONResponse({"raw": str(result)})
    text = "\n".join(t for t in texts if t)
    try:
        return JSONResponse(json.loads(text))
    except (json.JSONDecodeError, TypeError):
        return JSONResponse({"raw": text})


if __name__ == "__main__":
    mcp.run(transport="sse")
