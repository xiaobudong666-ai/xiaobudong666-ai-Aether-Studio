#!/usr/bin/env bash
set -euo pipefail

COMMAND="${1:-preflight}"
shift || true
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BASE_COMPOSE="$ROOT/infra/docker/docker-compose.yml"
CANARY_COMPOSE="$ROOT/infra/docker/docker-compose.provider-canary.yml"
SMOKE="$ROOT/infra/docker/provider-canary-smoke.py"
STATE_DIR="${AETHER_CANARY_STATE_DIR:-/tmp/aether-provider-canary-state}"
STATE_FILE="$STATE_DIR/state.json"
PREFLIGHT_FILE="$STATE_DIR/preflight-public.json"
EVIDENCE_FILE="$STATE_DIR/evidence.jsonl"
API_URL="${AETHER_CANARY_API_URL:-http://127.0.0.1:8000}"
SYNTHETIC_SUBJECT="Aether synthetic canary: geometric shapes on a neutral background"
CANARY_PROFILE="private-single-task-v1"
READINESS_WAIT_SECONDS="${AETHER_CANARY_READINESS_WAIT_SECONDS:-120}"

mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"

emit() { printf '%s\n' "$1"; }
block() { printf '{"status":"blocked","reasonCode":"%s"}\n' "$1" >&2; exit 2; }
require_env() { local name="$1"; [[ -n "${!name:-}" ]] || block "${name}_REQUIRED"; }

compose() {
  docker compose -f "$BASE_COMPOSE" -f "$CANARY_COMPOSE" "$@"
}

check_git_gate() {
  require_env AETHER_CANARY_APPROVED_SHA
  local head
  head="$(git -C "$ROOT" rev-parse HEAD)"
  [[ "$head" == "$AETHER_CANARY_APPROVED_SHA" ]] || block "APPROVED_SHA_MISMATCH"
  git -C "$ROOT" diff --quiet --ignore-submodules -- || block "WORKTREE_DIRTY"
  git -C "$ROOT" diff --cached --quiet --ignore-submodules -- || block "WORKTREE_DIRTY"
  [[ -z "$(git -C "$ROOT" status --porcelain --untracked-files=normal)" ]] || block "WORKTREE_DIRTY"
}

check_execution_gate() {
  [[ "${AETHER_PROVIDER_CANARY_REAL_EXECUTION_APPROVED:-}" == "YES" ]] || block "REAL_EXECUTION_NOT_APPROVED"
  [[ "${AETHER_CANARY_OWNER_APPROVAL:-}" == "YES" ]] || block "OWNER_APPROVAL_MISSING"
  [[ "${AETHER_CANARY_TARGET_CLASS:-}" == "private" ]] || block "PRIVATE_TARGET_REQUIRED"
  require_env AETHER_CANARY_APPROVAL_ID
  check_git_gate
}

check_public_evidence() {
  require_env AETHER_CANARY_LIMIT_EVIDENCE_FILE
  require_env AETHER_CANARY_LICENSE_EVIDENCE_FILE
  [[ -f "$AETHER_CANARY_LIMIT_EVIDENCE_FILE" && ! -L "$AETHER_CANARY_LIMIT_EVIDENCE_FILE" ]] || block "LIMIT_EVIDENCE_MISSING"
  [[ -f "$AETHER_CANARY_LICENSE_EVIDENCE_FILE" && ! -L "$AETHER_CANARY_LICENSE_EVIDENCE_FILE" ]] || block "LICENSE_EVIDENCE_MISSING"
}

