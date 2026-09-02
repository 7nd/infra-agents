# Кастомный образ, аналогично images/glitchtip-mcp — готового HTTP-режима
# read-only MCP-тула под Flux/k8s в природе нет (общие MCP для k8s либо
# generic CRUD, либо привязаны к отдельному flux-operator), поэтому
# собственный минимальный сервер поверх официального Python MCP SDK
# (mcp.server.fastmcp.FastMCP) + kubernetes client. Инструменты и их
# allow-list на kind — см. server.py; реальная граница безопасности всё
# равно RBAC ServiceAccount'а, под которым запущен под (см. rbac.yaml/
# flux-rbac.yaml в unitum-demo-k8s-infra), код — defense in depth поверх
# него.
#
# Собирается через in-cluster BuildKit (не локальный `docker build`) —
# локальная машина arm64 (Mac), ноды кластера amd64, см. README про сборку
# кастомных образов.
FROM python:3.12-slim

RUN pip install --no-cache-dir "mcp[cli]" kubernetes

COPY server.py /app/server.py
WORKDIR /app

EXPOSE 8080

ENTRYPOINT ["python", "server.py"]
