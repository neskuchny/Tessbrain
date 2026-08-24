#!/usr/bin/env bash
# ============================================================================
# TESSENT BRAIN — Single-VPS Backup Script
# Бэкапит Postgres + Neo4j + Qdrant в S3-compatible bucket.
# Запускать из cron, например ежедневно в 03:30 UTC:
#   30 3 * * * cd /opt/tessent_brain/deploy/single-vps && ./backup.sh >> /var/log/tessent-backup.log 2>&1
#
# Зависимости на хосте:
#   - docker compose
#   - awscli или mc (MinIO client) для аплоада
#   - gpg (если BACKUP_ENCRYPTION_PASSPHRASE задан) — W40
#   - flock (util-linux), sha256sum, jq
#
# Конфигурация (через env, не флаги):
#   BACKUP_S3_BUCKET=s3://my-bucket/tessent
#   BACKUP_RETENTION_DAYS=14
#   AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION
#   AWS_ENDPOINT_URL=  (для S3-совместимых: B2, R2, MinIO)
#
# W40 additions:
#   BACKUP_ENCRYPTION_PASSPHRASE=  (если задано — каждый файл gpg AES256
#                                    с расширением .gpg перед upload'ом)
#   BACKUP_LOCK_FILE=/tmp/tessent-backup.lock  (по умолчанию)
#   BACKUP_NOTIFY_WEBHOOK=         (POST JSON при ошибке/успехе)
#   BACKUP_METRIC_FILE=/var/lib/prometheus/textfile/tessent_backup.prom
#                                  (если задан — записывает timestamp успеха
#                                   в Prometheus textfile-collector format)
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")"

# === W40: lock file — предотвращает parallel runs из cron ===================
LOCK_FILE="${BACKUP_LOCK_FILE:-/tmp/tessent-backup.lock}"
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
    echo "[$(date -u +%FT%TZ)] another backup run already in progress; exiting"
    exit 0
fi

TS=$(date -u +"%Y%m%dT%H%M%SZ")
TMPDIR=$(mktemp -d -t tessent-backup-XXXXXX)
trap 'rm -rf "$TMPDIR"' EXIT

: "${BACKUP_S3_BUCKET:?BACKUP_S3_BUCKET must be set (e.g. s3://my-bucket/tessent)}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"

# .env здесь же — для пробросов паролей в exec-команды.
# shellcheck source=/dev/null
[ -f .env ] && set -a && source .env && set +a

# === W40: error notification helper =========================================
notify() {
    local status="$1"
    local msg="$2"
    [ -z "${BACKUP_NOTIFY_WEBHOOK:-}" ] && return 0
    local payload
    payload=$(printf '{"status":"%s","timestamp":"%s","host":"%s","message":"%s"}' \
        "$status" "$(date -u +%FT%TZ)" "$(hostname)" "$msg")
    curl -fsS --max-time 10 -H 'Content-Type: application/json' \
        -d "$payload" "$BACKUP_NOTIFY_WEBHOOK" >/dev/null 2>&1 || true
}

trap 'notify failure "backup script aborted at line $LINENO"' ERR

# === W40: encryption helper =================================================
# Если BACKUP_ENCRYPTION_PASSPHRASE задан — заворачиваем файл в gpg AES256,
# результат — file.gpg, оригинал удаляется. Без passphrase — no-op.
encrypt_if_enabled() {
    local f="$1"
    [ -z "${BACKUP_ENCRYPTION_PASSPHRASE:-}" ] && return 0
    [ ! -f "$f" ] && return 0
    echo "    encrypting $(basename "$f")"
    gpg --batch --yes --quiet \
        --symmetric --cipher-algo AES256 \
        --passphrase "$BACKUP_ENCRYPTION_PASSPHRASE" \
        --pinentry-mode loopback \
        --output "${f}.gpg" "$f"
    rm -f "$f"
}

echo "[$(date -u +%FT%TZ)] backup start: $TS"

# ----- Postgres ---------------------------------------------------------------
PG_FILE="$TMPDIR/postgres-$TS.sql.gz"
echo "  postgres -> $PG_FILE"
docker compose exec -T postgres \
    pg_dump --clean --if-exists --no-owner --no-privileges \
    -U "${POSTGRES_USER}" "${POSTGRES_DB}" \
    | gzip -9 > "$PG_FILE"