preflight() {
  check_git_gate
  require_env MONEYPRINTER_CONFIG_FILE
  require_env AETHER_CANARY_POLICY_FILE
  require_env AETHER_GENERATION_TENANT_ID
  require_env AETHER_GENERATION_CONFIG_VERSION_ID
  require_env AETHER_GENERATION_POLICY_HASH
  require_env AETHER_CANARY_PROVIDER
  require_env AETHER_CANARY_MODEL
  require_env AETHER_CANARY_MATERIAL_SOURCE
  require_env AETHER_CANARY_VOICE_PATH
  check_public_evidence
  python "$SMOKE" preflight \
    --config "$MONEYPRINTER_CONFIG_FILE" \
    --repo-root "$ROOT" \
    --policy-file "$AETHER_CANARY_POLICY_FILE" \
    --tenant-id "$AETHER_GENERATION_TENANT_ID" \
    --config-version-id "$AETHER_GENERATION_CONFIG_VERSION_ID" \
    --policy-hash "$AETHER_GENERATION_POLICY_HASH" \
    --provider "$AETHER_CANARY_PROVIDER" \
    --model "$AETHER_CANARY_MODEL" \
    --material-source "$AETHER_CANARY_MATERIAL_SOURCE" \
    --voice-path "$AETHER_CANARY_VOICE_PATH" \
    --log-level WARNING >"$PREFLIGHT_FILE"
  chmod 600 "$PREFLIGHT_FILE"
  AETHER_GENERATION_PROVIDER_MODE=disabled \
  AETHER_GENERATION_CREDENTIAL_STATE=PRESENT \
  AETHER_GENERATION_NETWORK_ISOLATION=ENFORCED \
  AETHER_GENERATION_CANARY_PROFILE="$CANARY_PROFILE" \
  compose config --quiet
  cat "$PREFLIGHT_FILE"
}

owner_api_post() {
  require_env AETHER_CANARY_OWNER_COOKIE_FILE
  [[ -f "$AETHER_CANARY_OWNER_COOKIE_FILE" && ! -L "$AETHER_CANARY_OWNER_COOKIE_FILE" ]] || block "OWNER_SESSION_MISSING"
  local path="$1" body="$2"
  curl --fail --silent --show-error \
    --cookie "$AETHER_CANARY_OWNER_COOKIE_FILE" \
    -H 'Content-Type: application/json' -H 'X-Aether-CSRF: 1' \
    -X POST "$API_URL$path" --data "$body" >/dev/null
}

readiness_json() {
  require_env AETHER_CANARY_OWNER_COOKIE_FILE
  [[ -f "$AETHER_CANARY_OWNER_COOKIE_FILE" && ! -L "$AETHER_CANARY_OWNER_COOKIE_FILE" ]] || block "OWNER_SESSION_MISSING"
  curl --fail --silent --show-error \
    --cookie "$AETHER_CANARY_OWNER_COOKIE_FILE" \
    "$API_URL/generation/providers/moneyprinter/readiness"
}

verify_kill_switch_disabled() {
  local body
  body="$(readiness_json)"
  python - "$body" <<'PY'
import json, sys
obj=json.loads(sys.argv[1])
ks=obj.get('killSwitch') or {}
if not ks.get('disabled'):
    raise SystemExit(2)
PY
}

wait_for_worker_proof() {
  local deadline body
  deadline=$((SECONDS + READINESS_WAIT_SECONDS))
  while (( SECONDS < deadline )); do
    if body="$(readiness_json 2>/dev/null)" && \
      AETHER_EXPECTED_CONFIG_VERSION_ID="$AETHER_GENERATION_CONFIG_VERSION_ID" \
      AETHER_EXPECTED_POLICY_HASH="$AETHER_GENERATION_POLICY_HASH" \
      python - "$body" <<'PY'
import json, os, sys
obj=json.loads(sys.argv[1])
proof=obj.get('workerProof') or {}
ks=obj.get('killSwitch') or {}
ok=(
    obj.get('operatorMode') == 'moneyprinter'
    and obj.get('configVersionId') == os.environ['AETHER_EXPECTED_CONFIG_VERSION_ID']
    and obj.get('policyHash') == os.environ['AETHER_EXPECTED_POLICY_HASH']
    and ks.get('disabled') is True
    and proof.get('present') is True
    and proof.get('fresh') is True
    and proof.get('adapterVersion') == 'aether-moneyprinter-v2'
    and proof.get('upstreamPin') == '475f21147f0808f5ffe3f58af9ab794b28a4da2c'
)
raise SystemExit(0 if ok else 1)
PY
    then
      return 0
    fi
    sleep 2
  done
  return 1
}

