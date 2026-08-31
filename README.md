# infra-agents

GitOps-репозиторий для FluxCD: AI Dev Team стенд — n8n как оркестратор AI-агентов
поверх Claude/OpenRouter, с PostgreSQL/Valkey/Qdrant как хранилищами, Forgejo (+
Actions runner) как VCS/CI, GlitchTip как error-tracking, Penpot как дизайн-тул,
и 4 MCP-сервера (Playwright, Postgres read-only, Forgejo/Git, GlitchTip) как tools
для n8n. Разворачивается на том же кластере (`test-on-serverum`), что и
[unitum-demo-k8s-infra](https://github.com/7nd/saas-demo-kubernetes-infra) —
переиспользует его ingress-nginx/cert-manager/external-dns/registry, добавляясь
как отдельный Flux `GitRepository` + два `Kustomization` в тот репозиторий (см.
"Разводка Flux" ниже).

Все внешние эндпоинты — `https://<app>.agents.hightps.online`, публично
доступны (не за VPN), TLS — один wildcard-сертификат `*.agents.hightps.online`
(namespace `agents`), тем же приёмом, что `demo-stands` в примере. Все workloads
— `nodeSelector: node-group: agents` (единственная нода в группе,
`launchpad-agents`).

## Структура

```
namespace.yaml            # namespace agents
certificate.yaml          # wildcard *.agents.${base_domain}
bootstrap/                 # agents-vars (ConfigMap) + agents-secrets (SOPS-Secret) —
                            # раскатывается ОТДЕЛЬНОЙ, более ранней Kustomization
sources/                    # OCIRepository (n8n, forgejo) + HelmRepository (qdrant, glitchtip)
postgres/                   # raw StatefulSet, postgres:17.11, 5 баз (n8n/forgejo/
                             # glitchtip/penpot/agents) + read-only роль mcp_readonly
valkey/                     # raw StatefulSet, valkey/valkey:9.1-alpine
qdrant/                     # HelmRelease (qdrant/qdrant-helm)
n8n/                        # HelmRelease (8gears/n8n-helm-chart, OCI)
forgejo/                    # HelmRelease (forgejo-helm, OCI), образ пинуется на LTS (11)
forgejo-runner/             # raw Deployment: dind sidecar + forgejo-runner daemon
glitchtip/                  # HelmRelease (официальный чарт проекта, GitLab)
penpot/                     # raw manifests (frontend/backend/exporter), нет вменяемого чарта
mcp/
  playwright/                #   mcr.microsoft.com/playwright/mcp, внутренний only
  postgres/                  #   crystaldba/postgres-mcp, --access-mode=restricted,
                              #   подключается ролью mcp_readonly (см. postgres/)
  forgejo-git/                #   ronmi/forgejo-mcp, внутренний only
  glitchtip/                  #   кастомный образ (см. images/), внутренний only
images/glitchtip-mcp/       # Dockerfile: supergateway + glitchtip-mcp (npm), собран
                             # и запушен вручную в registry.${base_domain}
```

## Разводка Flux

Кластер уже раскатан из `unitum-demo-k8s-infra` — этот репозиторий НЕ бутстрапится
отдельно, а подключается к уже работающим Flux-контроллерам как дополнительный
источник. Правки в `unitum-demo-k8s-infra` (минимальные, весь остальной контент
стенда — здесь):

1. `infrastructure/sources/gitrepositories.yaml` — `GitRepository infra-agents`
   (`https://github.com/7nd/infra-agents`, публичный, без `secretRef`).
2. `clusters/demo/infrastructure.yaml` — две `Kustomization`:
   - `infrastructure-agents-vars` (path `./bootstrap`, dependsOn
     `infrastructure-sources`) — раскатывает `agents-vars`/`agents-secrets` в
     `flux-system` ДО того, как остальной стек начнёт их читать через
     `postBuild.substituteFrom`.
   - `infrastructure-agents-stack` (path `./`, dependsOn
     `[infrastructure-issuers, infrastructure-agents-vars]`,
     `postBuild.substituteFrom: [ConfigMap cluster-vars, ConfigMap agents-vars,
     Secret agents-secrets, Secret cluster-secrets]` — `cluster-vars`/
     `cluster-secrets` дают `base_domain`/`ingress_class`/`registry_username`/
     `registry_password` (последний нужен только `mcp/glitchtip/pull-secret.yaml`
     — переиспользует существующий приватный registry вместо отдельного).

Обе используют `decryption.secretRef.name: sops-age` — тот же ключ, что у
`unitum-demo-k8s-infra` (см. `.sops.yaml` здесь: тот же age-recipient, так что
шифровать новые секреты можно публичным ключом без доступа к приватному half).

## Секреты и bootstrap-последовательность

`bootstrap/agents-secrets.yaml` (SOPS) уже содержит все пароли/ключи, которые
можно было сгенерировать заранее (postgres/valkey/qdrant/n8n/glitchtip/penpot/
forgejo admin, включая `forgejo_actions_runner_secret` — см. ниже почему он не
chicken-egg). Четыре значения — заглушки (`""`), заполняются вручную ПОСЛЕ
первого успешного деплоя:

1. **`forgejo_bot_token`** — Personal Access Token бота `${forgejo_admin_username}`
   для MCP Forgejo/Git и (при необходимости) n8n:
   ```sh
   kubectl -n agents exec deploy/forgejo -- forgejo admin user generate-access-token \
     --username agentbot --scopes write:repository,write:issue,read:user \
     --raw
   ```
2. **Forgejo Actions runner** — `forgejo_actions_runner_secret` УЖЕ настоящий
   (сгенерирован заранее, я сам выбрал значение) — единственное, что нужно
   сделать руками: зарегистрировать этот secret на стороне сервера (offline-
   регистрация, без обращения к runner UI):
   ```sh
   kubectl -n agents exec deploy/forgejo -- forgejo forgejo-cli actions register \
     --secret "$(SOPS_AGE_KEY_FILE=<path-to-age.agekey> sops -d bootstrap/agents-secrets.yaml | yq '.stringData.forgejo_actions_runner_secret')"
   ```
   Раннер сам зарегистрируется при следующем старте пода (init-контейнер).
3. **`glitchtip_mcp_token`** — API-токен в GlitchTip для организации
   `${glitchtip_organization}` (создать организацию и токен через UI/API после
   первого логина администратора).
4. **`llm_api_key` / `llm_api_base_url`** — намеренно оставлены пустыми: ключ
   придёт от пользователя позже (кастомный OpenRouter endpoint). До этого
   момента LLM-credential в n8n настраивается вручную через UI (Anthropic-
   совместимый credential с переопределённым Base URL) — деплой стенда на
   этом не блокируется.

Занести полученное значение обратно в SOPS — тем же приёмом, что
`registry_password` в `unitum-demo-k8s-infra`:
```sh
SOPS_AGE_KEY_FILE=<path-to-age.agekey> sops --set '["stringData"]["forgejo_bot_token"] "<значение>"' bootstrap/agents-secrets.yaml
```
закоммитить, дождаться реконсиляции (или `flux reconcile kustomization infrastructure-agents-vars --with-source`).

## Read-only Postgres MCP

Роль `mcp_readonly` (заведена в `postgres/init-configmap.yaml`) имеет `CONNECT`
только на базу `agents` и `SELECT`-only права на её `public`-схему — без
`INSERT`/`UPDATE`/`DELETE` вообще, не только на уровне флага
`--access-mode=restricted` самого MCP-сервера. Проверка:
```sql
-- через сам MCP-tool из n8n, ожидаемый результат — ошибка прав Postgres
INSERT INTO information_schema.tables VALUES (...);
-- ERROR: permission denied for schema public
```

## Что сознательно не сделано

- Формальные бэкапы (pg_dump/snapshot с retention) — по решению пользователя,
  только PVC-персистентность (переживает рестарт пода/ноды, не потерю диска).
- `penpot-mcp` — Penpot официально публикует свой MCP-сервер
  (`penpotapp/mcp`, флаг `enable-mcp`), но он не входит в обязательные 4
  MCP-сервера ТЗ — не разворачивается, чтобы не плодить лишний компонент вне
  скоупа.