# ----- Optional: meetflow Postgres (W6) ---------------------------------------
# Если в окружении заданы координаты meetflow-БД (например, alfa_asynk_meetflow
# поднята отдельно), бэкапим её тем же pg_dump. Не падаем, если недоступно.
if [ -n "${MEETFLOW_DATABASE_URL:-}" ]; then
    MEETFLOW_FILE="$TMPDIR/meetflow-postgres-$TS.sql.gz"
    echo "  meetflow postgres -> $MEETFLOW_FILE"
    if pg_dump "$MEETFLOW_DATABASE_URL" --clean --if-exists --no-owner --no-privileges \
        | gzip -9 > "$MEETFLOW_FILE"; then
        :
    else
        echo "  WARNING: meetflow pg_dump failed; continuing"
        rm -f "$MEETFLOW_FILE"
    fi
fi

# ----- Neo4j ------------------------------------------------------------------
# Neo4j community: dump только при остановленной БД. Используем admin-dump через
# отдельный контейнер по тому же volume — без даунтайма.
NEO_DIR="$TMPDIR/neo4j-$TS"
mkdir -p "$NEO_DIR"
echo "  neo4j -> $NEO_DIR"
# Запускаем neo4j-admin поверх того же volume, что и продовский контейнер.
docker run --rm \
    -v "tessent_brain_neo4j_data:/data:ro" \
    -v "$NEO_DIR:/backups" \
    --user "$(id -u):$(id -g)" \
    neo4j:5.24-community \
    neo4j-admin database dump neo4j --to-path=/backups || {
        # Если админ-дамп не сработал (например, БД залочена) — fallback на
        # online-export через cypher-shell. Грубее, но не падает.
        echo "  neo4j-admin dump failed; falling back to APOC export"
        docker compose exec -T neo4j \
            cypher-shell -u neo4j -p "${NEO4J_PASSWORD}" \
            "CALL apoc.export.cypher.all(null, {format: 'plain', useOptimizations: {type: 'UNWIND_BATCH', unwindBatchSize: 100}}) YIELD cypherStatements RETURN cypherStatements" \
            > "$NEO_DIR/neo4j-cypher-$TS.cypher" || true
    }
tar -C "$TMPDIR" -czf "$TMPDIR/neo4j-$TS.tar.gz" "neo4j-$TS"
rm -rf "$NEO_DIR"

# ----- Qdrant -----------------------------------------------------------------
# Qdrant snapshot API: создаём snapshot и копируем файл наружу.
QDRANT_FILE="$TMPDIR/qdrant-$TS.tar.gz"
echo "  qdrant -> $QDRANT_FILE"
SNAPSHOT_NAME=$(docker compose exec -T qdrant \
    sh -c "wget -qO- --header='api-key: ${QDRANT_API_KEY}' \
           --post-data='' http://localhost:6333/snapshots" \
    | grep -oP '\"name\":\"[^\"]+' | head -1 | cut -d'"' -f4) || true
if [ -n "$SNAPSHOT_NAME" ]; then
    docker run --rm \
        -v "tessent_brain_qdrant_data:/qdrant/storage:ro" \
        -v "$TMPDIR:/out" \
        alpine:3.20 \
        sh -c "cd /qdrant/storage && tar -czf /out/qdrant-$TS.tar.gz snapshots/"
else
    echo "  WARNING: qdrant snapshot creation failed; skipping qdrant in this run"
fi

# ----- app_data (ФАЙЛОВАЯ ПАМЯТЬ) ---------------------------------------------
# Аудит M3 [БЛОКЕР]: per-user JSON-сторы (event_log/cross_source_mentions/
# supersede/plan_memory/insights) + NetworkX-граф + vector-fallback живут в
# named-volume app_data и РАНЬШЕ НЕ БЭКАПИЛИСЬ — потеря тома была невосстановима.
# Тарим volume отдельным read-only контейнером (без даунтайма, как neo4j/qdrant).
APPDATA_FILE="$TMPDIR/app_data-$TS.tar.gz"
echo "  app_data -> $APPDATA_FILE"
docker run --rm \
    -v "tessent_brain_app_data:/app/data:ro" \
    -v "$TMPDIR:/out" \
    alpine:3.20 \
    sh -c "cd /app/data && tar -czf /out/app_data-$TS.tar.gz ." || {
        echo "  WARNING: app_data tar failed; skipping app_data in this run"
    }

