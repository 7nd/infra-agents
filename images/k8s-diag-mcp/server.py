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
import logging

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("k8s-diag-mcp")

config.load_incluster_config()
core_v1 = client.CoreV1Api()
apps_v1 = client.AppsV1Api()
net_v1 = client.NetworkingV1Api()
custom = client.CustomObjectsApi()

DIAG_NAMESPACE = "managed"
FLUX_NAMESPACE = "flux-system"

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


@mcp.tool()
def k8s_get(kind: str, namespace: str = DIAG_NAMESPACE, name: str | None = None) -> str:
    """Read-only: список или один объект в кластере.

    kind — один из: pods, deployments, services, ingresses, events.
    namespace жёстко ограничен на "managed" со стороны RBAC — другой
    namespace просто вернёт 403 от API-сервера, не от этой функции.
    """
    kind = kind.lower()
    if kind not in _K8S_KINDS:
        return _err(f"kind должен быть одним из {sorted(_K8S_KINDS)}, получено: {kind!r}")
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
        return client.ApiClient().sanitize_for_serialization(api)
    except ApiException as e:
        return _err(f"k8s API {e.status}: {e.reason}")


@mcp.tool()
def k8s_logs(pod: str, namespace: str = DIAG_NAMESPACE, container: str | None = None, tail_lines: int = 200) -> str:
    """Read-only: последние строки логов пода (по умолчанию 200)."""
    try:
        return core_v1.read_namespaced_pod_log(
            name=pod, namespace=namespace, container=container, tail_lines=min(tail_lines, 2000)
        )
    except ApiException as e:
        return _err(f"k8s API {e.status}: {e.reason}")


@mcp.tool()
def flux_get(kind: str, name: str) -> str:
    """Read-only: статус Flux-объекта (Kustomization/GitRepository/HelmRelease)
    в namespace flux-system — в первую очередь смотри .status.conditions,
    там причина, если реконсиляция не прошла."""
    if kind not in _FLUX_GVR:
        return _err(f"kind должен быть одним из {sorted(_FLUX_GVR)}, получено: {kind!r}")
    group, version, plural = _FLUX_GVR[kind]
    try:
        obj = custom.get_namespaced_custom_object(group, version, FLUX_NAMESPACE, plural, name)
        return obj.get("status", {})
    except ApiException as e:
        return _err(f"k8s API {e.status}: {e.reason}")


@mcp.tool()
def flux_reconcile(kind: str, name: str) -> str:
    """Форсирует немедленную реконсиляцию Kustomization/GitRepository/
    HelmRelease вместо ожидания штатного interval — эквивалент
    `flux reconcile <kind> <name>`. НЕ создаёт, не меняет и не удаляет
    ничего, кроме собственной аннотации на этом Flux-объекте; реально
    применяемые манифесты берутся из git."""
    if kind not in _FLUX_GVR:
        return _err(f"kind должен быть одним из {sorted(_FLUX_GVR)}, получено: {kind!r}")
    group, version, plural = _FLUX_GVR[kind]
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    patch = {"metadata": {"annotations": {"reconcile.fluxcd.io/requestedAt": now}}}
    try:
        custom.patch_namespaced_custom_object(
            group, version, FLUX_NAMESPACE, plural, name, patch,
        )
        return f"reconcile requested for {kind}/{name} at {now}"
    except ApiException as e:
        return _err(f"k8s API {e.status}: {e.reason}")


if __name__ == "__main__":
    mcp.run(transport="sse")