wait_for_enabled_readiness() {
  local deadline body
  deadline=$((SECONDS + READINESS_WAIT_SECONDS))
  while (( SECONDS < deadline )); do
    if body="$(readiness_json 2>/dev/null)" && \
      AETHER_EXPECTED_CONFIG_VERSION_ID="$AETHER_GENERATION_CONFIG_VERSION_ID" \
      AETHER_EXPECTED_POLICY_HASH="$AETHER_GENERATION_POLICY_HASH" \
      python - "$body" <<'PY'
import json, os, sys
obj=json.loads(sys.argv[1])
proof=obj.get('workerProof') or {}
ks=obj.get('killSwitch') or {}
ok=(
    obj.get('enabled') is True
    and obj.get('operatorMode') == 'moneyprinter'
    and obj.get('configVersionId') == os.environ['AETHER_EXPECTED_CONFIG_VERSION_ID']
    and obj.get('policyHash') == os.environ['AETHER_EXPECTED_POLICY_HASH']
    and ks.get('disabled') is False
    and proof.get('present') is True
    and proof.get('fresh') is True
)
raise SystemExit(0 if ok else 1)
PY
    then
      return 0
    fi
    sleep 2
  done
  return 1
}

write_state() {
  local status="$1"
  AETHER_STATE_STATUS="$status" \
  AETHER_STATE_PROFILE="$CANARY_PROFILE" \
  python - "$STATE_FILE" <<'PY'
import json, os, sys
payload={
    "state": os.environ["AETHER_STATE_STATUS"],
    "canaryProfile": os.environ["AETHER_STATE_PROFILE"],
    "approvedSha": os.environ.get("AETHER_CANARY_APPROVED_SHA"),
    "approvalId": os.environ.get("AETHER_CANARY_APPROVAL_ID"),
    "tenantId": os.environ.get("AETHER_GENERATION_TENANT_ID"),
    "configVersionId": os.environ.get("AETHER_GENERATION_CONFIG_VERSION_ID"),
    "policyHash": os.environ.get("AETHER_GENERATION_POLICY_HASH"),
}
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
    handle.write("\n")
PY
  chmod 600 "$STATE_FILE"
}

verify_armed_state() {
  [[ -f "$STATE_FILE" && ! -L "$STATE_FILE" ]] || block "CANARY_NOT_ARMED"
  AETHER_EXPECTED_PROFILE="$CANARY_PROFILE" python - "$STATE_FILE" <<'PY'
import json, os, sys
obj=json.load(open(sys.argv[1], encoding="utf-8"))
checks={
    "state":"ARMED",
    "canaryProfile":os.environ["AETHER_EXPECTED_PROFILE"],
    "approvedSha":os.environ.get("AETHER_CANARY_APPROVED_SHA"),
    "approvalId":os.environ.get("AETHER_CANARY_APPROVAL_ID"),
    "tenantId":os.environ.get("AETHER_GENERATION_TENANT_ID"),
    "configVersionId":os.environ.get("AETHER_GENERATION_CONFIG_VERSION_ID"),
    "policyHash":os.environ.get("AETHER_GENERATION_POLICY_HASH"),
}
if any(obj.get(k) != v for k, v in checks.items()):
    raise SystemExit(2)
PY
}

