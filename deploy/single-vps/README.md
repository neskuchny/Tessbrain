# Single-VPS deploy preset

Полный стек tessent_brain (Postgres + Redis + Qdrant + Neo4j + API + Worker
+ Caddy с auto-HTTPS) на одной 8 GB VPS. Целевая стоимость
**~$20–30/мес/клиент** инфраструктуры (без LLM-расхода).

## Когда использовать

- ≤ 50 пользователей, ≤ 200 встреч/мес.
- Один клиент = одна VPS (single-tenant) ИЛИ несколько клиентов на одной VPS
  с включённым `MULTITENANT_STRICT=true` (см. `tessent_brain/MULTITENANT.md`).

Если клиентов >5 или нагрузка растёт — переезжайте на managed Postgres (Neon)
+ managed Qdrant + managed Neo4j AuraDB; базовая стоимость поднимется до
~$120/мес, но performance и backup'ы становятся чьей-то ещё проблемой.

## Pre-flight checklist

| ✓ | Что |
|---|---|
| ☐ | VPS: Ubuntu 22.04 / Debian 12, ≥ 8 GB RAM, ≥ 60 GB SSD, открыты 22/80/443 |
| ☐ | Docker 24+ и `docker compose` v2 установлены |
| ☐ | Домен направлен A-записью на IP VPS (для Caddy ACME) |
| ☐ | Gemini API key (или OpenAI) |
| ☐ | S3-compatible bucket для бэкапов (опционально, но крайне рекомендуется) |

## Установка

```bash
# 1. Склонировать репо на VPS
git clone https://github.com/<your-fork>/tessent-test.git /opt/tessent
cd /opt/tessent/tessent_brain/deploy/single-vps

# 2. Заполнить .env
cp .env.example .env
nano .env
# - DOMAIN
# - ACME_EMAIL
# - все CHANGE_ME_* пароли (openssl rand -hex 24 / -base64 48)
# - GOOGLE_API_KEY или OPENAI_API_KEY

# 3. Поднять стек
docker compose pull
docker compose up -d

# 4. Дождаться готовности
docker compose ps                # все healthy
curl -sf https://$DOMAIN/readyz  # 200 + JSON со списком сервисов

# 5. Прогнать RLS-миграцию (если планируется multi-tenant)
docker compose exec postgres \
    psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -f /docker-entrypoint-initdb.d/020_multitenant_rls.sql
# (миграция уже скопирована в init.sql во время первого запуска,
#  но повторный запуск идемпотентен)
```

## Бэкапы

```bash
# 1. Установить awscli
sudo apt install -y awscli

# 2. AWS-credentials в /root/.aws/credentials или env
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=us-east-1
export BACKUP_S3_BUCKET=s3://my-bucket/tessent

# Для S3-совместимых (B2, R2, MinIO):
# export AWS_ENDPOINT_URL=https://s3.us-west-002.backblazeb2.com

# 3. Прогнать backup вручную
./backup.sh

# 4. Cron — ежедневно в 03:30 UTC
sudo crontab -e
# 30 3 * * * cd /opt/tessent/tessent_brain/deploy/single-vps \
#            && ./backup.sh >> /var/log/tessent-backup.log 2>&1
```

`backup.sh`:
- Postgres → `pg_dump --clean --if-exists | gzip`.
- Neo4j → `neo4j-admin database dump` (без даунтайма, через volume read-only),
  fallback на APOC cypher-export при ошибке.
- Qdrant → snapshot API + tar архив папки snapshots/.
- Заливает всё в `$BACKUP_S3_BUCKET/<UTC-timestamp>/`.
- Удаляет старше `BACKUP_RETENTION_DAYS` (по умолчанию 14 дней).

## Восстановление

