"""helm-mcp — валидация ПЕРЕД коммитом для n8n ops-агента.

Ничего здесь не трогает кластер (нет ServiceAccount/RBAC, нет
kubeconfig) — только публичные Helm-репозитории (через `helm` CLI, без
persistent `helm repo add`), офлайн JSON-схемы для `kubeconform` и
локальный `kustomize build` во временной директории.

Инструменты:

- helm_show_values / helm_template — как раньше: реальные default values
  чарта и рендер с проверкой шаблона.
- validate_yaml — офлайн-схемная валидация (kubeconform + core-k8s +
  datreeio/CRDs-catalog для Flux CRD). Теперь ДОПОЛНИТЕЛЬНО возвращает
  content_sha256 при успехе — это и есть hash-gate: forgejo_put_file
  (n8n-нода) требует validated_sha256 и отклоняет запись, если хэш от
  переданного content_base64 не совпадает, т.е. без похода в Forgejo.
  Так проверка физически привязана к тому, что реально коммитится, а не
  просто "была вызвана когда-то в этом же ходе".
- check_values_keys — set-diff ключей values_yaml против реальной схемы
  чарта (helm_show_values), hard fail на ключах, которых нет НИГДЕ в
  файле чарта (даже закомментированных) — то есть по-настоящему
  выдуманных моделью. Ключи, которые существуют только как
  закомментированный пример в values.yaml чарта (типовая helm-конвенция
  документировать необязательные поля так) — это НЕ ошибка сама по себе
  (шаблон чарта вполне может их использовать), поэтому такие идут в
  warnings, не в hard fail; см. docstring тула.
- render_helm_repository / render_helm_release / render_app_registration
  / render_kustomize_build_file — детерминированные рендеры вместо
  свободного текста для мест, где раньше ловились опечатки в
  kind/apiVersion/dependsOn/prune и т.п. (apiVersion, dependsOn,
  targetNamespace, secretRef-имя и т.д. — литералы в коде, недостижимы
  для опечатки моделью; переменные — только то, что реально должно
  меняться: name/chart/version/values/resources).
- kustomize_build — реальный `kustomize build` над ЦЕЛЫМ набором файлов
  репозитория приложения (а не одним файлом, как validate_yaml) — ловит
  рассинхрон "kustomization.yaml ссылается на файл, которого нет/который
  неверно назван".
"""

import hashlib
import json
import logging
import os
import subprocess
import tempfile

import yaml
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("helm-mcp")

CRDS_CATALOG_SCHEMA = (
    "https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/"
    "{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
)

# Значения, под которыми чарты обычно держат полностью свободные,
# пользовательские map'ы (аннотации, лейблы, env и т.п.) — при
# set-diff'е values_yaml не спускаемся в их детей, иначе любая
# пользовательская аннотация будет ложно помечена как "неизвестное поле".
OPAQUE_VALUE_CONTAINERS = {
    "annotations", "labels", "nodeSelector", "tolerations", "env",
    "extraEnv", "extraEnvVars", "envFrom", "matchLabels", "selector",
    "data", "nodeSelectorTerms", "podAnnotations", "podLabels",
    "extraLabels", "extraAnnotations",
}

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


def _yaml_dump(obj) -> str:
    return yaml.dump(obj, sort_keys=False, default_flow_style=False, width=1000)


# --------------------------------------------------------------------------
# helm_show_values / helm_template (без изменений в поведении)
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# validate_yaml (+ content_sha256 hash-gate)
# --------------------------------------------------------------------------


@mcp.tool()
def validate_yaml(yaml_text: str) -> str:
    """Офлайн-валидация YAML (один манифест или несколько через '---')
    против реальных схем: core Kubernetes + Flux и другие популярные CRD
    (kustomize.toolkit.fluxcd.io, source.toolkit.fluxcd.io,
    helm.toolkit.fluxcd.io и т.д. через datreeio/CRDs-catalog). Вызывай
    ОБЯЗАТЕЛЬНО перед КАЖДЫМ forgejo_put_file на .yaml.

    При успехе (ok: true) возвращает content_sha256 — sha256 от РОВНО
    ТОГО текста, что был передан сюда. forgejo_put_file требует этот же
    хэш как validated_sha256 и сам пересчитывает его от content_base64 —
    если ты передашь в put_file текст, отличающийся от того, что здесь
    провалидирован (даже на один символ), put_file откажет ДО обращения
    к Forgejo. Поэтому: значение content_base64 в put_file должно быть
    base64 от БУКВАЛЬНО того же yaml_text, что ты только что провалидировал
    здесь — не перепечатывай его заново."""
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
        content_sha256 = hashlib.sha256(yaml_text.encode("utf-8")).hexdigest()
        return json.dumps({"ok": True, "summary": counts, "content_sha256": content_sha256})
    return json.dumps({"ok": False, "summary": counts, "resources": summary.get("resources", [])}, ensure_ascii=False)


