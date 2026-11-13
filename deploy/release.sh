#!/usr/bin/env bash
set -euo pipefail
# Called in /opt/eyes/releases/<commit>; secrets stay outside releases.
cd "$(dirname "$0")"
exec 9>/opt/eyes/deploy.lock
flock 9
compose=(docker compose --project-name eyes --env-file /opt/eyes/.env --env-file images.env -f compose.yaml -f compose.cloud.yaml -f compose.tunnel.yaml -f compose.tunnel.cloud.yaml)
"${compose[@]}" config --quiet
# CI imports the exact application images over SSH before invoking this script.
# Pull only missing images so imported releases do not depend on GHCR connectivity.
"${compose[@]}" pull --policy missing
"${compose[@]}" up -d --wait --wait-timeout 180 postgres redis rabbitmq
# A previous attempt may have migrated the database before creating current.
mkdir -p /opt/eyes/backups
partial=$(mktemp "/opt/eyes/backups/$(date -u +%Y%m%dT%H%M%SZ).partial.XXXXXX")
trap 'rm -f "$partial"' EXIT
"${compose[@]}" exec -T postgres pg_dump -U eyes -d eyes -Fc > "$partial"
backup="${partial/.partial./-}.dump"
mv -- "$partial" "$backup"
trap - EXIT
printf 'Database backup: %s\n' "$backup"
"${compose[@]}" run --rm --no-deps migrate
"${compose[@]}" up -d --no-deps --wait --wait-timeout 180 api scheduler publisher result-consumer frontend gateway tunnel-runner
curl --fail --silent --show-error --max-time 15 http://127.0.0.1:8180/api/health/ready
ln -sfn "$PWD" /opt/eyes/current.next
mv -Tf /opt/eyes/current.next /opt/eyes/current
printf '\nDeployed %s\n' "$PWD"
