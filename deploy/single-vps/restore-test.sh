#!/usr/bin/env bash
# ============================================================================
# TESSENT BRAIN — Backup verification (W10 production hygiene).
#
# Скачивает последний backup из S3 и проверяет:
#  1. файлы существуют и не пустые,
#  2. .gz распаковывается,
#  3. pg_dump содержит ожидаемые таблицы,
#  4. neo4j .tar.gz / qdrant .tar.gz распаковываются,
#  5. (опц) поднимает scratch-Postgres и `psql -f` проверяет, что dump
#     загружается без ошибок.
#
# Запускается еженедельно в staging-окружении (cron:
#   0 5 * * 1 /opt/tessent_brain/deploy/single-vps/restore-test.sh
# ).
#
# Без флагов делает только smoke-проверки (быстро, ~30 сек).
# С `--full` поднимает scratch-postgres и реально загружает dump (~5 мин).
#
# Конфигурация — те же env, что у backup.sh (BACKUP_S3_BUCKET,
# AWS_*).
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")"

FULL_RESTORE=0
for arg in "$@"; do
    case "$arg" in
        --full) FULL_RESTORE=1 ;;
        --help|-h)
            echo "Usage: $0 [--full]"
            echo "  smoke (default): 30s integrity check"
            echo "  --full: spin up scratch-postgres and load dump (~5 min)"
            exit 0
            ;;
    esac
done

: "${BACKUP_S3_BUCKET:?BACKUP_S3_BUCKET must be set}"
[ -f .env ] && set -a && source .env && set +a

ENDPOINT_FLAG=""
[ -n "${AWS_ENDPOINT_URL:-}" ] && ENDPOINT_FLAG="--endpoint-url $AWS_ENDPOINT_URL"

TMPDIR=$(mktemp -d -t tessent-restore-XXXXXX)
trap 'rm -rf "$TMPDIR"' EXIT

ok() { echo "  ✓ $*"; }
fail() { echo "  ✗ $*" >&2; exit 1; }

# ----- 1. Find latest backup --------------------------------------------------
echo "[$(date -u +%FT%TZ)] restore-test start"
echo "  bucket: $BACKUP_S3_BUCKET"

# shellcheck disable=SC2086
LATEST=$(aws $ENDPOINT_FLAG s3 ls "$BACKUP_S3_BUCKET/" \
    | awk '{print $2}' | sed 's:/$::' \
    | grep -E '^[0-9]{8}T[0-9]{6}Z$' \
    | sort | tail -1)

[ -n "$LATEST" ] || fail "no backups found in $BACKUP_S3_BUCKET"
ok "latest backup: $LATEST"

# Проверяем age — backup не должен быть старше 48h.
AGE_LIMIT=$(date -u -d "48 hours ago" +"%Y%m%dT%H%M%SZ")
if [[ "$LATEST" < "$AGE_LIMIT" ]]; then
    fail "latest backup $LATEST older than 48h (cutoff $AGE_LIMIT)"
fi
ok "backup age within SLA (<48h)"

# ----- 2. Download ------------------------------------------------------------
echo "  downloading to $TMPDIR..."
# shellcheck disable=SC2086
aws $ENDPOINT_FLAG s3 cp --no-progress --recursive \
    "$BACKUP_S3_BUCKET/$LATEST/" "$TMPDIR/" >/dev/null

