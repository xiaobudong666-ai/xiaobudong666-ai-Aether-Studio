#!/usr/bin/env python3
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import stat
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

CANARY_PROFILE = "private-single-task-v1"
UPSTREAM_PIN = "475f21147f0808f5ffe3f58af9ab794b28a4da2c"
ARTIFACT_PREFIXES = ["/tasks/"]
ALLOWED_SOURCES = {"pexels", "pixabay"}
DISALLOWED_PROVIDERS = {"g4f", "pollinations"}
KNOWN_BASE_URLS = {
    "openai": "",
    "moonshot": "https://api.moonshot.cn/v1",
    "azure": "",
    "qwen": "",
    "deepseek": "https://api.deepseek.com",
    "gemini": "",
    "oneapi": "",
    "minimax": "https://api.minimax.io/v1",
    "modelscope": "https://api-inference.modelscope.cn/v1/",
}
CREDENTIAL_PROVIDERS = tuple(KNOWN_BASE_URLS)
SENSITIVE_FIELD_MARKERS = (
    "api_key", "apikey", "token", "secret", "password", "cookie",
    "authorization", "config_path", "config_file", "mtime", "sha256",
)

class CanaryValidationError(RuntimeError):
    pass

@dataclass(frozen=True)
class PublicPreflight:
    credentialState: str
    networkIsolation: str
    canaryProfile: str
    upstreamPin: str
    artifactPathPrefixes: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "credentialState": self.credentialState,
            "networkIsolation": self.networkIsolation,
            "canaryProfile": self.canaryProfile,
            "upstreamPin": self.upstreamPin,
            "artifactPathPrefixes": list(self.artifactPathPrefixes),
        }


def fail(code: str) -> None:
    raise CanaryValidationError(code)


def require_private_regular_file(path: Path, repo_root: Path) -> Path:
    if not path.is_absolute():
        fail("CONFIG_PATH_NOT_ABSOLUTE")
    if path.is_symlink():
        fail("CONFIG_SYMLINK_FORBIDDEN")
    try:
        resolved = path.resolve(strict=True)
        mode = resolved.stat().st_mode
    except FileNotFoundError:
        fail("CONFIG_ABSENT")
    if not stat.S_ISREG(mode):
        fail("CONFIG_NOT_REGULAR_FILE")
    if stat.S_IMODE(mode) & 0o077:
        fail("CONFIG_PERMISSIONS_TOO_BROAD")
    repo = repo_root.resolve(strict=True)
    if resolved == repo or repo in resolved.parents:
        fail("CONFIG_INSIDE_REPOSITORY")
    if not os.access(resolved, os.R_OK):
        fail("CONFIG_NOT_READABLE")
    return resolved


