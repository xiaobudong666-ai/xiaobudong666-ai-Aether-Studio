#!/usr/bin/env bash
set -euo pipefail

# Private Provider canary controller. The default action is read-only preflight.
# arm/run remain fail-closed until a separate execution approval is supplied.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPOSITORY_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)"
BASE_COMPOSE="$SCRIPT_DIR/docker-compose.yml"
CANARY_COMPOSE="$SCRIPT_DIR/docker-compose.provider-canary.yml"
CHECKER="$SCRIPT_DIR/provider-canary-smoke.py"
CANARY_PROFILE="private-one-task-v1"

command_name="preflight"
approved_sha="${AETHER_CANARY_APPROVED_SHA:-}"
if [[ $# -gt 0 && "$1" != --* ]]; then
  command_name="$1"
  shift
fi
while [[ $# -gt 0 ]]; do
  case "$1" in
    --approved-sha)
      [[ $# -ge 2 ]] || { echo '{"status":"REJECTED","reasonCode":"APPROVED_SHA_MISSING"}'; exit 2; }
      approved_sha="$2"
      shift 2
      ;;
    *)
      echo '{"status":"REJECTED","reasonCode":"ARGUMENT_INVALID"}'
      exit 2
      ;;
  esac
done

reject() {
  printf '{"status":"REJECTED","reasonCode":"%s"}\n' "$1"
  exit 1
}

require_env() {
  local name="$1"
  [[ -n "${!name:-}" ]] || reject "${name}_MISSING"
}

require_absolute_external_file() {
  local value="$1"
  [[ "$value" == /* ]] || reject "LOCAL_FILE_NOT_ABSOLUTE"
  [[ ! -L "$value" && -f "$value" ]] || reject "LOCAL_FILE_INVALID"
  case "$value" in
    "$REPOSITORY_ROOT"|"$REPOSITORY_ROOT"/*) reject "LOCAL_FILE_INSIDE_WORKTREE" ;;
  esac
  python3 - "$value" <<'PY' || reject "LOCAL_FILE_PERMISSIONS_INVALID"
import os
import stat
import sys
from pathlib import Path

metadata = Path(sys.argv[1]).lstat()
assert stat.S_ISREG(metadata.st_mode)
assert stat.S_IMODE(metadata.st_mode) & 0o077 == 0
if hasattr(os, "geteuid"):
    assert metadata.st_uid == os.geteuid()
PY
}

preflight() {
  require_env MONEYPRINTER_CONFIG_FILE
  require_env AETHER_CANARY_ENV_FILE
  require_env AETHER_CANARY_LLM_PROVIDER
  require_env AETHER_CANARY_MODEL
  require_env AETHER_CANARY_MATERIAL_SOURCE
  require_env AETHER_CANARY_VOICE_PATH
  require_env AETHER_GENERATION_TENANT_ID
  require_env AETHER_GENERATION_CONFIG_VERSION_ID
  require_env AETHER_GENERATION_POLICY_HASH
  require_env AETHER_CANARY_PROVIDER_BUDGET_EVIDENCE
  require_env AETHER_CANARY_MATERIAL_LICENSE_EVIDENCE
  [[ -n "$approved_sha" ]] || reject "APPROVED_SHA_MISSING"
  require_absolute_external_file "$MONEYPRINTER_CONFIG_FILE"
  require_absolute_external_file "$AETHER_CANARY_ENV_FILE"

  python3 "$CHECKER" preflight \
    --repository-root "$REPOSITORY_ROOT" \
    --config-file "$MONEYPRINTER_CONFIG_FILE" \
    --approved-sha "$approved_sha" \
    --llm-provider "$AETHER_CANARY_LLM_PROVIDER" \
    --model "$AETHER_CANARY_MODEL" \
    --material-source "$AETHER_CANARY_MATERIAL_SOURCE" \
    --voice-path "$AETHER_CANARY_VOICE_PATH" \
    --tenant-id "$AETHER_GENERATION_TENANT_ID" \
    --config-version-id "$AETHER_GENERATION_CONFIG_VERSION_ID" \
    --policy-hash "$AETHER_GENERATION_POLICY_HASH" \
    --profile "$CANARY_PROFILE" \
    --provider-budget-evidence "$AETHER_CANARY_PROVIDER_BUDGET_EVIDENCE" \
    --material-license-evidence "$AETHER_CANARY_MATERIAL_LICENSE_EVIDENCE" \
    --concurrent-limit "${AETHER_CANARY_CONCURRENT_LIMIT:-1}" \
    --request-limit "${AETHER_CANARY_REQUEST_LIMIT:-1}" \
    --generated-seconds-limit "${AETHER_CANARY_GENERATED_SECONDS_LIMIT:-10}" \
    --output-limit "${AETHER_CANARY_OUTPUT_LIMIT:-1}" \
    --artifact-path-prefix "${AETHER_CANARY_ARTIFACT_PATH_PREFIX:-/tasks/}"

  MONEYPRINTER_CONFIG_FILE="$MONEYPRINTER_CONFIG_FILE" docker compose \
    --env-file "$AETHER_CANARY_ENV_FILE" \
    -f "$BASE_COMPOSE" -f "$CANARY_COMPOSE" config --quiet
}

require_execution_approval() {
  [[ "${AETHER_CANARY_REAL_EXECUTION_APPROVED:-false}" == "true" ]] || reject "REAL_EXECUTION_NOT_APPROVED"
  [[ "${AETHER_CANARY_OWNER_CONFIRMED:-false}" == "true" ]] || reject "OWNER_CONFIRMATION_MISSING"
  [[ "${AETHER_CANARY_TARGET_PRIVATE:-false}" == "true" ]] || reject "TARGET_NOT_PRIVATE"
  require_env AETHER_CANARY_APPROVAL_ID
  [[ "$AETHER_CANARY_APPROVAL_ID" =~ ^[A-Za-z0-9._-]{1,128}$ ]] || reject "APPROVAL_ID_INVALID"
  require_env AETHER_CANARY_OWNER_UID
  [[ "$AETHER_CANARY_OWNER_UID" =~ ^[0-9]+$ && "$(id -u)" == "$AETHER_CANARY_OWNER_UID" ]] || reject "OWNER_IDENTITY_MISMATCH"
  require_env AETHER_CANARY_API_BASE_URL
  [[ "$AETHER_CANARY_API_BASE_URL" =~ ^https?://[^/@?#]+(:[0-9]+)?$ ]] || reject "API_BASE_URL_INVALID"
  require_env AETHER_CANARY_OWNER_COOKIE_FILE
  require_env AETHER_CANARY_STATE_FILE
  require_absolute_external_file "$AETHER_CANARY_OWNER_COOKIE_FILE"
  [[ "$AETHER_CANARY_STATE_FILE" == /* ]] || reject "STATE_FILE_NOT_ABSOLUTE"
  case "$AETHER_CANARY_STATE_FILE" in
    "$REPOSITORY_ROOT"|"$REPOSITORY_ROOT"/*) reject "STATE_FILE_INSIDE_WORKTREE" ;;
  esac
}

api_request() {
  local method="$1"
  local endpoint="$2"
  local output_file="$3"
  local data_file="${4:-}"
  local idempotency_key="${5:-}"
  local args=(
    --fail-with-body --silent --show-error --max-time 30 --request "$method"
    --cookie "$AETHER_CANARY_OWNER_COOKIE_FILE"
    --header 'X-Aether-CSRF: 1'
    --header 'Content-Type: application/json'
    --output "$output_file"
  )
  [[ -z "$data_file" ]] || args+=(--data-binary "@$data_file")
  [[ -z "$idempotency_key" ]] || args+=(--header "Idempotency-Key: $idempotency_key")
  curl "${args[@]}" "${AETHER_CANARY_API_BASE_URL%/}$endpoint"
}

write_state() {
  local status="$1"
  local one_post_sent="${2:-false}"
  local task_id="${3:-}"
  umask 077
  python3 - "$AETHER_CANARY_STATE_FILE" "$approved_sha" "$AETHER_CANARY_APPROVAL_ID" "$status" "$one_post_sent" "$task_id" <<'PY'
import json
import os
import sys
from pathlib import Path

target = Path(sys.argv[1])
payload = {
    "approvedSha": sys.argv[2],
    "approvalId": sys.argv[3],
    "canaryProfile": "private-one-task-v1",
    "status": sys.argv[4],
    "onePostSent": sys.argv[5] == "true",
}
if sys.argv[6]:
    payload["taskId"] = sys.argv[6]
temporary = target.with_suffix(target.suffix + ".tmp")
temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
os.chmod(temporary, 0o600)
os.replace(temporary, target)
PY
}

read_state_field() {
  local field="$1"
  python3 - "$AETHER_CANARY_STATE_FILE" "$field" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
value = payload.get(sys.argv[2], "")
if not isinstance(value, (str, bool)):
    raise SystemExit(1)
print(str(value).lower() if isinstance(value, bool) else value)
PY
}

kill_switch_is_disabled() {
  local response_file="$1"
  python3 - "$response_file" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
raise SystemExit(0 if payload.get("killSwitch", {}).get("disabled") is True else 1)
PY
}

set_kill_switch() {
  local disabled="$1"
  local reason="$2"
  local request_file response_file
  request_file="$(mktemp)"
  response_file="$(mktemp)"
  chmod 600 "$request_file" "$response_file"
  printf '{"disabled":%s,"reasonCode":"%s"}\n' "$disabled" "$reason" > "$request_file"
  if ! api_request POST '/generation/providers/moneyprinter/kill-switch' "$response_file" "$request_file"; then
    rm -f -- "$request_file" "$response_file"
    return 1
  fi
  rm -f -- "$request_file" "$response_file"
}

disarm_runtime() {
  local failure=0
  set_kill_switch true CANARY_DISARMED || failure=1
  export AETHER_GENERATION_PROVIDER_MODE=disabled
  export AETHER_GENERATION_CREDENTIAL_STATE=ABSENT
  export AETHER_GENERATION_NETWORK_ISOLATION=NOT_ENFORCED
  export AETHER_GENERATION_CANARY_PROFILE=disabled
  docker compose --env-file "$AETHER_CANARY_ENV_FILE" -f "$BASE_COMPOSE" \
    stop moneyprinter-sidecar worker >/dev/null || failure=1
  docker compose --env-file "$AETHER_CANARY_ENV_FILE" -f "$BASE_COMPOSE" \
    rm -f moneyprinter-sidecar worker >/dev/null || failure=1
  docker compose --env-file "$AETHER_CANARY_ENV_FILE" -f "$BASE_COMPOSE" \
    up -d --no-deps worker >/dev/null || failure=1
  write_state DISABLED "$(read_state_field onePostSent 2>/dev/null || printf false)" \
    "$(read_state_field taskId 2>/dev/null || true)"
  python3 "$CHECKER" scan-evidence --evidence-file "$AETHER_CANARY_STATE_FILE" >/dev/null || failure=1
  return "$failure"
}

arm() {
  require_execution_approval
  preflight >/dev/null
  local readiness_file
  readiness_file="$(mktemp)"
  chmod 600 "$readiness_file"
  api_request GET '/generation/providers/moneyprinter/readiness' "$readiness_file"
  if ! kill_switch_is_disabled "$readiness_file"; then
    rm -f -- "$readiness_file"
    reject "KILL_SWITCH_NOT_DISABLED"
  fi
  rm -f -- "$readiness_file"
  write_state PREFLIGHTED false
  trap 'disarm_runtime >/dev/null 2>&1 || true' ERR INT TERM
  MONEYPRINTER_CONFIG_FILE="$MONEYPRINTER_CONFIG_FILE" docker compose \
    --env-file "$AETHER_CANARY_ENV_FILE" \
    -f "$BASE_COMPOSE" -f "$CANARY_COMPOSE" \
    up -d --no-deps --wait moneyprinter-sidecar worker >/dev/null
  set_kill_switch false CANARY_ARMED
  write_state ARMED false
  trap - ERR INT TERM
  echo '{"status":"ARMED","canaryProfile":"private-one-task-v1"}'
}

run_one_task() {
  require_execution_approval
  require_env AETHER_CANARY_PROJECT_ID
  require_env AETHER_CANARY_REQUEST_FILE
  require_env AETHER_CANARY_IDEMPOTENCY_KEY
  require_absolute_external_file "$AETHER_CANARY_REQUEST_FILE"
  [[ "$AETHER_CANARY_PROJECT_ID" =~ ^[A-Za-z0-9._-]{1,128}$ ]] || reject "PROJECT_ID_INVALID"
  [[ "${AETHER_CANARY_MAX_WALL_SECONDS:-900}" =~ ^[0-9]+$ ]] || reject "CANARY_WALL_BUDGET_INVALID"
  (( ${AETHER_CANARY_MAX_WALL_SECONDS:-900} >= 1 && ${AETHER_CANARY_MAX_WALL_SECONDS:-900} <= 900 )) || reject "CANARY_WALL_BUDGET_INVALID"
  [[ -f "$AETHER_CANARY_STATE_FILE" ]] || reject "CANARY_NOT_ARMED"
  [[ "$(read_state_field status)" == "ARMED" ]] || reject "CANARY_NOT_ARMED"
  [[ "$(read_state_field approvedSha)" == "$approved_sha" ]] || reject "APPROVED_SHA_MISMATCH"
  [[ "$(read_state_field onePostSent)" == "false" ]] || reject "CANARY_POST_ALREADY_SENT"
  python3 "$CHECKER" validate-request \
    --request-file "$AETHER_CANARY_REQUEST_FILE" \
    --idempotency-key "$AETHER_CANARY_IDEMPOTENCY_KEY" >/dev/null

  local create_response status_response task_id status deadline
  create_response="$(mktemp)"
  status_response="$(mktemp)"
  chmod 600 "$create_response" "$status_response"
  trap 'write_state UNKNOWN true 2>/dev/null || true; disarm_runtime >/dev/null 2>&1 || true; rm -f -- "$create_response" "$status_response"' ERR INT TERM
  # Persist the one-POST boundary before transmitting. A timeout is UNKNOWN and
  # is never automatically replayed.
  write_state ONE_TASK_RUNNING true
  if ! api_request POST "/projects/$AETHER_CANARY_PROJECT_ID/generation-tasks" \
    "$create_response" "$AETHER_CANARY_REQUEST_FILE" "$AETHER_CANARY_IDEMPOTENCY_KEY"; then
    write_state UNKNOWN true
    disarm_runtime || true
    rm -f -- "$create_response" "$status_response"
    trap - ERR INT TERM
    reject "CANARY_SUBMISSION_UNKNOWN"
  fi
  task_id="$(python3 "$CHECKER" extract-task-field --response-file "$create_response" --field taskId)"
  write_state ONE_TASK_RUNNING true "$task_id"
  deadline=$((SECONDS + ${AETHER_CANARY_MAX_WALL_SECONDS:-900}))
  status="RUNNING"
  while (( SECONDS < deadline )); do
    api_request GET "/projects/$AETHER_CANARY_PROJECT_ID/generation-tasks/$task_id" "$status_response"
    status="$(python3 "$CHECKER" extract-task-field --response-file "$status_response" --field status)"
    case "$status" in
      SUCCEEDED|FAILED|UNKNOWN|CANCELED|PARTIAL) break ;;
    esac
    sleep 2
  done
  [[ "$status" != "RUNNING" && "$status" != "QUEUED" && "$status" != "SUBMITTING" && "$status" != "INGESTING" ]] || status="UNKNOWN"
  write_state "$status" true "$task_id"
  disarm_runtime
  rm -f -- "$create_response" "$status_response"
  trap - ERR INT TERM
  printf '{"status":"%s","taskId":"%s","disarmed":true}\n' "$status" "$task_id"
}

case "$command_name" in
  preflight) preflight ;;
  arm) arm ;;
  run) run_one_task ;;
  disarm)
    require_execution_approval
    disarm_runtime
    echo '{"status":"DISABLED","disarmed":true}'
    ;;
  *) reject "COMMAND_INVALID" ;;
esac
