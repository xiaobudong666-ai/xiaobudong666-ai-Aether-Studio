#!/bin/sh
set -eu

base_url="${AETHER_SMOKE_BASE_URL:-http://127.0.0.1:8088}"

curl --fail --show-error --silent "${base_url}/healthz" >/dev/null
curl --fail --show-error --silent "${base_url}/api/health" >/dev/null
curl --fail --show-error --silent "${base_url}/api/moneyprinter/health" >/dev/null
curl --fail --show-error --silent "${base_url}/api/moneyprinter/capabilities" >/dev/null
curl --fail --show-error --silent "${base_url}/api/video-use/health" >/dev/null
curl --fail --show-error --silent "${base_url}/api/video-use/capabilities" >/dev/null

printf '%s\n' "Aether production smoke checks passed at ${base_url}"