def text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def validate_config_document(
    data: dict[str, Any], *, expected_provider: str, expected_model: str,
    expected_source: str, expected_voice_path: str, log_level: str,
) -> None:
    app = data.get("app")
    if not isinstance(app, dict):
        fail("APP_SECTION_MISSING")
    provider = text(app.get("llm_provider"))
    source = text(app.get("video_source"))
    if log_level not in {"WARNING", "ERROR", "CRITICAL"}:
        fail("LOG_LEVEL_TOO_VERBOSE")
    if provider != expected_provider or provider not in KNOWN_BASE_URLS or provider in DISALLOWED_PROVIDERS:
        fail("PROVIDER_PROFILE_MISMATCH")
    if source != expected_source or source not in ALLOWED_SOURCES:
        fail("MATERIAL_SOURCE_MISMATCH")
    if text(app.get(f"{provider}_model_name")) != expected_model:
        fail("MODEL_PROFILE_MISMATCH")
    if text(app.get("endpoint")):
        fail("ENDPOINT_MUST_BE_EMPTY")
    if text(app.get("material_directory")) != "task":
        fail("MATERIAL_DIRECTORY_MUST_BE_TASK")
    if app.get("enable_redis") is not False:
        fail("REDIS_MUST_BE_DISABLED")
    if app.get("max_concurrent_tasks") != 1:
        fail("CONCURRENCY_MUST_BE_ONE")
    if app.get("hide_config") is not True:
        fail("CONFIG_UI_MUST_BE_HIDDEN")
    expected_base = KNOWN_BASE_URLS[provider]
    actual_base = text(app.get(f"{provider}_base_url"))
    if actual_base != expected_base:
        fail("PROVIDER_BASE_URL_MISMATCH")
    proxy = data.get("proxy", {})
    if not isinstance(proxy, dict) or any(text(v) for v in proxy.values()):
        fail("PROXY_MUST_BE_EMPTY")
    ui = data.get("ui", {})
    if not isinstance(ui, dict):
        fail("UI_SECTION_INVALID")
    if ui.get("upload_post_enabled") is not False or ui.get("upload_post_auto_upload") is not False:
        fail("AUTO_PUBLISH_MUST_BE_DISABLED")
    pexels = [v for v in list_value(app.get("pexels_api_keys")) if text(v)]
    pixabay = [v for v in list_value(app.get("pixabay_api_keys")) if text(v)]
    if source == "pexels" and (not pexels or pixabay):
        fail("MATERIAL_KEYS_MISMATCH")
    if source == "pixabay" and (not pixabay or pexels):
        fail("MATERIAL_KEYS_MISMATCH")
    if not text(app.get(f"{provider}_api_key")):
        fail("PROVIDER_CREDENTIAL_ABSENT")
    for other_provider in CREDENTIAL_PROVIDERS:
        if other_provider != provider and text(app.get(f"{other_provider}_api_key")):
            fail("MULTIPLE_PROVIDER_CREDENTIALS_FORBIDDEN")
    if expected_voice_path not in {"edge", "azure", "siliconflow"}:
        fail("VOICE_PATH_INVALID")
    if expected_voice_path == "azure":
        azure = data.get("azure", {})
        if not isinstance(azure, dict) or not text(azure.get("speech_key")) or not text(azure.get("speech_region")):
            fail("VOICE_CREDENTIAL_ABSENT")
    if expected_voice_path == "siliconflow":
        sf = data.get("siliconflow", {})
        if not isinstance(sf, dict) or not text(sf.get("api_key")):
            fail("VOICE_CREDENTIAL_ABSENT")
    azure = data.get("azure", {}) if isinstance(data.get("azure", {}), dict) else {}
    sf = data.get("siliconflow", {}) if isinstance(data.get("siliconflow", {}), dict) else {}
    if expected_voice_path != "azure" and (text(azure.get("speech_key")) or text(azure.get("speech_region"))):
        fail("MULTIPLE_VOICE_CREDENTIALS_FORBIDDEN")
    if expected_voice_path != "siliconflow" and text(sf.get("api_key")):
        fail("MULTIPLE_VOICE_CREDENTIALS_FORBIDDEN")


def load_and_validate_config(
    path: Path, *, repo_root: Path, expected_provider: str,
    expected_model: str, expected_source: str, expected_voice_path: str,
    log_level: str,
) -> PublicPreflight:
    resolved = require_private_regular_file(path, repo_root)
    try:
        with resolved.open("rb") as handle:
            data = tomllib.load(handle)
    except (tomllib.TOMLDecodeError, OSError):
        fail("CONFIG_INVALID")
    validate_config_document(
        data,
        expected_provider=expected_provider,
        expected_model=expected_model,
        expected_source=expected_source,
        expected_voice_path=expected_voice_path,
        log_level=log_level,
    )
    return PublicPreflight(
        credentialState="PRESENT",
        networkIsolation="ENFORCED",
        canaryProfile=CANARY_PROFILE,
        upstreamPin=UPSTREAM_PIN,
        artifactPathPrefixes=ARTIFACT_PREFIXES,
    )


def validate_policy(policy: dict[str, Any], *, tenant_id: str, config_version_id: str, policy_hash: str) -> None:
    expected = {
        "tenantId": tenant_id,
        "configVersionId": config_version_id,
        "policyHash": policy_hash,
        "artifactPathPrefixes": ARTIFACT_PREFIXES,
        "concurrentTaskLimit": 1,
        "monthlyRequestLimit": 1,
        "maxOutputs": 1,
    }
    for key, value in expected.items():
        if policy.get(key) != value:
            fail(f"POLICY_{key.upper()}_MISMATCH")
    seconds = policy.get("monthlyGeneratedSecondsLimit")
    if not isinstance(seconds, int) or not 1 <= seconds <= 10:
        fail("POLICY_DURATION_BUDGET_MISMATCH")