record_public_evidence() {
  local event="$1" payload="${2:-{}}"
  AETHER_EVIDENCE_EVENT="$event" \
  AETHER_EVIDENCE_PAYLOAD="$payload" \
  AETHER_EVIDENCE_PROFILE="$CANARY_PROFILE" \
  python - "$EVIDENCE_FILE" <<'PY'
import datetime, json, os, re, sys
payload=json.loads(os.environ["AETHER_EVIDENCE_PAYLOAD"])
record={
    "event":os.environ["AETHER_EVIDENCE_EVENT"],
    "timestamp":datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00","Z"),
    "mainSha":os.environ.get("AETHER_CANARY_APPROVED_SHA"),
    "approvalId":os.environ.get("AETHER_CANARY_APPROVAL_ID"),
    "canaryProfile":os.environ["AETHER_EVIDENCE_PROFILE"],
    "payload":payload,
}
serialized=json.dumps(record, ensure_ascii=False, sort_keys=True).lower()
for marker in ("api_key","apikey","token","secret","password","cookie","authorization","config_path","config_file","mtime"):
    if marker in serialized:
        raise SystemExit(2)
if re.search(r"\b(bearer\s+[a-z0-9._-]{8,}|sk-[a-z0-9_-]{8,})\b", serialized):
    raise SystemExit(2)
with open(sys.argv[1], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
PY
  chmod 600 "$EVIDENCE_FILE"
}

fail_closed_disarm() {
  set +e
  if [[ -n "${AETHER_CANARY_OWNER_COOKIE_FILE:-}" && -f "${AETHER_CANARY_OWNER_COOKIE_FILE:-}" ]]; then
    owner_api_post "/generation/providers/moneyprinter/kill-switch" '{"disabled":true,"reasonCode":"CANARY_FAIL_CLOSED"}' >/dev/null 2>&1
  fi
  AETHER_GENERATION_PROVIDER_MODE=disabled compose down --remove-orphans >/dev/null 2>&1
  rm -f "$PREFLIGHT_FILE"
  write_state "DISARMED"
  if [[ -n "${AETHER_CANARY_APPROVAL_ID:-}" ]]; then
    record_public_evidence "DISARMED" '{"killSwitch":"requested-disabled","operatorMode":"disabled","mountState":"removed"}' >/dev/null 2>&1
  fi
  set -e
}

arm() {
  check_execution_gate
  preflight >/dev/null
  verify_kill_switch_disabled || block "CANARY_PREARM_KILL_SWITCH_DISABLED"
  trap 'fail_closed_disarm' EXIT ERR INT TERM
  record_public_evidence "PREFLIGHTED" "$(cat "$PREFLIGHT_FILE")"
  AETHER_GENERATION_PROVIDER_MODE=moneyprinter \
  AETHER_GENERATION_CREDENTIAL_STATE=PRESENT \
  AETHER_GENERATION_NETWORK_ISOLATION=ENFORCED \
  AETHER_GENERATION_CANARY_PROFILE="$CANARY_PROFILE" \
  compose up -d --wait --wait-timeout 180 moneyprinter-sidecar api worker
  wait_for_worker_proof || block "WORKER_PROOF_NOT_READY"
  owner_api_post "/generation/providers/moneyprinter/kill-switch" '{"disabled":false,"reasonCode":"PRIVATE_CANARY_ARM"}'
  wait_for_enabled_readiness || block "READINESS_NOT_ENABLED_AFTER_ARM"
  write_state "ARMED"
  record_public_evidence "ARMED" "$(readiness_json)"
  trap - EXIT ERR INT TERM
  emit '{"status":"armed","canaryProfile":"private-single-task-v1"}'
}

run_canary() {
  check_execution_gate
  verify_armed_state || block "CANARY_STATE_MISMATCH"
  require_env AETHER_CANARY_PROJECT_ID
  require_env AETHER_CANARY_IDEMPOTENCY_KEY
  require_env AETHER_CANARY_VOICE_NAME
  require_env AETHER_CANARY_OWNER_COOKIE_FILE
  local duration="${AETHER_CANARY_DURATION_SECONDS:-10}" result
  [[ "$duration" =~ ^[1-9]$|^10$ ]] || block "CANARY_DURATION_INVALID"
  trap 'fail_closed_disarm' EXIT ERR INT TERM
  result="$(python "$SMOKE" run-request \
    --api-url "$API_URL" \
    --cookie-file "$AETHER_CANARY_OWNER_COOKIE_FILE" \
    --project-id "$AETHER_CANARY_PROJECT_ID" \
    --config-version-id "$AETHER_GENERATION_CONFIG_VERSION_ID" \
    --policy-hash "$AETHER_GENERATION_POLICY_HASH" \
    --subject "$SYNTHETIC_SUBJECT" \
    --idempotency-key "$AETHER_CANARY_IDEMPOTENCY_KEY" \
    --voice-name "$AETHER_CANARY_VOICE_NAME" \
    --duration-seconds "$duration")"
  emit "$result"
  record_public_evidence "CANARY_TASK_TERMINAL" "$result"
  write_state "REQUESTED"
}

disarm() {
  fail_closed_disarm
  emit '{"status":"disarmed","canaryProfile":"private-single-task-v1"}'
}

case "$COMMAND" in
  preflight) preflight ;;
  arm) arm ;;
  run) run_canary ;;
  disarm) disarm ;;
  self-test) python "$SMOKE" self-test --repo-root "$ROOT" ;;
  *) block "UNKNOWN_COMMAND" ;;
esac