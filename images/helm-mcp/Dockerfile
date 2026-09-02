# Валидация ПЕРЕД коммитом для ops-агента (см. server.py) — helm CLI +
# kubeconform, ни то ни другое не трогает кластер (helm работает против
# публичных chart-репозиториев через --repo, kubeconform — против
# офлайн-схем), поэтому ServiceAccount/RBAC этому поду не нужен вообще.
#
# Собирается через in-cluster BuildKit (не локальный docker build) — тот
# же приём, что images/k8s-diag-mcp, локальная машина arm64, ноды amd64.
FROM python:3.12-slim

ARG HELM_VERSION=v4.2.4
ARG KUBECONFORM_VERSION=v0.8.0
ARG KUSTOMIZE_VERSION=v5.8.1

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" | tar -xz -C /tmp \
    && mv /tmp/linux-amd64/helm /usr/local/bin/helm \
    && chmod +x /usr/local/bin/helm \
    && rm -rf /tmp/linux-amd64 \
    && curl -fsSL "https://github.com/yannh/kubeconform/releases/download/${KUBECONFORM_VERSION}/kubeconform-linux-amd64.tar.gz" | tar -xz -C /usr/local/bin kubeconform \
    && chmod +x /usr/local/bin/kubeconform \
    && curl -fsSL "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2F${KUSTOMIZE_VERSION}/kustomize_${KUSTOMIZE_VERSION}_linux_amd64.tar.gz" | tar -xz -C /usr/local/bin kustomize \
    && chmod +x /usr/local/bin/kustomize

# mcp>=2 переименовал FastMCP -> MCPServer с другим API — пинуем v1 ветку
# (тот же пин, что понадобился для k8s-diag-mcp). pyyaml — для
# render_*/check_values_keys (детерминированная сборка YAML вместо
# свободного текста, set-diff ключей values).
RUN pip install --no-cache-dir "mcp[cli]<2" pyyaml

COPY server.py /app/server.py
WORKDIR /app

EXPOSE 8080

ENTRYPOINT ["python", "server.py"]