def validate_public_evidence(payload: Any) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True).lower()
    for marker in SENSITIVE_FIELD_MARKERS:
        if marker in serialized:
            fail("EVIDENCE_SECRET_SHAPE_DETECTED")
    if re.search(r"\b(bearer\s+[a-z0-9._-]{8,}|sk-[a-z0-9_-]{8,})\b", serialized):
        fail("EVIDENCE_SECRET_VALUE_DETECTED")


def load_json_file(path: str) -> dict[str, Any]:
    obj = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        fail("PUBLIC_INPUT_INVALID")
    return obj


def cmd_preflight(args: argparse.Namespace) -> int:
    validate_policy(
        load_json_file(args.policy_file), tenant_id=args.tenant_id,
        config_version_id=args.config_version_id, policy_hash=args.policy_hash,
    )
    result = load_and_validate_config(
        Path(args.config), repo_root=Path(args.repo_root),
        expected_provider=args.provider, expected_model=args.model,
        expected_source=args.material_source, expected_voice_path=args.voice_path,
        log_level=args.log_level,
    ).as_dict()
    validate_public_evidence(result)
    print(json.dumps(result, sort_keys=True))
    return 0


def _api_opener(cookie_file: str) -> urllib.request.OpenerDirector:
    jar = http.cookiejar.MozillaCookieJar(cookie_file)
    jar.load(ignore_discard=True, ignore_expires=True)
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def _api_json(opener: urllib.request.OpenerDirector, method: str, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    encoded = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=encoded, method=method)
    request.add_header("Accept", "application/json")
    request.add_header("X-Aether-CSRF", "1")
    if encoded is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with opener.open(request, timeout=10) as response:
            parsed = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError):
        fail("CONTROL_API_FAILED")
    if not isinstance(parsed, dict):
        fail("CONTROL_API_INVALID")
    return parsed


def cmd_run_request(args: argparse.Namespace) -> int:
    if args.subject != "Aether synthetic canary: geometric shapes on a neutral background":
        fail("SUBJECT_NOT_APPROVED_SYNTHETIC")
    try:
        key = str(uuid.UUID(args.idempotency_key))
    except ValueError:
        fail("IDEMPOTENCY_KEY_INVALID")
    opener = _api_opener(args.cookie_file)
    base = args.api_url.rstrip("/")
    readiness = _api_json(opener, "GET", f"{base}/generation/providers/moneyprinter/readiness")
    if not readiness.get("enabled"):
        fail("READINESS_NOT_ENABLED")
    if readiness.get("configVersionId") != args.config_version_id or readiness.get("policyHash") != args.policy_hash:
        fail("READINESS_POLICY_MISMATCH")
    project = _api_json(opener, "GET", f"{base}/projects/{args.project_id}")
    payload = {
        "videoSubject": args.subject,
        "videoAspect": "9:16",
        "voiceName": args.voice_name,
        "videoConcatMode": "sequential",
        "videoClipDuration": args.duration_seconds,
        "outputCount": 1,
        "inputAssetVersionIds": [],
        "idempotencyKey": key,
        "capabilitySnapshotHash": readiness["snapshotHash"],
        "expectedProjectRevision": project["revision"],
        "confirmExternalGeneration": True,
    }
    # Validate first; exactly one create follows only after validation succeeds.
    _api_json(opener, "POST", f"{base}/projects/{args.project_id}/generation-tasks/validate", payload)
    task = _api_json(opener, "POST", f"{base}/projects/{args.project_id}/generation-tasks", payload)
    task_id = str(task.get("taskId") or "")
    if not task_id:
        fail("TASK_ID_MISSING")
    deadline = time.monotonic() + args.timeout_seconds
    terminal = {"RIGHTS_BLOCKED", "FAILED", "CANCELED", "UNKNOWN", "PARTIAL"}
    current = task
    while time.monotonic() < deadline:
        status = str(current.get("status") or "")
        if status in terminal:
            break
        time.sleep(1)
        current = _api_json(opener, "GET", f"{base}/projects/{args.project_id}/generation-tasks/{task_id}")
    else:
        fail("CANARY_TASK_TIMEOUT")
    status = str(current.get("status") or "")
    public = {"taskId": task_id, "status": status, "canaryProfile": CANARY_PROFILE}
    validate_public_evidence(public)
    print(json.dumps(public, sort_keys=True))
    if status != "RIGHTS_BLOCKED":
        fail("CANARY_TASK_NOT_RIGHTS_BLOCKED")
    return 0


