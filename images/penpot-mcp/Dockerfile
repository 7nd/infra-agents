# Собран из форка informatJonas/penpot-mcp-server (ветка
# fix/http-transport-shared-session, commit e50d2493) — у оригинала
# (zcube/penpot-mcp-server, публикуется на npm как @zcubekr/penpot-mcp-server)
# баг: один глобальный StreamableHTTPServerTransport на весь процесс
# вместо Map sessionId->transport, из-за чего ЛЮБОЙ клиент после первого
# получает "Server already initialized" — воспроизведено вживую на этом
# деплое. Баг открыт (zcube/penpot-mcp-server#2), фикс есть, но не
# смёржен и не опубликован в npm (zcube/penpot-mcp-server#3) — собираем
# из ветки с фиксом напрямую.
#
# Собран и запушен в registry.${base_domain} вручную (одноразово, не
# через CI) — тот же приём, что images/glitchtip-mcp.
FROM node:20-alpine AS build
RUN apk add --no-cache git
WORKDIR /build
RUN git clone --branch fix/http-transport-shared-session --depth 1 \
      https://github.com/informatJonas/penpot-mcp-server.git .
RUN npm install
RUN npm run build
RUN npm prune --omit=dev

FROM node:20-alpine
WORKDIR /app
COPY --from=build /build/dist ./dist
COPY --from=build /build/node_modules ./node_modules
COPY --from=build /build/package.json ./package.json
ENV TRANSPORT=http
ENV HTTP_HOST=0.0.0.0
ENV HTTP_PORT=8080
EXPOSE 8080
CMD ["node", "dist/index.js"]
