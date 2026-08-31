# Обёртка над stdio-only community MCP-сервером glitchtip-mcp (npm, автор
# coffebar) через supergateway (npm, "run MCP stdio servers over SSE/HTTP"):
# готового контейнера/HTTP-режима у самого glitchtip-mcp нет, а встроенный
# MCP GlitchTip (GLITCHTIP_ENABLE_MCP=true) требует интерактивного OAuth-
# consent, что не подходит для headless service-to-service вызова из n8n —
# см. infra-agents/repo/README.md.
#
# Собирается локально один раз и пушится в приватный registry этого кластера
# (registry.${base_domain}) — не через Forgejo Actions: раннер стенда
# проверяется отдельным тривиальным тестовым workflow (DoD п.4), не сборкой
# этого образа.
FROM node:20-alpine

RUN npm install -g supergateway@3 glitchtip-mcp

EXPOSE 8080

ENTRYPOINT ["supergateway", "--stdio", "glitchtip-mcp", "--outputTransport", "sse", "--port", "8080"]
