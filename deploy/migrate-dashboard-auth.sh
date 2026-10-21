#!/bin/sh
set -eu

APP_DIR=/root/apps/stock-screener
ENV_FILE="$APP_DIR/.env"
CREDENTIAL_FILE="$APP_DIR/deploy/.initial_credentials"

if grep -q '^DASHBOARD_PASSWORD_HASH=.' "$ENV_FILE" 2>/dev/null \
    && grep -q '^FLASK_SECRET_KEY=.' "$ENV_FILE" 2>/dev/null; then
    echo "Dashboard application login is already configured"
    exit 0
fi

if [ ! -f "$CREDENTIAL_FILE" ]; then
    echo "Missing initial credential file: $CREDENTIAL_FILE" >&2
    exit 1
fi

credential="$(head -n 1 "$CREDENTIAL_FILE")"
username="${credential%%:*}"
password="${credential#*:}"

if [ -z "$username" ] || [ "$password" = "$credential" ] || [ -z "$password" ]; then
    echo "Invalid initial credential format; expected username:password" >&2
    exit 1
fi

password_hash="$(printf '%s' "$password" | docker exec -i stock-screener-dashboard \
    python -c 'import sys; from werkzeug.security import generate_password_hash; print(generate_password_hash(sys.stdin.read()))')"
secret_key="$(head -c 48 /dev/urandom | base64 | tr -d '\n')"

temp_file="$ENV_FILE.tmp"
grep -v -E '^(DASHBOARD_USERNAME|DASHBOARD_PASSWORD_HASH|FLASK_SECRET_KEY|SESSION_COOKIE_SECURE)=' "$ENV_FILE" > "$temp_file" || true
{
    printf '\nDASHBOARD_USERNAME=%s\n' "$username"
    printf 'DASHBOARD_PASSWORD_HASH=%s\n' "$password_hash"
    printf 'FLASK_SECRET_KEY=%s\n' "$secret_key"
    printf 'SESSION_COOKIE_SECURE=true\n'
} >> "$temp_file"

chmod 600 "$temp_file"
mv "$temp_file" "$ENV_FILE"
unset credential username password password_hash secret_key

echo "Dashboard application login migrated from the existing credentials"