```bash
# Postgres
docker compose exec -T postgres psql -U "$POSTGRES_USER" "$POSTGRES_DB" \
    < <(aws s3 cp s3://my-bucket/tessent/<TS>/postgres-<TS>.sql.gz - | gunzip)

# Neo4j
docker compose down neo4j
aws s3 cp s3://my-bucket/tessent/<TS>/neo4j-<TS>.tar.gz /tmp/
tar -xzf /tmp/neo4j-<TS>.tar.gz -C /tmp/
docker run --rm -v tessent_brain_neo4j_data:/data \
    -v /tmp/neo4j-<TS>:/backups \
    neo4j:5.24-community \
    neo4j-admin database load neo4j --from-path=/backups --overwrite-destination=true
docker compose up -d neo4j

# Qdrant — восстановление через snapshot recover API,
# см. https://qdrant.tech/documentation/concepts/snapshots/
```

## Backup verification (W10)

`backup.sh` написан простым shell — теоретически может молча сломаться
(пустые дампы, корраптнутый gzip). `restore-test.sh` качает последний
backup из S3 и проверяет:

```bash
# Smoke (30 сек, ежедневно):
./restore-test.sh

# Full restore (5 мин, еженедельно): поднимает scratch-postgres и
# реально загружает дамп — самая надёжная проверка.
./restore-test.sh --full
```

Cron-рекомендация (на той же машине что и `backup.sh`):

```
# ежедневная smoke-проверка через 30 минут после backup'а
0 4 * * *   cd /opt/tessent/tessent_brain/deploy/single-vps && ./restore-test.sh
# еженедельный full-restore по понедельникам утром
0 5 * * 1   cd /opt/tessent/tessent_brain/deploy/single-vps && ./restore-test.sh --full
```

Скрипт fail-fast: первая же проблема (старее 48h, пустой файл, битый
gzip, отсутствующая ключевая таблица) → exit 1, попадёт в логи и
любой alerting-pipeline.

## Database migrations (W10)

SQL-миграции лежат в `backend/db/migrations/` и применяются автоматически
через runner с tracking-таблицей `public.schema_migrations`:

```bash
# Применить все pending
docker compose exec api python -m backend.db.migrate

# Список доступных миграций
docker compose exec api python -m backend.db.migrate --list

# Apply до конкретной версии (для постепенных rollouts)
docker compose exec api python -m backend.db.migrate --target 040_audit_log

# Dry run — увидеть что бы применилось
docker compose exec api python -m backend.db.migrate --dry-run
```

Runner идемпотентен: уже применённые версии пропускаются. Защита от
гонок в multi-replica deploy через `pg_advisory_lock` — две реплики
не побьют друг друга при одновременном `migrate`.

Рекомендуется в entrypoint API-контейнера: делать `python -m backend.db.migrate`
перед запуском сервиса. На фрешем environment это создаёт всю схему
с нуля; на существующем — no-op.

## Памятка по нагрузке

На 8 GB VPS лимиты в `docker-compose.yml`:

| Сервис | RAM-cap | Назначение |
|---|---|---|
| postgres | 2 GB | основной OLTP + TimescaleDB |
| qdrant | 2 GB | вектор-индекс ~5M точек |
| neo4j | 2 GB | граф ~1M нод (heap 1G + page-cache 512M) |
| api | 1.5 GB | Granian + Gemini-клиенты |
| worker | 1 GB | Taskiq + ночные джобы |
| redis | 512 MB | кэш + очередь |
| caddy | ~50 MB | TLS-терминатор |
| **итого** | ~9 GB | при пиках уходит в swap; при росте → managed Postgres |

## Откат

```bash
# Полная остановка + удаление контейнеров (volumes остаются → данные целы):
docker compose down

# С volumes (УДАЛИТ ДАННЫЕ):
docker compose down -v
```

## Известные ограничения

- Caddy получает сертификат через ACME — на первой загрузке нужен публичный
  IP и валидный DNS. За CDN/Cloudflare нужно отключить proxy-режим до выпуска
  сертификата (или использовать DNS-01).
- При обновлении `IMAGE_TAG` сначала `docker compose pull`, потом `up -d` —
  иначе Compose пересоберёт image из контекста.
- Neo4j community не поддерживает hot-online dump → ежедневный бэкап
  использует read-only volume. Это безопасно, но требует, чтобы Neo4j
  завершил wal-flush к моменту дампа.
