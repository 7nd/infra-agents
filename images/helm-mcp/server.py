"""helm-mcp — валидация ПЕРЕД коммитом для n8n ops-агента.

Три read-only тула, ни один не трогает кластер вообще (нет
ServiceAccount/RBAC, нет kubeconfig) — только публичные Helm-репозитории
(через `helm` CLI, без persistent `helm repo add`) и офлайн JSON-схемы для
`kubeconform`:

- helm_show_values — реальные default values чарта вместо угадывания
  моделью.
- helm_template — рендерит чарт с данными values, ловит ошибки на уровне
  самого чарта (несуществующее поле, неверный тип) ДО того, как что-то
  уйдёт в git.
- validate_yaml — офлайн-валидация ЛЮБОГО YAML (включая Flux CRD —
  HelmRelease/GitRepository/Kustomization) против реальных OpenAPI-схем:
  `-schema-location default` для core k8s, плюс datreeio/CRDs-catalog для
  Flux и прочих популярных CRD.
"""

import json
import logging
import subprocess
import tempfile

from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("helm-mcp")

CRDS_CATALOG_SCHEMA = (
    "https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/"
    "{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
)

mcp = FastMCP("helm-diag", host="0.0.0.0", port=8080)


def _run(cmd: list[str], input_text: str | None = None, timeout: int = 60) -> dict:
    try:
        proc = subprocess.run(
            cmd,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    except subprocess.TimeoutExpired:
        return {"returncode": None, "stdout": "", "stderr": f"timed out after {timeout}s"}


@mcp.tool()
def helm_show_values(repo_url: str, chart: str, version: str = "") -> str:
    """Возвращает РЕАЛЬНЫЕ default values.yaml чарта — используй перед тем,
    как писать values для HelmRelease, вместо того чтобы угадывать поля.

    repo_url — url Helm-репозитория (например
    https://sonatype.github.io/helm3-charts), chart — имя чарта внутри
    него (например nexus-repository-manager), version — опционально
    (semver constraint или конкретная версия, по умолчанию последняя)."""
    cmd = ["helm", "show", "values", chart, "--repo", repo_url]
    if version:
        cmd += ["--version", version]
    res = _run(cmd)
    if res["returncode"] != 0:
        return f"ERROR: helm show values failed:\n{res['stderr']}"
    return res["stdout"]


@mcp.tool()
def helm_template(
    repo_url: str,
    chart: str,
    values_yaml: str,
    version: str = "",
    release_name: str = "release",
    namespace: str = "managed",
) -> str:
    """Рендерит чарт с переданными values — ловит ошибки шаблонизации
    (несуществующее поле, неверный тип и т.п.) ДО коммита в git. Вызывай
    для КАЖДОГО HelmRelease перед тем, как класть его в репозиторий
    приложения. Возвращает либо готовые манифесты, либо текст ошибки
    helm — по нему обычно понятно, какое поле values неверно."""
    cmd = [
        "helm", "template", release_name, chart,
        "--repo", repo_url, "-n", namespace, "-f", "-",
    ]
    if version:
        cmd += ["--version", version]
    res = _run(cmd, input_text=values_yaml)
    if res["returncode"] != 0:
        return f"ERROR: helm template failed:\n{res['stderr']}"
    return res["stdout"]


@mcp.tool()
def validate_yaml(yaml_text: str) -> str:
    """Офлайн-валидация YAML (один манифест или несколько через '---')
    против реальных схем: core Kubernetes + Flux и другие популярные CRD
    (kustomize.toolkit.fluxcd.io, source.toolkit.fluxcd.io,
    helm.toolkit.fluxcd.io и т.д. через datreeio/CRDs-catalog). Вызывай
    ОБЯЗАТЕЛЬНО перед КАЖДЫМ forgejo_put_file на .yaml — ловит опечатки в
    kind/полях и неверные apiVersion до того, как они попадут в git и
    сломают Flux-реконсиляцию."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=True) as f:
        f.write(yaml_text)
        f.flush()
        cmd = [
            "kubeconform",
            "-strict",
            "-summary",
            "-output", "json",
            "-schema-location", "default",
            "-schema-location", CRDS_CATALOG_SCHEMA,
            f.name,
        ]
        res = _run(cmd, timeout=30)
    stdout = res["stdout"].strip()
    if not stdout:
        return f"ERROR: kubeconform produced no output:\n{res['stderr']}"
    try:
        summary = json.loads(stdout)
    except json.JSONDecodeError:
        return f"ERROR: could not parse kubeconform output:\n{stdout}\n{res['stderr']}"
    counts = summary.get("summary", {})
    if counts.get("invalid", 0) == 0 and counts.get("errors", 0) == 0:
        return json.dumps({"ok": True, "summary": counts})
    return json.dumps({"ok": False, "summary": counts, "resources": summary.get("resources", [])}, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="sse")
