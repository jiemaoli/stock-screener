#!/bin/sh
set -eu

APP_DIR=/root/apps/stock-screener
ARCHIVE_NAME=stock_screener_deploy.tar.gz
LOG_DIR="$APP_DIR/data/logs"

find_archive() {
    for candidate in \
        "$APP_DIR/$ARCHIVE_NAME" \
        "/root/apps/$ARCHIVE_NAME" \
        "/root/$ARCHIVE_NAME"
    do
        if [ -f "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

mkdir -p "$APP_DIR" "$LOG_DIR" "$APP_DIR/data/cache" \
    "$APP_DIR/data/daily_scans" "$APP_DIR/data/fundamentals_cache"

ARCHIVE_PATH="$(find_archive || true)"
if [ -n "$ARCHIVE_PATH" ]; then
    echo "Deploying package: $ARCHIVE_PATH"
    tar -xzf "$ARCHIVE_PATH" -C "$APP_DIR"
else
    echo "No new $ARCHIVE_NAME found; rebuilding current application files"
fi

cd "$APP_DIR"

if [ ! -f .env ]; then
    echo "Missing $APP_DIR/.env" >&2
    exit 1
fi
chmod 600 .env

# Production deployments are package-based. Git metadata and credentials are
# deliberately not retained on the VPS.
rm -rf .git
rm -f /root/.ssh/stock_screener_github /root/.ssh/stock_screener_github.pub

chmod +x scripts/*.sh 2>/dev/null || true

docker compose -f deploy/docker-compose.yml build
docker compose -f deploy/docker-compose.yml up -d

cp deploy/nginx-stock-min.conf /root/infrastructure/nginx/conf.d/stock-min.conf
docker exec nginx-proxy nginx -t
docker exec nginx-proxy nginx -s reload

if [ -n "$ARCHIVE_PATH" ]; then
    rm -f "$ARCHIVE_PATH"
fi

docker image prune -f
docker compose -f deploy/docker-compose.yml ps
