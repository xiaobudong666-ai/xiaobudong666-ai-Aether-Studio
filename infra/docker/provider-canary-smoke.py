#!/usr/bin/env python3
"""Fail-closed, secret-free checks for the private MoneyPrinter canary path.

The checker intentionally uses only the Python standard library.  It parses a
target-local TOML file in memory, but never returns values, paths, metadata or
digests from that file.  CI calls ``self-test`` with disposable fake TOML only.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import tomllib
import uuid
from pathlib import Path
from typing import Any


CANARY_PROFILE = "private-one-task-v1"
UPSTREAM_PIN = "475f21147f0808f5ffe3f58af9ab794b28a4da2c"
ALLOWED_LOG_LEVELS = {"WARNING", "ERROR", "CRITICAL"}
FORBIDDEN_PROVIDERS = {"g4f", "pollinations", "ollama", "oneapi"}
PROVIDER_FIELDS = {
    "openai": ("openai_api_key", "openai_model_name"),
    "moonshot": ("moonshot_api_key", "moonshot_model_name"),
    "qwen": ("qwen_api_key", "qwen_model_name"),
    "deepseek": ("deepseek_api_key", "deepseek_model_name"),
    "gemini": ("gemini_api_key", "gemini_model_name"),
    "minimax": ("minimax_api_key", "minimax_model_name"),
    "modelscope": ("modelscope_api_key", "modelscope_model_name"),
}
SECRET_FIELD_NAMES = {fields[0] for fields in PROVIDER_FIELDS.values()} | {
    "pollinations_api_key",
    "upload_post_api_key",
}
FORBIDDEN_EVIDENCE_KEYS = {
    "configpath",
    "configurationpath",
    "filepath",
    "mtime",
    "configlength",
    "configsha256",
    "apikey",
    "authorization",
    "cookie",
    "token",
    "password",
    "prompt",
    "rawrequest",
    "rawresponse",
    "providerurl",
}


class PreflightError(RuntimeError):
    """A stable, non-sensitive preflight rejection."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def reject(code: str) -> None:
    raise PreflightError(code)


