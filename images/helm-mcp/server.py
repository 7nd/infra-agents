"""helm-mcp — детерминированный рендеринг + валидация ПЕРЕД коммитом для
n8n ops-агента. Ничего здесь не трогает кластер (нет ServiceAccount/RBAC,
нет kubeconfig, нет доступа к Forgejo) — только публичные Helm-репозитории
(через `helm` CLI, без persistent `helm repo add`), офлайн JSON-схемы для
`kubeconform` и локальный `kustomize build` во временной директории.

Инструменты:

- helm_show_values / helm_template — как раньше.
- validate_yaml — офлайн-схемная валидация + content_sha256 на успехе
  (половина hash-gate; вторая половина — в forgejo_write_file, n8n).
- render_manifest(kind, name, params) — ОДИН тул, детерминированный
  рендер для HelmRelease / HelmRepository / GitRepository / Kustomization
  (Flux CR) / Ingress / KustomizeBuildFile / RawWorkload. Все константы
  (apiVersion, cloudflare-proxied аннотация, secretName wildcard,
  отсутствие cert-manager.io/cluster-issuer, префикс <name>-, nodeSelector
  node-group=workload, отсутствие metadata.namespace) — литералы в коде,
  недостижимы для опечатки моделью. Для kind=HelmRelease — обязательный
  values-diff против реальной схемы чарта ДО сборки (см. _check_values_keys).
  Возвращает content_sha256 сразу на успехе.
- kustomize_build — реальный `kustomize build` над ЦЕЛЫМ набором файлов
  репозитория (не один YAML, как validate_yaml).

Ошибки везде — {"ok": false, "error_code": "<enum>", ...детали...},
error_code классифицируется кодом тула (по типу исключения/структуре
ответа), не LLM — иначе статистика repair-попыток (Postgres
repair_attempts/repair_policy) не группируется, почти каждая ошибка
текстово уникальна.
"""

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
from typing import Optional

import psycopg2
import yaml
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("helm-mcp")

CRDS_CATALOG_SCHEMA = (
    "https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/"
    "{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
)

# Значения, под которыми чарты/манифесты обычно держат полностью
# свободные, пользовательские map'ы — при set-diff'е values_yaml не
# спускаемся в их детей.
OPAQUE_VALUE_CONTAINERS = {
    "annotations", "labels", "nodeSelector", "tolerations", "env",
    "extraEnv", "extraEnvVars", "envFrom", "matchLabels", "selector",
    "data", "nodeSelectorTerms", "podAnnotations", "podLabels",
    "extraLabels", "extraAnnotations",
}

# Только эти namespace'ы/литералы допустимы — берутся из bootstrap этого
# кластера (unitum-demo-k8s-infra), не параметр модели.
MANAGED_NAMESPACE = "managed"
NODE_GROUP_LABEL = {"node-group": "workload"}
WILDCARD_TLS_SECRET = "wildcard-managed-tls"
CLOUDFLARE_UNPROXIED_ANNOTATION = {"external-dns.alpha.kubernetes.io/cloudflare-proxied": "false"}
BASE_DOMAIN = "managed.hightps.online"
FORGEJO_INTERNAL_BASE = "http://forgejo-http.agents.svc.cluster.local:3000/ops"
OPS_CONTROL_REPO_AUTH_SECRET = "ops-control-repo-auth"

mcp = FastMCP("helm-diag", host="0.0.0.0", port=8080)


def _run(cmd: list[str], input_text: str | None = None, timeout: int = 60) -> dict:
    try:
        proc = subprocess.run(
            cmd, input=input_text, capture_output=True, text=True, timeout=timeout,
        )
        return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    except subprocess.TimeoutExpired:
        return {"returncode": None, "stdout": "", "stderr": f"timed out after {timeout}s"}


def _yaml_dump(obj) -> str:
    return yaml.dump(obj, sort_keys=False, default_flow_style=False, width=1000)


def _err(error_code: str, message: str, **extra) -> str:
    return json.dumps({"ok": False, "error_code": error_code, "message": message, **extra}, ensure_ascii=False)


def _ok_content(yaml_text: str, **extra) -> str:
    return json.dumps({
        "ok": True,
        "content_sha256": hashlib.sha256(yaml_text.encode("utf-8")).hexdigest(),
        "yaml": yaml_text,
        **extra,
    }, ensure_ascii=False)


# --------------------------------------------------------------------------
# helm_show_values / helm_template
# --------------------------------------------------------------------------


