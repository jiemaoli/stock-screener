#!/bin/sh
set -eu

APP_DIR=/root/apps/stock-screener
LOG_FILE="$APP_DIR/data/logs/live-upgrade.log"

exec 9>"$APP_DIR/data/.scan.lock"
/usr/bin/flock -n 9 || exit 1

echo "$(date -Is) waiting for the legacy full scan" >> "$LOG_FILE"
while docker top stock-screener-dashboard 2>/dev/null | grep -q '[r]un_optimized_scan.py'; do
    sleep 30
done

echo "$(date -Is) legacy scan finished; reconciling Compose services" >> "$LOG_FILE"
printf '{"status":"completed","full":true,"completed_at":"%s","note":"Legacy full scan completed during live upgrade"}\n' "$(date -Is)" > "$APP_DIR/data/logs/scan_job.json"
docker rm -f stock-screener-dashboard >> "$LOG_FILE" 2>&1 || true

cd "$APP_DIR"
docker compose -f deploy/docker-compose.yml up -d --no-deps --force-recreate dashboard scheduler >> "$LOG_FILE" 2>&1

cp deploy/nginx-stock-min.conf /root/infrastructure/nginx/conf.d/stock-min.conf
docker exec nginx-proxy nginx -t >> "$LOG_FILE" 2>&1
docker exec nginx-proxy nginx -s reload >> "$LOG_FILE" 2>&1
docker rm -f stock-screener-dashboard-v2 >> "$LOG_FILE" 2>&1 || true

echo "$(date -Is) live upgrade complete" >> "$LOG_FILE"