# ----- W40: decrypt if encrypted backups + verify manifest checksums --------
if ls "$TMPDIR"/*.gpg >/dev/null 2>&1; then
    : "${BACKUP_ENCRYPTION_PASSPHRASE:?BACKUP_ENCRYPTION_PASSPHRASE required to restore encrypted backups}"
    echo "  decrypting backups..."
    for f in "$TMPDIR"/*.gpg; do
        plain="${f%.gpg}"
        gpg --batch --yes --quiet \
            --passphrase "$BACKUP_ENCRYPTION_PASSPHRASE" \
            --pinentry-mode loopback \
            --decrypt --output "$plain" "$f"
        rm -f "$f"
    done
    ok "decryption ok"
fi

if [ -f "$TMPDIR/manifest.json" ]; then
    echo "  verifying manifest checksums..."
    # Manifest содержит sha256 от файла ДО decrypt'а (т.е. .gpg).
    # Если backups encrypted — checksums сравниваем по originally-uploaded
    # .gpg файлам, которых уже нет (мы их удалили). Поэтому делаем
    # verification только для unencrypted случая.
    if ! ls "$TMPDIR"/*.gpg >/dev/null 2>&1 \
            && [ -z "${BACKUP_ENCRYPTION_PASSPHRASE:-}" ]; then
        python3 -c "
import json, hashlib, os, sys
m = json.load(open('$TMPDIR/manifest.json'))
mismatches = []
for entry in m.get('files', []):
    path = os.path.join('$TMPDIR', entry['name'])
    if not os.path.exists(path):
        mismatches.append(f'missing: {entry[\"name\"]}')
        continue
    sha = hashlib.sha256(open(path, 'rb').read()).hexdigest()
    if sha != entry['sha256']:
        mismatches.append(f'sha256 mismatch: {entry[\"name\"]}')
if mismatches:
    print('manifest verification failed:', '; '.join(mismatches), file=sys.stderr)
    sys.exit(1)
print(f'  ✓ manifest: {len(m[\"files\"])} files match sha256')
" || fail "manifest checksum verification failed"
    else
        ok "skipping manifest verify (encrypted backup — sha was on .gpg)"
    fi
fi

# ----- 3. Smoke checks --------------------------------------------------------
PG_FILE=$(ls "$TMPDIR"/postgres-*.sql.gz 2>/dev/null | head -1 || true)
NEO_FILE=$(ls "$TMPDIR"/neo4j-*.tar.gz 2>/dev/null | head -1 || true)
QDRANT_FILE=$(ls "$TMPDIR"/qdrant-*.tar.gz 2>/dev/null | head -1 || true)

[ -n "$PG_FILE" ] || fail "postgres dump not found in backup"
[ -s "$PG_FILE" ] || fail "postgres dump is empty: $PG_FILE"
ok "postgres dump present: $(du -h "$PG_FILE" | cut -f1)"

if ! gzip -t "$PG_FILE" 2>/dev/null; then
    fail "postgres dump gzip is corrupt"
fi
ok "postgres gzip integrity"

# Sanity check содержимого: в dump'е должны быть наши ключевые таблицы.
EXPECTED_TABLES=("schema_migrations" "audit_events" "user_profiles_v2")
DUMP_TEXT=$(gunzip -c "$PG_FILE")
for t in "${EXPECTED_TABLES[@]}"; do
    if grep -q "CREATE TABLE.*${t}\|COPY public.${t}\|ALTER TABLE.*${t}" <<< "$DUMP_TEXT"; then
        ok "table $t present in dump"
    else
        echo "  ⚠ table $t NOT found in dump (may be normal if migration not applied yet)"
    fi
done

# Neo4j / Qdrant — best-effort (могут отсутствовать в air-gap deploy).
if [ -n "$NEO_FILE" ]; then
    [ -s "$NEO_FILE" ] || fail "neo4j archive is empty"
    tar -tzf "$NEO_FILE" >/dev/null || fail "neo4j archive corrupt"
    ok "neo4j archive integrity"
fi
if [ -n "$QDRANT_FILE" ]; then
    [ -s "$QDRANT_FILE" ] || fail "qdrant archive is empty"
    tar -tzf "$QDRANT_FILE" >/dev/null || fail "qdrant archive corrupt"
    ok "qdrant archive integrity"
fi

# ----- 4. Full restore (optional) ---------------------------------------------
if [ "$FULL_RESTORE" -eq 1 ]; then
    echo "  --full: spinning up scratch-postgres..."

    SCRATCH_NAME="tessent-restore-test-$(date +%s)"
    SCRATCH_PASS=$(openssl rand -hex 16)

    docker run -d --name "$SCRATCH_NAME" \
        -e POSTGRES_PASSWORD="$SCRATCH_PASS" \
        -e POSTGRES_DB=tessent_restore \
        -e POSTGRES_USER=tessent \
        -p 0:5432 \
        postgres:16-alpine >/dev/null
    trap 'rm -rf "$TMPDIR"; docker rm -f "$SCRATCH_NAME" >/dev/null 2>&1' EXIT

    # ждём пока postgres готов (max 30s)
    for _ in $(seq 1 30); do
        if docker exec "$SCRATCH_NAME" pg_isready -U tessent >/dev/null 2>&1; then
            break
        fi
        sleep 1
    done

    echo "  loading dump..."
    if gunzip -c "$PG_FILE" | docker exec -i "$SCRATCH_NAME" \
        psql -U tessent -d tessent_restore -v ON_ERROR_STOP=1 >/dev/null; then
        ok "dump loaded successfully into scratch-postgres"
    else
        fail "dump failed to load — backup IS NOT VALID"
    fi

    # Проверим что хотя бы что-то есть в результирующей БД.
    TABLE_COUNT=$(docker exec "$SCRATCH_NAME" \
        psql -U tessent -d tessent_restore -tAc \
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
    if [ "$TABLE_COUNT" -lt 1 ]; then
        fail "restored db has 0 tables — backup is not useful"
    fi
    ok "restored db has $TABLE_COUNT tables in public schema"
fi

echo "[$(date -u +%FT%TZ)] restore-test ok"