def normalized_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def load_toml_safely(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            payload = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        reject("CONFIG_TOML_INVALID")
    if not isinstance(payload, dict):
        reject("CONFIG_TOML_INVALID")
    return payload


def check_target_file(path: Path, repository_root: Path) -> None:
    if not path.is_absolute():
        reject("CONFIG_PATH_NOT_ABSOLUTE")
    try:
        file_lstat = path.lstat()
    except OSError:
        reject("CONFIG_ABSENT")
    if stat.S_ISLNK(file_lstat.st_mode):
        reject("CONFIG_SYMLINK_REJECTED")
    if not stat.S_ISREG(file_lstat.st_mode):
        reject("CONFIG_NOT_REGULAR_FILE")
    try:
        resolved_path = path.resolve(strict=True)
        resolved_root = repository_root.resolve(strict=True)
        if os.path.commonpath((resolved_path, resolved_root)) == str(resolved_root):
            reject("CONFIG_INSIDE_WORKTREE")
    except (OSError, ValueError):
        reject("CONFIG_PATH_INVALID")
    if stat.S_IMODE(file_lstat.st_mode) & 0o077:
        reject("CONFIG_PERMISSIONS_TOO_BROAD")
    if hasattr(os, "geteuid") and file_lstat.st_uid != os.geteuid():
        reject("CONFIG_OWNER_MISMATCH")
    if not os.access(path, os.R_OK):
        reject("CONFIG_NOT_READABLE")


def require_empty(value: object, code: str) -> None:
    if value not in (None, "", [], {}):
        reject(code)


def validate_config(
    payload: dict[str, Any],
    *,
    llm_provider: str,
    model: str,
    material_source: str,
    voice_path: str,
) -> None:
    app = payload.get("app")
    if not isinstance(app, dict):
        reject("CONFIG_APP_SECTION_INVALID")
    if str(payload.get("log_level", "")).upper() not in ALLOWED_LOG_LEVELS:
        reject("CONFIG_LOG_LEVEL_UNSAFE")
    if app.get("hide_config") is not True:
        reject("CONFIG_UI_NOT_HIDDEN")
    if app.get("enable_redis") is not False:
        reject("CONFIG_EXTERNAL_QUEUE_FORBIDDEN")
    if app.get("endpoint") != "":
        reject("CONFIG_ENDPOINT_FORBIDDEN")
    if app.get("material_directory") != "task":
        reject("CONFIG_MATERIAL_DIRECTORY_INVALID")
    if app.get("max_concurrent_tasks") != 1:
        reject("CONFIG_CONCURRENCY_INVALID")
    if app.get("upload_post_enabled") is not False:
        reject("CONFIG_AUTO_PUBLISH_ENABLED")
    if app.get("upload_post_auto_upload") is not False:
        reject("CONFIG_AUTO_UPLOAD_ENABLED")

    selected = str(app.get("llm_provider", "")).strip().lower()
    expected_provider = llm_provider.strip().lower()
    if selected != expected_provider or selected in FORBIDDEN_PROVIDERS:
        reject("CONFIG_LLM_PROVIDER_INVALID")
    if selected not in PROVIDER_FIELDS:
        reject("CONFIG_LLM_PROVIDER_UNSUPPORTED")
    secret_field, model_field = PROVIDER_FIELDS[selected]
    if not isinstance(app.get(secret_field), str) or not app.get(secret_field):
        reject("CONFIG_SELECTED_PROVIDER_KEY_ABSENT")
    if app.get(model_field) != model or not model.strip():
        reject("CONFIG_MODEL_MISMATCH")
    for field in SECRET_FIELD_NAMES - {secret_field}:
        require_empty(app.get(field), "CONFIG_MULTIPLE_PROVIDER_KEYS")
    for key, value in app.items():
        if normalized_key(key).endswith("baseurl"):
            require_empty(value, "CONFIG_BASE_URL_FORBIDDEN")

    source = str(app.get("video_source", "")).strip().lower()
    expected_source = material_source.strip().lower()
    if expected_source not in {"pexels", "pixabay"} or source != expected_source:
        reject("CONFIG_MATERIAL_SOURCE_INVALID")
    selected_keys = app.get(f"{source}_api_keys")
    other_source = "pixabay" if source == "pexels" else "pexels"
    other_keys = app.get(f"{other_source}_api_keys")
    if not isinstance(selected_keys, list) or len(selected_keys) != 1:
        reject("CONFIG_MATERIAL_KEY_COUNT_INVALID")
    if not isinstance(selected_keys[0], str) or not selected_keys[0]:
        reject("CONFIG_MATERIAL_KEY_ABSENT")
    require_empty(other_keys, "CONFIG_MULTIPLE_MATERIAL_SOURCES")

    if voice_path != "edge" or app.get("subtitle_provider") != "edge":
        reject("CONFIG_VOICE_PATH_INVALID")
    for section in ("proxy", "azure", "siliconflow"):
        values = payload.get(section, {})
        if not isinstance(values, dict):
            reject("CONFIG_SECTION_INVALID")
        if any(value not in (None, "", [], {}) for value in values.values()):
            reject("CONFIG_PROXY_OR_EXTRA_CREDENTIAL_FORBIDDEN")


def validate_public_binding(args: argparse.Namespace) -> None:
    if args.profile != CANARY_PROFILE:
        reject("CANARY_PROFILE_MISMATCH")
    if not args.tenant_id or not args.config_version_id:
        reject("CANARY_BINDING_MISSING")
    if not re.fullmatch(r"[0-9a-f]{64}", args.policy_hash or ""):
        reject("CANARY_POLICY_HASH_INVALID")
    if args.provider_budget_evidence != "PRESENT":
        reject("PROVIDER_BUDGET_EVIDENCE_MISSING")
    if args.material_license_evidence != "PRESENT":
        reject("MATERIAL_LICENSE_EVIDENCE_MISSING")
    if (
        args.concurrent_limit != 1
        or args.request_limit != 1
        or not 1 <= args.generated_seconds_limit <= 10
        or args.output_limit != 1
        or args.artifact_path_prefix != "/tasks/"
    ):
        reject("CANARY_POLICY_BUDGET_INVALID")


def check_repository(repository_root: Path, approved_sha: str) -> None:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repository_root,
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repository_root,
            check=True, capture_output=True, text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        reject("REPOSITORY_STATE_UNAVAILABLE")
    if not re.fullmatch(r"[0-9a-f]{40}", approved_sha or "") or head != approved_sha:
        reject("APPROVED_SHA_MISMATCH")
    if dirty:
        reject("WORKTREE_NOT_CLEAN")


def validate_compose_boundary(repository_root: Path) -> None:
    base = (repository_root / "infra/docker/docker-compose.yml").read_text(encoding="utf-8")
    override = (
        repository_root / "infra/docker/docker-compose.provider-canary.yml"
    ).read_text(encoding="utf-8")
    required_base = (
        "provider-control:",
        "internal: true",
        "provider-egress:",
        "AETHER_GENERATION_PROVIDER_MODE=${AETHER_GENERATION_PROVIDER_MODE:-disabled}",
    )
    if any(marker not in base for marker in required_base):
        reject("COMPOSE_NETWORK_BOUNDARY_INVALID")
    sidecar_block = base.split("  moneyprinter-sidecar:", 1)[1].split("\n  api:", 1)[0]
    if "aether-net" in sidecar_block or "ports:" in sidecar_block:
        reject("COMPOSE_SIDECAR_EXPOSED")
    for marker in (
        "MONEYPRINTER_CONFIG_FILE:?",
        "target: /MoneyPrinterTurbo/config.toml",
        "read_only: true",
        "AETHER_GENERATION_CREDENTIAL_STATE=PRESENT",
        "AETHER_GENERATION_NETWORK_ISOLATION=ENFORCED",
        f"AETHER_GENERATION_CANARY_PROFILE={CANARY_PROFILE}",
    ):
        if marker not in override:
            reject("COMPOSE_OVERRIDE_INVALID")
    if any(marker in override for marker in ("ports:", "build:", "command:", "labels:")):
        reject("COMPOSE_OVERRIDE_SCOPE_INVALID")


def preflight(args: argparse.Namespace) -> dict[str, Any]:
    repository_root = Path(args.repository_root)
    config_path = Path(args.config_file)
    check_repository(repository_root, args.approved_sha)
    check_target_file(config_path, repository_root)
    validate_compose_boundary(repository_root)
    validate_config(
        load_toml_safely(config_path),
        llm_provider=args.llm_provider,
        model=args.model,
        material_source=args.material_source,
        voice_path=args.voice_path,
    )
    validate_public_binding(args)
    return {
        "status": "PREFLIGHTED",
        "credentialState": "PRESENT",
        "networkIsolation": "ENFORCED",
        "canaryProfile": CANARY_PROFILE,
        "approvedSha": args.approved_sha,
        "upstreamPin": UPSTREAM_PIN,
    }


def scan_evidence(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if normalized_key(key) in FORBIDDEN_EVIDENCE_KEYS:
                reject("EVIDENCE_FORBIDDEN_FIELD")
            scan_evidence(item)
    elif isinstance(value, list):
        for item in value:
            scan_evidence(item)
    elif isinstance(value, str):
        lowered = value.lower()
        if (
            "://" in lowered
            or "-----begin" in lowered
            or re.search(r"(?:api[_-]?key|authorization|cookie|token|password)\s*[:=]", lowered)
        ):
            reject("EVIDENCE_SECRET_SHAPE")


def validate_request_file(path: Path, expected_idempotency_key: str) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        reject("CANARY_REQUEST_INVALID")
    if not isinstance(payload, dict):
        reject("CANARY_REQUEST_INVALID")
    required = {
        "videoSubject",
        "videoAspect",
        "voiceName",
        "videoConcatMode",
        "videoClipDuration",
        "outputCount",
        "inputAssetVersionIds",
        "idempotencyKey",
        "capabilitySnapshotHash",
        "expectedProjectRevision",
        "confirmExternalGeneration",
    }
    if set(payload) != required:
        reject("CANARY_REQUEST_FIELDS_INVALID")
    try:
        request_key = str(uuid.UUID(str(payload["idempotencyKey"])))
        expected_key = str(uuid.UUID(expected_idempotency_key))
    except (ValueError, AttributeError):
        reject("CANARY_IDEMPOTENCY_KEY_INVALID")
    if request_key != expected_key:
        reject("CANARY_IDEMPOTENCY_KEY_MISMATCH")
    subject = payload["videoSubject"]
    if (
        not isinstance(subject, str)
        or not subject.strip()
        or len(subject) > 200
        or "://" in subject
        or "@" in subject
        or re.search(r"\b(?:person|face|voice clone|customer|client)\b", subject, re.I)
    ):
        reject("CANARY_SUBJECT_NOT_SYNTHETIC")
    if payload["videoAspect"] not in {"16:9", "9:16", "1:1"}:
        reject("CANARY_ASPECT_INVALID")
    if not isinstance(payload["voiceName"], str) or not payload["voiceName"].strip():
        reject("CANARY_VOICE_INVALID")
    if payload["videoConcatMode"] not in {"random", "sequential"}:
        reject("CANARY_CONCAT_MODE_INVALID")
    if not isinstance(payload["videoClipDuration"], int) or not 1 <= payload["videoClipDuration"] <= 10:
        reject("CANARY_DURATION_INVALID")
    if payload["outputCount"] != 1 or payload["inputAssetVersionIds"] != []:
        reject("CANARY_OUTPUT_OR_INPUT_INVALID")
    if not re.fullmatch(r"[0-9a-f]{64}", str(payload["capabilitySnapshotHash"])):
        reject("CANARY_SNAPSHOT_HASH_INVALID")
    if not isinstance(payload["expectedProjectRevision"], int) or payload["expectedProjectRevision"] < 0:
        reject("CANARY_PROJECT_REVISION_INVALID")
    if payload["confirmExternalGeneration"] is not True:
        reject("CANARY_CONFIRMATION_MISSING")


def extract_task_field(path: Path, field: str) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        reject("CANARY_RESPONSE_INVALID")
    if not isinstance(payload, dict):
        reject("CANARY_RESPONSE_INVALID")
    if field == "taskId":
        value = payload.get("taskId")
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value):
            reject("CANARY_TASK_ID_INVALID")
        return value
    value = payload.get("canonicalStatus", payload.get("status"))
    normalized = str(value or "").upper()
    allowed = {
        "QUEUED", "SUBMITTING", "RUNNING", "INGESTING",
        "SUCCEEDED", "FAILED", "UNKNOWN", "CANCELED", "PARTIAL",
    }
    if normalized not in allowed:
        reject("CANARY_TASK_STATUS_INVALID")
    return normalized


def fake_config() -> dict[str, Any]:
    app: dict[str, Any] = {
        "video_source": "pexels",
        "hide_config": True,
        "pexels_api_keys": ["fake-ci-material-key"],
        "pixabay_api_keys": [],
        "llm_provider": "openai",
        "openai_api_key": "fake-ci-llm-key",
        "openai_model_name": "fake-ci-model",
        "subtitle_provider": "edge",
        "endpoint": "",
        "material_directory": "task",
        "enable_redis": False,
        "max_concurrent_tasks": 1,
        "upload_post_enabled": False,
        "upload_post_auto_upload": False,
    }
    for provider, (secret_field, model_field) in PROVIDER_FIELDS.items():
        if provider != "openai":
            app[secret_field] = ""
            app[model_field] = ""
    app["pollinations_api_key"] = ""
    app["upload_post_api_key"] = ""
    return {
        "log_level": "WARNING",
        "app": app,
        "proxy": {},
        "azure": {},
        "siliconflow": {},
    }


def run_self_test() -> dict[str, Any]:
    good = fake_config()
    cases: list[tuple[str, Any, str | None]] = [
        ("valid", {}, None),
        ("log-level", {"log_level": "DEBUG"}, "CONFIG_LOG_LEVEL_UNSAFE"),
        ("hide-config", {"app.hide_config": False}, "CONFIG_UI_NOT_HIDDEN"),
        ("redis", {"app.enable_redis": True}, "CONFIG_EXTERNAL_QUEUE_FORBIDDEN"),
        ("endpoint", {"app.endpoint": "https://example.invalid"}, "CONFIG_ENDPOINT_FORBIDDEN"),
        ("material-dir", {"app.material_directory": "/tmp"}, "CONFIG_MATERIAL_DIRECTORY_INVALID"),
        ("concurrency", {"app.max_concurrent_tasks": 2}, "CONFIG_CONCURRENCY_INVALID"),
        ("publish", {"app.upload_post_enabled": True}, "CONFIG_AUTO_PUBLISH_ENABLED"),
        ("auto-upload", {"app.upload_post_auto_upload": True}, "CONFIG_AUTO_UPLOAD_ENABLED"),
        ("g4f", {"app.llm_provider": "g4f"}, "CONFIG_LLM_PROVIDER_INVALID"),
        ("missing-key", {"app.openai_api_key": ""}, "CONFIG_SELECTED_PROVIDER_KEY_ABSENT"),
        ("wrong-model", {"app.openai_model_name": "other"}, "CONFIG_MODEL_MISMATCH"),
        ("multiple-provider", {"app.deepseek_api_key": "fake"}, "CONFIG_MULTIPLE_PROVIDER_KEYS"),
        ("base-url", {"app.openai_base_url": "https://example.invalid"}, "CONFIG_BASE_URL_FORBIDDEN"),
        ("material-source", {"app.video_source": "pixabay"}, "CONFIG_MATERIAL_SOURCE_INVALID"),
        ("material-key-count", {"app.pexels_api_keys": ["a", "b"]}, "CONFIG_MATERIAL_KEY_COUNT_INVALID"),
        ("multiple-material", {"app.pixabay_api_keys": ["fake"]}, "CONFIG_MULTIPLE_MATERIAL_SOURCES"),
        ("voice", {"app.subtitle_provider": "whisper"}, "CONFIG_VOICE_PATH_INVALID"),
        ("proxy", {"proxy.http": "http://proxy.invalid"}, "CONFIG_PROXY_OR_EXTRA_CREDENTIAL_FORBIDDEN"),
        ("extra-credential", {"azure.speech_key": "fake"}, "CONFIG_PROXY_OR_EXTRA_CREDENTIAL_FORBIDDEN"),
    ]
    passed = 0
    for name, changes, expected in cases:
        del name
        candidate = copy.deepcopy(good)
        for dotted_key, value in changes.items():
            target = candidate
            parts = dotted_key.split(".")
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = value
        caught = None
        try:
            validate_config(
                candidate,
                llm_provider="openai",
                model="fake-ci-model",
                material_source="pexels",
                voice_path="edge",
            )
        except PreflightError as exc:
            caught = exc.code
        if caught != expected:
            raise AssertionError(f"self-test contract mismatch: expected={expected} actual={caught}")
        passed += 1

    safe_evidence = {
        "mainSha": "a" * 40,
        "credentialState": "PRESENT",
        "networkIsolation": "ENFORCED",
        "canaryProfile": CANARY_PROFILE,
        "taskId": "fake-task",
        "status": "DISABLED",
    }
    scan_evidence(safe_evidence)
    passed += 1
    try:
        scan_evidence({"apiKey": "must-not-appear"})
    except PreflightError as exc:
        if exc.code != "EVIDENCE_FORBIDDEN_FIELD":
            raise
    else:
        raise AssertionError("evidence scanner accepted forbidden field")
    passed += 1

    binding_defaults = {
        "profile": CANARY_PROFILE,
        "tenant_id": "fake-tenant",
        "config_version_id": "fake-config",
        "policy_hash": "a" * 64,
        "provider_budget_evidence": "PRESENT",
        "material_license_evidence": "PRESENT",
        "concurrent_limit": 1,
        "request_limit": 1,
        "generated_seconds_limit": 10,
        "output_limit": 1,
        "artifact_path_prefix": "/tasks/",
    }
    binding_cases = [
        ({}, None),
        ({"profile": "disabled"}, "CANARY_PROFILE_MISMATCH"),
        ({"policy_hash": "bad"}, "CANARY_POLICY_HASH_INVALID"),
        ({"provider_budget_evidence": "ABSENT"}, "PROVIDER_BUDGET_EVIDENCE_MISSING"),
        ({"material_license_evidence": "ABSENT"}, "MATERIAL_LICENSE_EVIDENCE_MISSING"),
        ({"concurrent_limit": 2}, "CANARY_POLICY_BUDGET_INVALID"),
        ({"request_limit": 2}, "CANARY_POLICY_BUDGET_INVALID"),
        ({"generated_seconds_limit": 11}, "CANARY_POLICY_BUDGET_INVALID"),
        ({"output_limit": 2}, "CANARY_POLICY_BUDGET_INVALID"),
        ({"artifact_path_prefix": "/"}, "CANARY_POLICY_BUDGET_INVALID"),
    ]
    for changes, expected in binding_cases:
        values = {**binding_defaults, **changes}
        caught = None
        try:
            validate_public_binding(argparse.Namespace(**values))
        except PreflightError as exc:
            caught = exc.code
        if caught != expected:
            raise AssertionError(
                f"binding self-test mismatch: expected={expected} actual={caught}"
            )
        passed += 1

    with tempfile.TemporaryDirectory(prefix="aether-fake-canary-") as temp_dir:
        temp = Path(temp_dir)
        config_path = temp / "fake.toml"
        config_path.write_text("log_level = 'WARNING'\n", encoding="utf-8")
        config_path.chmod(0o600)
        check_target_file(config_path, Path.cwd())
        passed += 1
        config_path.chmod(0o644)
        try:
            check_target_file(config_path, Path.cwd())
        except PreflightError as exc:
            if exc.code != "CONFIG_PERMISSIONS_TOO_BROAD":
                raise
        else:
            raise AssertionError("broad fake config permissions were accepted")
        passed += 1

        request_key = "cc501b5f-c18c-47cb-9b57-b444fdb07323"
        request_payload = {
            "videoSubject": "Abstract blue shapes crossing a synthetic city",
            "videoAspect": "9:16",
            "voiceName": "en-US-JennyNeural",
            "videoConcatMode": "sequential",
            "videoClipDuration": 10,
            "outputCount": 1,
            "inputAssetVersionIds": [],
            "idempotencyKey": request_key,
            "capabilitySnapshotHash": "b" * 64,
            "expectedProjectRevision": 0,
            "confirmExternalGeneration": True,
        }
        request_cases = [
            ({}, None),
            ({"videoSubject": "customer face"}, "CANARY_SUBJECT_NOT_SYNTHETIC"),
            ({"outputCount": 2}, "CANARY_OUTPUT_OR_INPUT_INVALID"),
            ({"inputAssetVersionIds": ["identity-asset"]}, "CANARY_OUTPUT_OR_INPUT_INVALID"),
            ({"videoClipDuration": 11}, "CANARY_DURATION_INVALID"),
        ]
        request_path = temp / "request.json"
        for changes, expected in request_cases:
            request_path.write_text(
                json.dumps({**request_payload, **changes}), encoding="utf-8"
            )
            caught = None
            try:
                validate_request_file(request_path, request_key)
            except PreflightError as exc:
                caught = exc.code
            if caught != expected:
                raise AssertionError(
                    f"request self-test mismatch: expected={expected} actual={caught}"
                )
            passed += 1

    try:
        scan_evidence({"status": "https://provider.invalid/private"})
    except PreflightError as exc:
        if exc.code != "EVIDENCE_SECRET_SHAPE":
            raise
    else:
        raise AssertionError("evidence scanner accepted a URL-shaped value")
    passed += 1

    return {"status": "PASS", "fakeOnly": True, "cases": passed}


def add_preflight_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository-root", required=True)
    parser.add_argument("--config-file", required=True)
    parser.add_argument("--approved-sha", required=True)
    parser.add_argument("--llm-provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--material-source", required=True)
    parser.add_argument("--voice-path", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--config-version-id", required=True)
    parser.add_argument("--policy-hash", required=True)
    parser.add_argument("--profile", default=CANARY_PROFILE)
    parser.add_argument("--provider-budget-evidence", choices=("PRESENT", "ABSENT"), required=True)
    parser.add_argument("--material-license-evidence", choices=("PRESENT", "ABSENT"), required=True)
    parser.add_argument("--concurrent-limit", type=int, default=1)
    parser.add_argument("--request-limit", type=int, default=1)
    parser.add_argument("--generated-seconds-limit", type=int, default=10)
    parser.add_argument("--output-limit", type=int, default=1)
    parser.add_argument("--artifact-path-prefix", default="/tasks/")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_preflight_arguments(subparsers.add_parser("preflight"))
    evidence_parser = subparsers.add_parser("scan-evidence")
    evidence_parser.add_argument("--evidence-file", required=True)
    request_parser = subparsers.add_parser("validate-request")
    request_parser.add_argument("--request-file", required=True)
    request_parser.add_argument("--idempotency-key", required=True)
    extract_parser = subparsers.add_parser("extract-task-field")
    extract_parser.add_argument("--response-file", required=True)
    extract_parser.add_argument("--field", choices=("taskId", "status"), required=True)
    subparsers.add_parser("self-test")
    args = parser.parse_args()
    try:
        if args.command == "preflight":
            result = preflight(args)
        elif args.command == "scan-evidence":
            try:
                evidence = json.loads(Path(args.evidence_file).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                reject("EVIDENCE_INVALID")
            scan_evidence(evidence)
            result = {"status": "PASS", "sanitized": True}
        elif args.command == "validate-request":
            validate_request_file(Path(args.request_file), args.idempotency_key)
            result = {"status": "PASS", "synthetic": True, "oneOutput": True}
        elif args.command == "extract-task-field":
            print(extract_task_field(Path(args.response_file), args.field))
            return 0
        else:
            result = run_self_test()
    except PreflightError as exc:
        print(json.dumps({"status": "REJECTED", "reasonCode": exc.code}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