# --------------------------------------------------------------------------
# check_values_keys
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


@mcp.tool()
def check_values_keys(repo_url: str, chart: str, values_yaml: str, version: str = "") -> str:
    """Проверяет ключи values_yaml против реальной схемы чарта (то же,
    что вернул бы helm_show_values) — hard fail (ok: false) на ключах,
    которых нет НИГДЕ в values.yaml чарта, даже закомментированных.
    Вызывай ДО helm_template, для того же чарта — helm_template может
    молча проигнорировать лишний ключ, эта проверка — нет.

    Ключи, которые есть в values.yaml чарта только как закомментированный
    пример (обычная конвенция Helm-чартов для необязательных полей) — НЕ
    hard fail, они в warnings: шаблон чарта вполне может их использовать,
    даже если их нет в дефолтных values. Известное ограничение: полностью
    свободные map'ы (annotations/labels/nodeSelector/env и т.п.) не
    проверяются на детей вообще — там любой ключ легитимен по смыслу."""
    cmd = ["helm", "show", "values", chart, "--repo", repo_url]
    if version:
        cmd += ["--version", version]
    res = _run(cmd)
    if res["returncode"] != 0:
        return json.dumps({"ok": False, "error": f"helm show values failed: {res['stderr']}"})
    raw_values_text = res["stdout"]
    try:
        schema = yaml.safe_load(raw_values_text) or {}
    except yaml.YAMLError as e:
        return json.dumps({"ok": False, "error": f"could not parse chart values.yaml: {e}"})
    try:
        provided = yaml.safe_load(values_yaml) or {}
    except yaml.YAMLError as e:
        return json.dumps({"ok": False, "error": f"could not parse values_yaml: {e}"})

    schema_paths = _walk_paths(schema)
    provided_paths = _walk_paths(provided)

    # bare key tokens mentioned ANYWHERE in the chart's values.yaml text,
    # commented or not — used to distinguish "undocumented-but-real" from
    # "never existed at all" for keys missing from the active schema.
    import re
    mentioned_tokens = set(re.findall(r"^\s*#?\s*([A-Za-z0-9_-]+):", raw_values_text, re.MULTILINE))

    hard_fail = []
    warnings = []
    for path in sorted(provided_paths - schema_paths):
        leaf = path.rsplit(".", 1)[-1]
        if leaf in mentioned_tokens:
            warnings.append(path)
        else:
            hard_fail.append(path)

    if hard_fail:
        return json.dumps({
            "ok": False,
            "unknown_keys": hard_fail,
            "undocumented_but_maybe_real_keys": warnings,
        }, ensure_ascii=False)
    return json.dumps({"ok": True, "undocumented_but_maybe_real_keys": warnings}, ensure_ascii=False)


# --------------------------------------------------------------------------
# render_* — детерминированные шаблоны вместо свободного текста
# --------------------------------------------------------------------------


@mcp.tool()
def render_helm_repository(name: str, url: str) -> str:
    """Рендерит Flux HelmRepository (apiVersion source.toolkit.fluxcd.io/v1
    — литерал, недостижим для опечатки). Единственные переменные — name и
    url источника чарта."""
    obj = {
        "apiVersion": "source.toolkit.fluxcd.io/v1",
        "kind": "HelmRepository",
        "metadata": {"name": name},
        "spec": {"interval": "30m", "url": url},
    }
    return _yaml_dump(obj)


@mcp.tool()
def render_helm_release(
    name: str,
    chart: str,
    chart_version: str,
    helm_repository_name: str,
    values_yaml: str,
) -> str:
    """Рендерит Flux HelmRelease (apiVersion helm.toolkit.fluxcd.io/v2,
    spec.chart.spec.sourceRef.kind — литералы в коде). values_yaml —
    ТЕКСТ (YAML), который ты уже прогнал через helm_template и
    check_values_keys для этого же chart/chart_version — он парсится и
    один в один вставляется под spec.values. Не пиши apiVersion/kind/
    sourceRef руками нигде — этот тул их даёт всегда одинаковыми."""
    try:
        values = yaml.safe_load(values_yaml) or {}
    except yaml.YAMLError as e:
        return f"ERROR: values_yaml is not valid YAML: {e}"
    obj = {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "kind": "HelmRelease",
        "metadata": {"name": name},
        "spec": {
            "interval": "30m",
            "chart": {
                "spec": {
                    "chart": chart,
                    "version": str(chart_version),
                    "sourceRef": {"kind": "HelmRepository", "name": helm_repository_name},
                }
            },
            "values": values,
        },
    }
    return _yaml_dump(obj)