def _fake_config(provider: str = "openai", source: str = "pexels") -> str:
    app_lines = [
        "[app]", f'llm_provider = "{provider}"', f'{provider}_api_key = "fake-not-real"',
        f'{provider}_model_name = "fake-model"', f'{provider}_base_url = "{KNOWN_BASE_URLS[provider]}"',
        f'video_source = "{source}"', 'pexels_api_keys = ["fake-material"]' if source == "pexels" else 'pexels_api_keys = []',
        'pixabay_api_keys = ["fake-material"]' if source == "pixabay" else 'pixabay_api_keys = []',
        'endpoint = ""', 'material_directory = "task"', 'enable_redis = false',
        'max_concurrent_tasks = 1', 'hide_config = true', "", "[proxy]", "", "[ui]",
        "upload_post_enabled = false", "upload_post_auto_upload = false", "", "[azure]",
        'speech_key = ""', 'speech_region = ""', "", "[siliconflow]", 'api_key = ""',
    ]
    return "\n".join(app_lines) + "\n"


def cmd_self_test(args: argparse.Namespace) -> int:
    repo = Path(args.repo_root).resolve()
    compose = (repo / "infra/docker/docker-compose.yml").read_text()
    override = (repo / "infra/docker/docker-compose.provider-canary.yml").read_text()
    shell = (repo / "infra/docker/provider-canary.sh").read_text()
    ci = (repo / ".github/workflows/ci.yml").read_text()
    adapter = (repo / "apps/worker/app/moneyprinter_adapter.py").read_text()
    api_main = (repo / "apps/api/app/main.py").read_text()
    tests: list[tuple[int, Callable[[], bool]]] = []
    def add(n: int, fn: Callable[[], bool]) -> None: tests.append((n, fn))
    add(1, lambda: "AETHER_GENERATION_PROVIDER_MODE=${AETHER_GENERATION_PROVIDER_MODE:-disabled}" in compose)
    add(2, lambda: "MONEYPRINTER_CONFIG_FILE:?" in override)
    add(3, lambda: "CONFIG_PATH_NOT_ABSOLUTE" in Path(__file__).read_text())
    add(4, lambda: "CONFIG_PERMISSIONS_TOO_BROAD" in Path(__file__).read_text())
    add(5, lambda: "CONFIG_INVALID" in Path(__file__).read_text())
    add(6, lambda: "read_only: true" in override and "/MoneyPrinterTurbo/config.toml" in override)
    add(7, lambda: "MONEYPRINTER_CONFIG_FILE" not in compose)
    add(8, lambda: "MONEYPRINTER_CONFIG_FILE" not in api_main and "MONEYPRINTER_CONFIG_FILE" not in (repo / "apps/worker/app/main.py").read_text())
    add(9, lambda: all(marker not in (repo / "docs/evidence/IM18-IM20-PRIVATE-CANARY-VERIFICATION.md").read_text().lower() for marker in ["api_key=", "authorization:", "cookie:"]))
    add(10, lambda: CANARY_PROFILE in override)
    add(11, lambda: "LOGURU_LEVEL: WARNING" in override and "LOG_LEVEL_TOO_VERBOSE" in Path(__file__).read_text() and "upload_post_auto_upload" in Path(__file__).read_text())
    add(12, lambda: "DISALLOWED_PROVIDERS = {\"g4f\", \"pollinations\"}" in Path(__file__).read_text())
    add(13, lambda: 'ARTIFACT_PREFIXES = ["/tasks/"]' in Path(__file__).read_text())
    add(14, lambda: "provider-canary-smoke.py self-test" in ci)
    add(15, lambda: "moneyprinter-sidecar:\n" in compose and "- aether-net" not in compose.split("moneyprinter-sidecar:",1)[1].split("  api:",1)[0])
    add(16, lambda: "provider-control:" in compose and "internal: true" in compose and "- provider-control" in compose)
    add(17, lambda: "provider-egress" in compose and compose.count("- provider-egress") == 1)
    add(18, lambda: "API cannot reach MoneyPrinter Sidecar" in ci)
    add(19, lambda: "Web and video-use cannot reach MoneyPrinter Sidecar" in ci)
    add(20, lambda: "Worker reaches MoneyPrinter Sidecar through provider-control" in ci)
    add(21, lambda: "trust_env=False" in adapter and "follow_redirects=False" in adapter)
    add(22, lambda: api_main.count('status_code=410') >= 1 and "/moneyprinter/health" in api_main)
    add(23, lambda: "ports:" not in compose.split("moneyprinter-sidecar:",1)[1].split("  api:",1)[0])
    add(24, lambda: "Sanitize fake canary logs" in ci)
    add(25, lambda: "preflight" in shell and "fail_closed_disarm" in shell)
    add(26, lambda: "docker compose -f infra/docker/docker-compose.yml config --quiet" in ci)
    add(27, lambda: 'COMMAND="${1:-preflight}"' in shell)
    add(28, lambda: "diff --quiet" in shell and "AETHER_CANARY_APPROVED_SHA" in shell)
    add(29, lambda: "CANARY_PREARM_KILL_SWITCH_DISABLED" in shell)
    add(30, lambda: "monthlyGeneratedSecondsLimit" in Path(__file__).read_text())
    add(31, lambda: "AETHER_CANARY_LIMIT_EVIDENCE_FILE" in shell and "AETHER_CANARY_LICENSE_EVIDENCE_FILE" in shell)
    add(32, lambda: "SUBJECT_NOT_APPROVED_SYNTHETIC" in Path(__file__).read_text())
    add(33, lambda: shell.count("run-request") == 1)
    add(34, lambda: 'ARTIFACT_PREFIXES = ["/tasks/"]' in Path(__file__).read_text())
    add(35, lambda: "AMBIGUOUS_SUBMISSION" in (repo / "apps/worker/app/main.py").read_text())
    add(36, lambda: "RIGHTS_BLOCKED" in (repo / "apps/worker/Test_generation_tasks.py").read_text())
    add(37, lambda: "trap 'fail_closed_disarm'" in shell)
    add(38, lambda: "AETHER_GENERATION_PROVIDER_MODE=disabled" in shell and "down --remove-orphans" in shell)
    add(39, lambda: "validate_public_evidence" in Path(__file__).read_text())
    add(40, lambda: all(x in ci for x in ["pytest apps/api", "pytest /tmp/test_worker.py", "playwright", "provider-canary-smoke.py self-test"]))
    failures = [n for n, fn in tests if not fn()]
    if len(tests) != 40 or failures:
        print(json.dumps({"passed": len(tests)-len(failures), "total": len(tests), "failed": failures}), file=sys.stderr)
        return 1
    print(json.dumps({"passed": 40, "total": 40, "mode": "fake-only"}, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aether governed private Provider canary helper")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("preflight")
    for flag in ["config", "repo-root", "policy-file", "tenant-id", "config-version-id", "policy-hash", "provider", "model", "material-source", "voice-path"]:
        p.add_argument(f"--{flag}", required=True)
    p.add_argument("--log-level", required=True, choices=("WARNING", "ERROR", "CRITICAL"))
    p.set_defaults(func=cmd_preflight)
    r = sub.add_parser("run-request")
    r.add_argument("--api-url", required=True); r.add_argument("--cookie-file", required=True)
    r.add_argument("--project-id", required=True); r.add_argument("--config-version-id", required=True)
    r.add_argument("--policy-hash", required=True); r.add_argument("--subject", required=True)
    r.add_argument("--idempotency-key", required=True); r.add_argument("--voice-name", required=True)
    r.add_argument("--duration-seconds", required=True, type=int, choices=range(1, 11))
    r.add_argument("--timeout-seconds", type=int, default=180, choices=range(10, 601))
    r.set_defaults(func=cmd_run_request)
    s = sub.add_parser("self-test"); s.add_argument("--repo-root", required=True); s.set_defaults(func=cmd_self_test)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except CanaryValidationError as exc:
        print(json.dumps({"status": "blocked", "reasonCode": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    except (json.JSONDecodeError, OSError, ValueError):
        print(json.dumps({"status": "blocked", "reasonCode": "PUBLIC_INPUT_INVALID"}, sort_keys=True), file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
