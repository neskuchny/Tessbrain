# Secrets Management (W11)

`.env` файл — простейший вариант, но в SOC 2-compliant deploy секреты
должны быть **encrypted-at-rest** и иметь **versioned audit trail**.
Этот документ описывает три рекомендованных пути по уровню зрелости.

## Уровень 0 — `.env` + filesystem perms (dev only)

```bash
chmod 600 .env
chown tessent:tessent .env
```

Только для local dev и одиночных POC. **Не для prod**: любой кто
получает root доступ читает `.env`; нет ротации; нет audit log.

## Уровень 1 — sops + age (recommended baseline)

Простое решение для single-VPS / git-ops workflow. Секреты живут в
git encrypted, расшифровываются на старте контейнера.

### Setup

```bash
# 1. Установить sops + age
brew install sops age           # Mac
apt install age && curl ... | tar  # Linux: см. github.com/getsops/sops

# 2. Сгенерировать age-ключ для оператора (и забэкапить!)
age-keygen -o ~/.config/sops/age/keys.txt
# Public key: age1abc123...

# 3. На сервере деплоя — тот же приватный ключ
mkdir -p /etc/sops/age && \
    cp ~/.config/sops/age/keys.txt /etc/sops/age/keys.txt && \
    chmod 600 /etc/sops/age/keys.txt
```

В корне репо `.sops.yaml`:

```yaml
creation_rules:
  - path_regex: \.env\.sops$
    age: age1abc123...   # public key оператора
```

Зашифровать `.env`:

```bash
sops -e .env > .env.sops
git add .sops.yaml .env.sops
git commit -m "secrets: encrypted env"
# .env остаётся в .gitignore — НИ В КОЕМ СЛУЧАЕ не коммитим plaintext.
```

В `entrypoint.sh` контейнера:

```bash
#!/bin/sh
set -e
export SOPS_AGE_KEY_FILE=/etc/sops/age/keys.txt
sops -d /app/.env.sops > /tmp/.env
set -a; . /tmp/.env; set +a
rm /tmp/.env  # plaintext не должен жить дольше initialization
exec python -m backend.api.app
```

### Ротация ключа

```bash
# 1. Сгенерировать новый age-ключ
age-keygen -o ~/.config/sops/age/keys-v2.txt
# 2. Обновить .sops.yaml с НОВЫМ public key
# 3. Re-encrypt
sops updatekeys .env.sops
git commit -am "secrets: rotate age key"
# 4. Старый ключ безопасно архивировать (нужен для расшифровки старых
#    git revisions при forensic, но не для prod).
```

## Уровень 2 — Hashicorp Vault (enterprise)

Для multi-server deploy, dynamic credentials, fine-grained ACL.

### Архитектура

```
[Vault server] ←→ [Tessbrain app servers]
       │
       └→ Postgres dynamic creds (24h TTL)
       └→ AWS IAM dynamic keys
       └→ static KV для API keys (ротация раз в 90 дней)
```

### Application integration

```python
# В backend/api/app.py перед загрузкой Settings:
import hvac
client = hvac.Client(url="https://vault.example.com", token=os.environ["VAULT_TOKEN"])
secrets = client.secrets.kv.v2.read_secret_version(path="tessent/prod")["data"]["data"]
for k, v in secrets.items():
    os.environ.setdefault(k, v)
```

`VAULT_TOKEN` injected через AppRole authentication (orchestrator
рендерит short-lived token, app exchange'ит на full token).

### Pros/cons vs sops

| | sops + age | Vault |
|---|---|---|
| Setup | 30 минут | 1-2 дня |
| Ops overhead | низкий (один ключ-файл) | высокий (HA cluster) |
| Audit trail | git log | full audit log API |
| Dynamic creds | нет | есть (Postgres / AWS / etc.) |
| Cost | 0 | $0 OSS / $$$ Enterprise |
| Когда выбирать | <5 серверов, single-tenant | enterprise, multi-tenant prod |

## Какие секреты считаем критичными

Эти ОБЯЗАТЕЛЬНО должны быть зашифрованы (любой из уровней выше),
не в plain `.env` в проде:

- `JWT_SECRET_KEY`, `SECRET_KEY` — подделка JWT = compromise аккаунта
- `POSTGRES_PASSWORD`, `NEO4J_PASSWORD` — full data access
- `OPENAI_API_KEY`, `GOOGLE_API_KEY` — биллинг + LLM-выкачка
- `SUPABASE_SERVICE_KEY` — bypass RLS
- `BACKUP_AWS_*` — полный бэкап = data exfiltration
- `SERVICE_JWT_SECRET` — подделка inter-service auth
- `PGCRYPTO_FIELD_KEY` (W11) — расшифровка PII at-rest

Менее критичные (можно оставить в plain `.env` если perms 600):

- `LITELLM_MASTER_KEY` (если используется для прокси)
- `MEETFLOW_API_URL` (это URL, не secret)
- хосты, имена пользователей БД (без паролей)

## Compliance impact

| Control | Requirement | Solution |
|---|---|---|
| SOC 2 CC6.1 | Logical access | sops + git ACL (l1) или Vault ACL (l2) |
| SOC 2 CC6.7 | Encryption-at-rest | sops AES-256 / Vault transit |
| GDPR Art. 32 | Pseudonymisation/encryption | pgcrypto for PII (мiграция 050) |
| ISO 27001 A.10 | Cryptography | sops/Vault + key rotation policy |

## Связанные документы

- `tessent_brain/COMPLIANCE.md` — общий compliance reference
- `tessent_brain/ENTERPRISE.md` — enterprise/on-prem deployment
- Migration `050_pgcrypto.sql` — pgcrypto helper functions для PII fields

## Цикл обратной связи (guide/feedback)

| Переменная | Зачем |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Бот, который шлёт тикеты пользователей в группу саппорта |
| `TESSENT_SUPPORT_CHAT_ID` | chat_id группы саппорта (добавьте бота в группу, возьмите id через @getidsbot) |

Проверка: `GET /api/v1/guide/feedback/health` — покажет, что настроено и
что осталось (включая наличие Supabase-таблицы `feedback_tickets`, миграция 268).