@mcp.tool()
def helm_show_values(repo_url: str, chart: str, version: str = "") -> str:
    """Возвращает РЕАЛЬНЫЕ default values.yaml чарта — используй перед тем,
    как заполнять params.values_yaml для render_manifest(kind=HelmRelease),
    вместо того чтобы угадывать поля."""
    cmd = ["helm", "show", "values", chart, "--repo", repo_url]
    if version:
        cmd += ["--version", version]
    res = _run(cmd)
    if res["returncode"] != 0:
        return _err("helm.show_values_failed", res["stderr"])
    return res["stdout"]


@mcp.tool()
def helm_template(
    repo_url: str,
    chart: str,
    values_yaml: str,
    version: str = "",
    release_name: str = "release",
    namespace: str = MANAGED_NAMESPACE,
) -> str:
    """Рендерит чарт с переданными values — ловит ошибки шаблонизации ДО
    коммита в git. render_manifest(kind=HelmRelease) уже прогоняет это
    внутри себя автоматически — вызывай отдельно только для диагностики
    (например в Supervisor-ветке на нестандартном чарте)."""
    cmd = ["helm", "template", release_name, chart, "--repo", repo_url, "-n", namespace, "-f", "-"]
    if version:
        cmd += ["--version", version]
    res = _run(cmd, input_text=values_yaml)
    if res["returncode"] != 0:
        return _err("helm.template_error", res["stderr"])
    return res["stdout"]


# --------------------------------------------------------------------------
# validate_yaml
# --------------------------------------------------------------------------