# ----- W40: encryption (если задан passphrase) -------------------------------
for f in "$TMPDIR"/*.gz "$TMPDIR"/*.tar.gz; do
    [ -f "$f" ] && encrypt_if_enabled "$f"
done

# ----- W40: manifest со sha256 + metadata ------------------------------------
MANIFEST="$TMPDIR/manifest.json"
echo "  building manifest -> $MANIFEST"
{
    echo "{"
    echo "  \"timestamp\": \"$TS\","
    echo "  \"host\": \"$(hostname)\","
    echo "  \"encrypted\": $([ -n "${BACKUP_ENCRYPTION_PASSPHRASE:-}" ] && echo true || echo false),"
    echo "  \"retention_days\": $RETENTION_DAYS,"
    echo "  \"files\": ["
    first=1
    for f in "$TMPDIR"/*; do
        [ ! -f "$f" ] && continue
        [ "$(basename "$f")" = "manifest.json" ] && continue
        [ $first -eq 0 ] && echo "    ,"
        first=0
        sha=$(sha256sum "$f" | cut -d' ' -f1)
        size=$(stat -c%s "$f" 2>/dev/null || stat -f%z "$f")
        printf '    {"name":"%s","sha256":"%s","size_bytes":%s}' \
            "$(basename "$f")" "$sha" "$size"
    done
    echo ""
    echo "  ]"
    echo "}"
} > "$MANIFEST"

# ----- S3 upload --------------------------------------------------------------
echo "  uploading to $BACKUP_S3_BUCKET/$TS/"
ENDPOINT_FLAG=""
[ -n "${AWS_ENDPOINT_URL:-}" ] && ENDPOINT_FLAG="--endpoint-url $AWS_ENDPOINT_URL"

# shellcheck disable=SC2086
aws $ENDPOINT_FLAG s3 cp --no-progress --recursive "$TMPDIR/" "$BACKUP_S3_BUCKET/$TS/" \
    --exclude "*" \
    --include "postgres-*.sql.gz*" \
    --include "meetflow-postgres-*.sql.gz*" \
    --include "neo4j-*.tar.gz*" \
    --include "qdrant-*.tar.gz*" \
    --include "app_data-*.tar.gz*" \
    --include "manifest.json"

# ----- Retention --------------------------------------------------------------
# Удаляем папки старше N дней. Простая реализация: список + cutoff по имени.
CUTOFF=$(date -u -d "$RETENTION_DAYS days ago" +"%Y%m%dT%H%M%SZ")
echo "  retention cutoff: $CUTOFF"
# shellcheck disable=SC2086
aws $ENDPOINT_FLAG s3 ls "$BACKUP_S3_BUCKET/" \
    | awk '{print $2}' \
    | sed 's:/$::' \
    | while read -r dir; do
        [ -z "$dir" ] && continue
        if [[ "$dir" < "$CUTOFF" ]]; then
            echo "    expiring $dir"
            aws $ENDPOINT_FLAG s3 rm --recursive "$BACKUP_S3_BUCKET/$dir/" || true
        fi
    done

# ----- W40: Prometheus textfile metric для алерта на stale backup -----------
if [ -n "${BACKUP_METRIC_FILE:-}" ]; then
    metric_dir=$(dirname "$BACKUP_METRIC_FILE")
    mkdir -p "$metric_dir" 2>/dev/null || true
    epoch=$(date +%s)
    {
        echo "# HELP tessent_backup_last_success_timestamp_seconds Unix timestamp of last successful backup"
        echo "# TYPE tessent_backup_last_success_timestamp_seconds gauge"
        echo "tessent_backup_last_success_timestamp_seconds $epoch"
        echo "# HELP tessent_backup_last_size_bytes Total size of last backup in bytes (sum of files)"
        echo "# TYPE tessent_backup_last_size_bytes gauge"
        total=0
        for f in "$TMPDIR"/*; do
            [ -f "$f" ] || continue
            sz=$(stat -c%s "$f" 2>/dev/null || stat -f%z "$f" 2>/dev/null || echo 0)
            total=$((total + sz))
        done
        echo "tessent_backup_last_size_bytes $total"
    } > "${BACKUP_METRIC_FILE}.tmp" && mv "${BACKUP_METRIC_FILE}.tmp" "$BACKUP_METRIC_FILE"
fi

notify success "backup $TS uploaded to $BACKUP_S3_BUCKET"
echo "[$(date -u +%FT%TZ)] backup ok: $TS"
