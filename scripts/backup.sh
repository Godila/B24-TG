#!/usr/bin/env bash
# Ночной бэкоп ЧатМост (cron на хосте VM, /etc/cron.d/bitrix-tg-backup).
#
# Забирает всё невоспроизводимое состояние:
#   1. БД (pg_dump из контейнера postgres, gzip);
#   2. tg_sessions ( Telethon .session-файлы ) — tar из docker-volume;
#   3. .env (кредиты B24/прокси/секреты — права 600).
# Хранение: backups/ рядом с проектом, ротация старше KEEP_DAYS дней.
# Выгрузка наружу (опционально): BACKUP_UPLOAD_DST="user@host:/path" в
# /etc/bitrix-tg-backup.env — тогда свежие архивы копируются scp'ом.
#
# Ручной запуск: /opt/bitrix-tg/scripts/backup.sh
set -euo pipefail

PROJECT_DIR="/opt/bitrix-tg"
BACKUP_DIR="${PROJECT_DIR}/backups"
KEEP_DAYS="${KEEP_DAYS:-7}"
STAMP="$(date +%F_%H%M)"
VOLUME_TG_SESSIONS="bitrix-tg_tg_sessions"

mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"

echo "[$(date -Is)] backup start (${STAMP})"

# 1. База данных.
docker compose -f "${PROJECT_DIR}/docker-compose.yml" exec -T postgres \
    pg_dump -U bitrix_tg bitrix_tg \
    | gzip > "${BACKUP_DIR}/db_${STAMP}.sql.gz"
echo "  db: db_${STAMP}.sql.gz ($(du -h "${BACKUP_DIR}/db_${STAMP}.sql.gz" | cut -f1))"

# 2. Telethon-сессии (образ postgres:16-alpine уже скачан — busybox tar).
docker run --rm --entrypoint /bin/tar \
    -v "${VOLUME_TG_SESSIONS}:/data:ro" \
    -v "${BACKUP_DIR}:/backup" \
    postgres:16-alpine \
    czf "/backup/tg_sessions_${STAMP}.tar.gz" -C /data .
echo "  tg_sessions: tg_sessions_${STAMP}.tar.gz ($(du -h "${BACKUP_DIR}/tg_sessions_${STAMP}.tar.gz" | cut -f1))"

# 3. .env.
cp "${PROJECT_DIR}/.env" "${BACKUP_DIR}/env_${STAMP}"
chmod 600 "${BACKUP_DIR}/env_${STAMP}"
echo "  env: env_${STAMP}"

# 4. Ротация.
find "${BACKUP_DIR}" -maxdepth 1 -type f \
    \( -name 'db_*' -o -name 'tg_sessions_*' -o -name 'env_*' \) \
    -mtime "+${KEEP_DAYS}" -delete
echo "  retention: старше ${KEEP_DAYS} дней удалено"

# 5. Выгрузка наружу (если настроена в /etc/bitrix-tg-backup.env).
ENV_FILE="/etc/bitrix-tg-backup.env"
if [ -f "${ENV_FILE}" ]; then
    # shellcheck disable=SC1090
    . "${ENV_FILE}"
fi
if [ -n "${BACKUP_UPLOAD_DST:-}" ]; then
    scp -q -o ConnectTimeout=15 \
        "${BACKUP_DIR}/db_${STAMP}.sql.gz" \
        "${BACKUP_DIR}/tg_sessions_${STAMP}.tar.gz" \
        "${BACKUP_DIR}/env_${STAMP}" \
        "${BACKUP_UPLOAD_DST}"
    echo "  upload: ${BACKUP_UPLOAD_DST} — ок"
fi

echo "[$(date -Is)] backup done"