@mcp.tool()
def validate_yaml(yaml_text: str) -> str:
    """Офлайн-валидация YAML против реальных схем: core Kubernetes + Flux
    и другие CRD (datreeio/CRDs-catalog). Нужен только для содержимого,
    которое НЕ вышло из render_manifest (тот уже гарантированно валиден
    структурно) — например произвольного YAML в Supervisor-ветке.
    При успехе возвращает content_sha256 — hash-gate в forgejo_write_file
    требует его как validated_sha256."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=True) as f:
        f.write(yaml_text)
        f.flush()
        cmd = [
            "kubeconform", "-strict", "-summary", "-output", "json",
            "-schema-location", "default", "-schema-location", CRDS_CATALOG_SCHEMA,
            f.name,
        ]
        res = _run(cmd, timeout=30)
    stdout = res["stdout"].strip()
    if not stdout:
        return _err("yaml.validator_no_output", res["stderr"])
    try:
        summary = json.loads(stdout)
    except json.JSONDecodeError:
        return _err("yaml.validator_output_unparseable", stdout + res["stderr"])
    counts = summary.get("summary", {})
    if counts.get("invalid", 0) == 0 and counts.get("errors", 0) == 0:
        return json.dumps({
            "ok": True, "summary": counts,
            "content_sha256": hashlib.sha256(yaml_text.encode("utf-8")).hexdigest(),
        })
    return json.dumps({
        "ok": False, "error_code": "yaml.schema_invalid",
        "summary": counts, "resources": summary.get("resources", []),
    }, ensure_ascii=False)


# --------------------------------------------------------------------------
# values-key diff (used internally by render_manifest for HelmRelease)
# --------------------------------------------------------------------------


def _walk_paths(node, prefix: str = "") -> set[str]:
    paths: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            paths.add(p)
            if k in OPAQUE_VALUE_CONTAINERS:
                continue
            paths |= _walk_paths(v, p)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, dict):
                paths |= _walk_paths(item, prefix)
    return paths


def _check_values_keys(repo_url: str, chart: str, values: dict, version: str = "") -> Optional[dict]:
    """Возвращает None если ок, иначе dict с error_code/unknown_keys."""
    cmd = ["helm", "show", "values", chart, "--repo", repo_url]
    if version:
        cmd += ["--version", version]
    res = _run(cmd)
    if res["returncode"] != 0:
        return {"error_code": "helm.show_values_failed", "message": res["stderr"]}
    raw_values_text = res["stdout"]
    try:
        schema = yaml.safe_load(raw_values_text) or {}
    except yaml.YAMLError as e:
        return {"error_code": "helm.values_schema_unparseable", "message": str(e)}

    schema_paths = _walk_paths(schema)
    provided_paths = _walk_paths(values)
    mentioned_tokens = set(re.findall(r"^\s*#?\s*([A-Za-z0-9_-]+):", raw_values_text, re.MULTILINE))

    hard_fail, warnings = [], []
    for path in sorted(provided_paths - schema_paths):
        leaf = path.rsplit(".", 1)[-1]
        (warnings if leaf in mentioned_tokens else hard_fail).append(path)

    if hard_fail:
        return {"error_code": "values.unknown_key", "unknown_keys": hard_fail, "warnings": warnings}
    return None


@mcp.tool()
def check_values_keys(repo_url: str, chart: str, values_yaml: str, version: str = "") -> str:
    """Отдельный вызов values-diff вне render_manifest — для диагностики
    (render_manifest(kind=HelmRelease) уже гоняет эту же проверку сам)."""
    try:
        values = yaml.safe_load(values_yaml) or {}
    except yaml.YAMLError as e:
        return _err("values.yaml_unparseable", str(e))
    fail = _check_values_keys(repo_url, chart, values, version)
    if fail:
        return json.dumps({"ok": False, **fail}, ensure_ascii=False)
    return json.dumps({"ok": True})


# --------------------------------------------------------------------------
# repo_knowledge — exact-key facts about specific chart repos (access
# restrictions, known-good alternates), NOT a fuzzy/semantic store — a
# Postgres table fits better here than Qdrant's similarity search would,
# since this is "do we know something about THIS exact URL", not "find
# something similar". Manually curated for now (no auto-promotion from
# escalations yet — deliberately deferred).
# --------------------------------------------------------------------------


@mcp.tool()
def lookup_repo_knowledge(repo_url: str) -> str:
    """Проверяет, есть ли известные факты про конкретный Helm-репозиторий
    ДО того, как пытаться render_manifest/helm_show_values против него —
    например репозиторий может требовать платной подписки для доступа
    (как classic-индекс Bitnami, который отдаёт 403 без подписки) и иметь
    известную рабочую альтернативу. Вызывай ЭТИМ URL'ом (тем, что дал
    пользователь/Extract), не пытайся угадать канонический вид.
    Возвращает {"ok": true, "found": bool, "note": str|null,
    "alternative_repo_url": str|null} — found:false просто значит "ничего
    не известно", не ошибка. Если alternative_repo_url задан — используй
    ЕГО вместо исходного repo_url в последующих вызовах."""
    database_uri = os.environ.get("DATABASE_URI")
    if not database_uri:
        return json.dumps({"ok": False, "error_code": "repo_knowledge.no_db_configured", "message": "DATABASE_URI not set"})
    try:
        conn = psycopg2.connect(database_uri, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT note, alternative_repo_url FROM repo_knowledge WHERE repo_url = %s", (repo_url,))
                row = cur.fetchone()
        finally:
            conn.close()
    except psycopg2.Error as e:
        return json.dumps({"ok": False, "error_code": "repo_knowledge.db_error", "message": str(e)})
    if not row:
        return json.dumps({"ok": True, "found": False, "note": None, "alternative_repo_url": None})
    return json.dumps({"ok": True, "found": True, "note": row[0], "alternative_repo_url": row[1]})


# --------------------------------------------------------------------------
# render_manifest — единая точка детерминированного рендера
# --------------------------------------------------------------------------

VALID_KINDS = {
    "HelmRelease", "HelmRepository", "GitRepository", "Kustomization",
    "Ingress", "KustomizeBuildFile", "RawWorkload",
}


class RenderParams(BaseModel):
    # HelmRelease
    chart: Optional[str] = None
    chart_version: Optional[str] = None
    helm_repository_name: Optional[str] = None
    helm_repository_url: Optional[str] = Field(
        default=None,
        description="нужен ТОЛЬКО для values-diff (helm show values) — сам HelmRepository-объект собирай отдельным вызовом render_manifest(kind=HelmRepository)",
    )
    values_yaml: Optional[str] = None
    # HelmRepository
    url: Optional[str] = None
    # GitRepository
    secret_ref: Optional[str] = None
    # Kustomization (Flux CR)
    source_ref: Optional[str] = None
    depends_on: Optional[str] = None
    target_namespace: Optional[str] = None
    # Ingress
    host: Optional[str] = None
    service_name: Optional[str] = None
    service_port: Optional[int] = None
    extra_annotations: Optional[dict] = None
    # KustomizeBuildFile
    resources: Optional[list[str]] = None
    # RawWorkload
    image: Optional[str] = None
    port: Optional[int] = None
    replicas: Optional[int] = 1
    env: Optional[list[dict]] = None
    persistence_gb: Optional[int] = None
    mount_path: Optional[str] = None


def _render_helm_repository(name: str, params: RenderParams) -> tuple[Optional[str], Optional[dict]]:
    if not params.url:
        return None, {"error_code": "render.missing_param", "message": "params.url is required for kind=HelmRepository"}
    obj = {
        "apiVersion": "source.toolkit.fluxcd.io/v1",
        "kind": "HelmRepository",
        "metadata": {"name": name},
        "spec": {"interval": "30m", "url": params.url},
    }
    return _yaml_dump(obj), None


def _render_helm_release(name: str, params: RenderParams) -> tuple[Optional[str], Optional[dict], Optional[dict]]:
    # chart_version deliberately NOT required — omitting it is a normal,
    # common request ("deploy the latest") and Flux's HelmRelease CRD
    # already treats a missing spec.chart.spec.version as "any/latest"
    # (same as omitting --version to `helm install`). Previously required
    # here, which meant every unversioned chart request failed with
    # render.missing_param and escalated to Supervisor unnecessarily —
    # found live via testing (bitnami/redis, no version given).
    missing = [f for f in ("chart", "helm_repository_name", "values_yaml") if not getattr(params, f)]
    if missing:
        return None, {"error_code": "render.missing_param", "message": f"missing required params for kind=HelmRelease: {missing}"}, None
    try:
        values = yaml.safe_load(params.values_yaml) or {}
    except yaml.YAMLError as e:
        return None, {"error_code": "values.yaml_unparseable", "message": str(e)}, None
    detected_services = []
    if params.helm_repository_url:
        fail = _check_values_keys(params.helm_repository_url, params.chart, values, params.chart_version or "")
        if fail:
            return None, fail, None
        tmpl_res = _run(
            ["helm", "template", "release", params.chart, "--repo", params.helm_repository_url,
             "-n", MANAGED_NAMESPACE, "-f", "-"] + (["--version", params.chart_version] if params.chart_version else []),
            input_text=params.values_yaml,
        )
        if tmpl_res["returncode"] != 0:
            return None, {"error_code": "helm.template_error", "message": tmpl_res["stderr"]}, None
        # render_manifest is the only place that already runs `helm template`
        # for values-diff purposes — reuse that output to deterministically
        # discover the chart's own Service objects (name + ports), instead
        # of the caller having to guess a service name for kind=Ingress.
        # Extract's schema (Appendix A) has no service_name/service_port
        # slot for kind=chart at all, so this is the only source for it.
        try:
            for doc in yaml.safe_load_all(tmpl_res["stdout"]):
                if isinstance(doc, dict) and doc.get("kind") == "Service":
                    svc_name = doc.get("metadata", {}).get("name")
                    ports = [p.get("port") for p in doc.get("spec", {}).get("ports", []) if p.get("port")]
                    if svc_name and ports:
                        detected_services.append({"name": svc_name, "ports": ports})
        except yaml.YAMLError:
            pass  # best-effort — a parse failure here doesn't invalidate the HelmRelease itself
    chart_spec = {
        "chart": params.chart,
        "sourceRef": {"kind": "HelmRepository", "name": params.helm_repository_name},
    }
    if params.chart_version:
        chart_spec["version"] = str(params.chart_version)
    obj = {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "kind": "HelmRelease",
        "metadata": {"name": name},
        "spec": {
            "interval": "30m",
            "chart": {"spec": chart_spec},
            "values": values,
        },
    }
    return _yaml_dump(obj), None, ({"detected_services": detected_services} if detected_services else None)


def _render_git_repository(name: str, params: RenderParams) -> tuple[Optional[str], Optional[dict]]:
    if not params.url:
        return None, {"error_code": "render.missing_param", "message": "params.url is required for kind=GitRepository"}
    obj = {
        "apiVersion": "source.toolkit.fluxcd.io/v1",
        "kind": "GitRepository",
        "metadata": {"name": name, "namespace": "flux-system"},
        "spec": {
            "interval": "5m",
            "url": params.url,
            "ref": {"branch": "main"},
            "secretRef": {"name": params.secret_ref or OPS_CONTROL_REPO_AUTH_SECRET},
        },
    }
    return _yaml_dump(obj), None


def _render_kustomization_cr(name: str, params: RenderParams) -> tuple[Optional[str], Optional[dict]]:
    if not params.source_ref:
        return None, {"error_code": "render.missing_param", "message": "params.source_ref is required for kind=Kustomization"}
    obj = {
        "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
        "kind": "Kustomization",
        "metadata": {"name": name, "namespace": "flux-system"},
        "spec": {
            "interval": "10m",
            "dependsOn": [{"name": params.depends_on or "infrastructure-ops-control"}],
            "sourceRef": {"kind": "GitRepository", "name": params.source_ref},
            "path": "./",
            "targetNamespace": params.target_namespace or MANAGED_NAMESPACE,
            "prune": True,
            "wait": True,
            "postBuild": {"substituteFrom": [{"kind": "ConfigMap", "name": "cluster-vars"}]},
        },
    }
    return _yaml_dump(obj), None


def _render_ingress(name: str, params: RenderParams) -> tuple[Optional[str], Optional[dict]]:
    missing = [f for f in ("host", "service_name", "service_port") if not getattr(params, f)]
    if missing:
        return None, {"error_code": "render.missing_param", "message": f"missing required params for kind=Ingress: {missing}"}
    annotations = dict(CLOUDFLARE_UNPROXIED_ANNOTATION)
    if params.extra_annotations:
        annotations.update(params.extra_annotations)
    obj = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {"name": f"{name}-ingress", "annotations": annotations},
        "spec": {
            "ingressClassName": "nginx",
            "rules": [{
                "host": params.host,
                "http": {"paths": [{
                    "path": "/", "pathType": "Prefix",
                    "backend": {"service": {"name": params.service_name, "port": {"number": params.service_port}}},
                }]},
            }],
            "tls": [{"hosts": [params.host], "secretName": WILDCARD_TLS_SECRET}],
        },
    }
    return _yaml_dump(obj), None


def _render_kustomize_build_file(name: str, params: RenderParams) -> tuple[Optional[str], Optional[dict]]:
    if not params.resources:
        return None, {"error_code": "render.missing_param", "message": "params.resources (list of filenames) is required for kind=KustomizeBuildFile"}
    obj = {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "resources": list(params.resources),
    }
    return _yaml_dump(obj), None


def _render_raw_workload(name: str, params: RenderParams) -> tuple[Optional[str], Optional[dict]]:
    missing = [f for f in ("image", "port") if not getattr(params, f)]
    if missing:
        return None, {"error_code": "render.missing_param", "message": f"missing required params for kind=RawWorkload: {missing}"}
    container = {
        "name": name,
        "image": params.image,
        "ports": [{"containerPort": params.port}],
    }
    if params.env:
        container["env"] = params.env
    volumes = []
    docs = []
    if params.persistence_gb:
        mount_path = params.mount_path or "/data"
        container["volumeMounts"] = [{"name": "data", "mountPath": mount_path}]
        volumes.append({"name": "data", "persistentVolumeClaim": {"claimName": f"{name}-data"}})
        docs.append({
            "apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"name": f"{name}-data"},
            "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": f"{params.persistence_gb}Gi"}}},
        })
    deployment = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": f"{name}-deployment"},
        "spec": {
            "replicas": params.replicas or 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {
                    "nodeSelector": dict(NODE_GROUP_LABEL),
                    "containers": [container],
                    **({"volumes": volumes} if volumes else {}),
                },
            },
        },
    }
    service = {
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": f"{name}-svc"},
        "spec": {"selector": {"app": name}, "ports": [{"port": 80, "targetPort": params.port}]},
    }
    docs = [deployment] + docs + [service]
    return "---\n".join(_yaml_dump(d) for d in docs), None


_RENDERERS = {
    "HelmRepository": _render_helm_repository,
    "HelmRelease": _render_helm_release,
    "GitRepository": _render_git_repository,
    "Kustomization": _render_kustomization_cr,
    "Ingress": _render_ingress,
    "KustomizeBuildFile": _render_kustomize_build_file,
    "RawWorkload": _render_raw_workload,
}


@mcp.tool()
def render_manifest(kind: str, name: str, params: RenderParams) -> str:
    """Единственный способ получить YAML для типовых частей деплоя — НЕ
    пиши эти файлы текстом руками, apiVersion/kind/константы (namespace-
    аннотации, wildcard TLS secret, node-group label, отсутствие
    cert-manager.io/cluster-issuer, отсутствие metadata.namespace) зашиты
    в код и недостижимы для опечатки.

    kind — один из: HelmRelease, HelmRepository, GitRepository,
    Kustomization (Flux CR — НЕ обычный build-файл kustomize),
    Ingress, KustomizeBuildFile (обычный build-файл kustomize.config.k8s.io
    для репозитория приложения), RawWorkload (Deployment+Service+опц.PVC
    для приложения без Helm-чарта — НЕ включает Ingress, вызови отдельно
    render_manifest(kind=Ingress) для него).

    name — короткое имя приложения (совпадает с именем репозитория
    ops/<name> и доменом <name>.managed.hightps.online).

    params — только реальные переменные для этого kind:
    - HelmRelease: chart, chart_version, helm_repository_name, values_yaml
      (+ helm_repository_url — опционально, но КРАЙНЕ рекомендуется: если
      задан, tool сам прогоняет values-diff против реальной схемы чарта и
      helm_template ДО рендера — без него проверка пропускается).
    - HelmRepository / GitRepository: url (+ secret_ref опционально для
      GitRepository, по умолчанию ops-control-repo-auth).
    - Kustomization (Flux CR): source_ref (+ depends_on, target_namespace
      — есть разумные дефолты).
    - Ingress: host, service_name, service_port (+ extra_annotations).
    - KustomizeBuildFile: resources (список имён файлов).
    - RawWorkload: image, port (+ replicas, env, persistence_gb, mount_path).

    При успехе — {"ok": true, "content_sha256": "...", "yaml": "..."}.
    content_sha256 передавай как validated_sha256 в forgejo_write_file для
    ЭТОГО ЖЕ yaml — тул откажет, если запишешь что-то другое.
    Для kind=HelmRelease с заданным helm_repository_url — дополнительно
    "detected_services": [{"name", "ports": [...]}], найденные в
    отрендеренных чартом манифестах (kind=Service) — используй
    detected_services[0] как service_name/service_port для последующего
    render_manifest(kind=Ingress), не угадывай имя сервиса чарта.
    При ошибке — {"ok": false, "error_code": "...", ...} с закрытым
    error_code (render.missing_param / values.unknown_key /
    helm.template_error / helm.show_values_failed / values.yaml_unparseable),
    не свободным текстом — так группируются repair-попытки."""
    if kind not in VALID_KINDS:
        return _err("render.unknown_kind", f"kind must be one of {sorted(VALID_KINDS)}, got {kind!r}")
    result = _RENDERERS[kind](name, params)
    yaml_text, err, extra = result if len(result) == 3 else (*result, None)
    if err:
        return json.dumps({"ok": False, **err}, ensure_ascii=False)
    return _ok_content(yaml_text, **(extra or {}))


# --------------------------------------------------------------------------
# kustomize_build
# --------------------------------------------------------------------------


@mcp.tool()
def kustomize_build(files: dict[str, str]) -> str:
    """Гоняет настоящий `kustomize build` над ЦЕЛЫМ набором файлов
    репозитория приложения — ловит рассинхрон вида "kustomization.yaml
    ссылается на файл, которого нет / который неверно назван" по точному
    имени файла. files — {имя_файла: содержимое}, ДОЛЖЕН включать
    kustomization.yaml. Обязательный гейт перед register_app_in_control_repo
    (n8n-нода) — не переходи к регистрации, если ok:false."""
    for name in files:
        if ".." in name or name.startswith("/") or name.startswith("~"):
            return _err("kustomize.invalid_filename", f"invalid filename: {name}")
    with tempfile.TemporaryDirectory() as d:
        for name, content in files.items():
            path = os.path.join(d, name)
            if os.path.dirname(path):
                os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
        res = _run(["kustomize", "build", d], timeout=30)
    if res["returncode"] != 0:
        stderr = (res["stderr"] or res["stdout"]).strip()
        error_code = "kustomize.missing_file" if "no such file or directory" in stderr else "kustomize.build_failed"
        return _err(error_code, stderr)
    return json.dumps({"ok": True, "rendered": res["stdout"]}, ensure_ascii=False)


# --------------------------------------------------------------------------
# plain HTTP passthrough — the n8n deterministic graph (point 10) calls
# tools directly from Code nodes, not through an AI Agent's ai_tool
# connection, so it has no MCP client/session to speak the SSE-framed
# protocol with. This exposes the exact same registered tools (same
# validation, same functions) over plain POST JSON — no new logic, just a
# second transport for first-party, non-agentic callers.
# --------------------------------------------------------------------------


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
