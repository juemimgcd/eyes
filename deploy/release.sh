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
"${compose[@]}" up -d --wait postgres redis rabbitmq
# Existing databases are backed up before schema changes. Fresh installs have no current link.
if [[ -L /opt/eyes/current ]]; then
    mkdir -p /opt/eyes/backups
    "${compose[@]}" exec -T postgres pg_dump -U eyes -d eyes -Fc > "/opt/eyes/backups/$(date -u +%Y%m%dT%H%M%SZ).dump"
fi
"${compose[@]}" run --rm --no-deps migrate
"${compose[@]}" up -d --no-deps --wait --wait-timeout 180 api scheduler publisher result-consumer frontend gateway tunnel-runner
curl --fail --silent --show-error http://127.0.0.1:8180/api/health/ready
ln -sfn "$PWD" /opt/eyes/current
printf '\nDeployed %s\n' "$PWD"