@mcp.tool()
def render_app_registration(name: str) -> str:
    """Рендерит ПОЛНОЕ содержимое apps/<name>.yaml для control-репо
    ops/gitops-control (app-of-apps) — GitRepository(app-<name>) +
    Kustomization(app-<name>) одним документом, разделены '---'. Все поля
    (namespace flux-system, secretRef ops-control-repo-auth, dependsOn
    infrastructure-ops-control, targetNamespace managed, prune/wait и
    т.д.) — литералы в коде. Единственный вход — name; url собирается из
    него же (ops/<name>.git). Используй этот тул вместо ручного
    написания apps/<name>.yaml — это единственное официальное место,
    которое знает правильную форму этой пары."""
    git_repo = {
        "apiVersion": "source.toolkit.fluxcd.io/v1",
        "kind": "GitRepository",
        "metadata": {"name": f"app-{name}", "namespace": "flux-system"},
        "spec": {
            "interval": "5m",
            "url": f"http://forgejo-http.agents.svc.cluster.local:3000/ops/{name}.git",
            "ref": {"branch": "main"},
            "secretRef": {"name": "ops-control-repo-auth"},
        },
    }
    kustomization = {
        "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
        "kind": "Kustomization",
        "metadata": {"name": f"app-{name}", "namespace": "flux-system"},
        "spec": {
            "interval": "10m",
            "dependsOn": [{"name": "infrastructure-ops-control"}],
            "sourceRef": {"kind": "GitRepository", "name": f"app-{name}"},
            "path": "./",
            "targetNamespace": "managed",
            "prune": True,
            "wait": True,
            "postBuild": {"substituteFrom": [{"kind": "ConfigMap", "name": "cluster-vars"}]},
        },
    }
    return _yaml_dump(git_repo) + "---\n" + _yaml_dump(kustomization)


@mcp.tool()
def render_kustomize_build_file(resources: list[str]) -> str:
    """Рендерит kustomization.yaml (apiVersion kustomize.config.k8s.io/
    v1beta1 — обычный build-файл kustomize, НЕ Flux CR) для репозитория
    ОДНОГО приложения — просто список файлов. resources — точные имена
    файлов, которые ты УЖЕ записал в этот репозиторий через
    forgejo_put_file (например ["helmrepository.yaml", "helmrelease.yaml"]).
    Обязательно прогони результат через kustomize_build вместе с этими же
    файлами перед тем, как считать репозиторий готовым — этот тул не
    проверяет, что перечисленные файлы реально существуют."""
    obj = {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "resources": list(resources),
    }
    return _yaml_dump(obj)


# --------------------------------------------------------------------------
# kustomize_build — кросс-файловая валидация целого репозитория
# --------------------------------------------------------------------------


@mcp.tool()
def kustomize_build(files: dict[str, str]) -> str:
    """Гоняет настоящий `kustomize build` над ЦЕЛЫМ набором файлов
    репозитория приложения — не один YAML, как validate_yaml, а все файлы
    сразу, с реальными именами. Ловит рассинхрон вида "kustomization.yaml
    ссылается на helmrepository.xaml, а реально записан
    helmrepository.yaml" — ошибку по недостающему/неверно названному
    файлу.

    files — словарь {имя_файла: содержимое}, ДОЛЖЕН включать
    kustomization.yaml и все файлы, которые он перечисляет в resources.
    Вызывай как последний шаг перед тем, как переходить к регистрации
    приложения в control-репо (render_app_registration) — если
    kustomize_build не прошёл (ok: false), регистрировать в control-репо
    рано."""
    for name in files:
        if ".." in name or name.startswith("/") or name.startswith("~"):
            return json.dumps({"ok": False, "error": f"invalid filename: {name}"})
    with tempfile.TemporaryDirectory() as d:
        for name, content in files.items():
            path = os.path.join(d, name)
            os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
            with open(path, "w") as f:
                f.write(content)
        res = _run(["kustomize", "build", d], timeout=30)
    if res["returncode"] != 0:
        return json.dumps({"ok": False, "error": (res["stderr"] or res["stdout"]).strip()}, ensure_ascii=False)
    return json.dumps({"ok": True, "rendered": res["stdout"]}, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="sse")
