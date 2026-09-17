"""M1-C1 frozen fake-only tests: 40 deterministic cases, no network/DB/Worker/Provider."""
from datetime import datetime, timezone, timedelta

from app.timeline_receipt_validator import validate_timeline_receipt, validate_release_timeline_binding
from app.quality_release_gate import evaluate_quality_release, validate_human_release_decision
from app.platform_rule_evaluator import evaluate_rule_pack
from app.render_parity_contract import evaluate_render_parity

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
D = "sha256:" + "a" * 64


def receipt(**kw):
    base = dict(idempotency_key="idem1", timeline_version_before=1, timeline_version_after=2,
                timeline_digest_after=D, operation_type="trim", parameters_digest=D,
                undo_ref="cp1", preview_evidence_ref="pv1")
    base.update(kw)
    return base


def test_trv_001_accept_valid_receipt(): assert validate_timeline_receipt(receipt(), current_version=1).accepted

def test_trv_002_reject_version_conflict(): assert validate_timeline_receipt(receipt(timeline_version_before=0), current_version=1).code == "TIMELINE_VERSION_CONFLICT"

def test_trv_003_idempotent_replay_no_advance():
    p=dict(idempotency_key="idem1",timeline_version_before=1,timeline_version_after=2,timeline_digest_after=D)
    assert validate_timeline_receipt(receipt(), current_version=2, prior_commit=p).deduplicated

def test_trv_004_idempotency_conflict():
    p=dict(idempotency_key="idem1",timeline_version_before=1,timeline_version_after=2,timeline_digest_after="x")
    assert validate_timeline_receipt(receipt(), current_version=2, prior_commit=p).code == "TIMELINE_IDEMPOTENCY_CONFLICT"

def test_trv_005_atomic_batch_failure(): assert validate_timeline_receipt(receipt(operation_type="batch",batch_result="partial_failure"), current_version=1).code == "TIMELINE_ATOMIC_BATCH_FAILED"

def test_trv_006_undo_checkpoint_required(): assert validate_timeline_receipt(receipt(operation_type="undo"), current_version=1).code == "TIMELINE_UNDO_CHECKPOINT_INVALID"

def test_trv_007_preview_evidence_required(): assert not validate_timeline_receipt(receipt(preview_evidence_ref=None), current_version=1).accepted

def test_trv_008_digest_required(): assert not validate_timeline_receipt(receipt(timeline_digest_after=None), current_version=1).accepted

def test_trv_009_stale_release_version(): assert not validate_release_timeline_binding(dict(approved_timeline_version=1,approved_timeline_digest=D),current_version=2,current_digest=D).accepted

def test_trv_010_release_binding_valid(): assert validate_release_timeline_binding(dict(approved_timeline_version=2,approved_timeline_digest=D),current_version=2,current_digest=D).accepted


def test_qrg_001_no_findings_review(): assert evaluate_quality_release([]).aggregate == "REVIEW_REQUIRED"

def test_qrg_002_advisory_can_reach_human(): assert evaluate_quality_release([{"state":"ADVISORY"}]).eligible_for_human_release

def test_qrg_003_review_blocks(): assert not evaluate_quality_release([{"state":"REVIEW_REQUIRED"}]).eligible_for_human_release

def test_qrg_004_overridable_block_blocks(): assert evaluate_quality_release([{"state":"BLOCK_OVERRIDABLE"}]).aggregate == "BLOCK_OVERRIDABLE"

def test_qrg_005_nonoverride_wins(): assert evaluate_quality_release([{"state":"ADVISORY"},{"state":"BLOCK_NON_OVERRIDABLE"}]).aggregate == "BLOCK_NON_OVERRIDABLE"

def test_qrg_006_unknown_state_reviews(): assert evaluate_quality_release([{"state":"NEW_UNKNOWN"}]).aggregate == "REVIEW_REQUIRED"

def decision(**kw):
    b=dict(actor_type="human",decision="APPROVE",timeline_digest=D,preview_evidence_ref="pv",qa_summary_digest=D,rule_pack_digest=D,rights_evidence_digest=D,parity_policy_version="v1")
    b.update(kw); return b

def check_dec(d): return validate_human_release_decision(d,timeline_digest=D,preview_evidence_ref="pv",qa_summary_digest=D,rule_pack_digest=D,rights_evidence_digest=D,parity_policy_version="v1")

def test_qrg_007_human_required(): assert not check_dec(decision(actor_type="agent")).eligible_for_human_release

def test_qrg_008_stale_evidence_blocks(): assert check_dec(decision(rule_pack_digest="old")).aggregate == "STALE_RECOMPUTE_REQUIRED"

def test_qrg_009_valid_human_release(): assert check_dec(decision()).eligible_for_human_release


def pack(**kw):
    b=dict(verification_status="verified",source_fingerprint=D,revalidate_after=NOW+timedelta(days=1),effective_from=NOW-timedelta(days=1),platform_overlay_status="verified")
    b.update(kw); return b

def test_pre_001_unverified_fails_closed(): assert evaluate_rule_pack(pack(verification_status="draft"),evaluation_time=NOW,content={}).manual_review_required

def test_pre_002_stale_fails_closed(): assert evaluate_rule_pack(pack(revalidate_after=NOW-timedelta(seconds=1)),evaluation_time=NOW,content={}).code == "POLICY_RULE_PACK_STALE"

def test_pre_003_not_effective_fails_closed(): assert not evaluate_rule_pack(pack(effective_from=NOW+timedelta(days=1)),evaluation_time=NOW,content={}).allowed_to_prepare

def test_pre_004_expired_fails_closed(): assert evaluate_rule_pack(pack(effective_to=NOW-timedelta(seconds=1)),evaluation_time=NOW,content={}).code == "POLICY_RULE_PACK_EXPIRED"

def test_pre_005_source_fingerprint_required(): assert not evaluate_rule_pack(pack(source_fingerprint=None),evaluation_time=NOW,content={}).allowed_to_prepare

def test_pre_006_provenance_tamper_hard_block(): assert not evaluate_rule_pack(pack(),evaluation_time=NOW,content={"provenance_tampered":True}).manual_review_required

def test_pre_007_ai_label_required(): assert evaluate_rule_pack(pack(jurisdiction_ai_label_required=True),evaluation_time=NOW,content={"ai_generated_or_synthetic":True,"public_distribution":True}).code == "POLICY_AI_LABEL_REQUIRED"

def test_pre_008_verified_rules_pass(): assert evaluate_rule_pack(pack(),evaluation_time=NOW,content={}).allowed_to_prepare


def parity(**changes):
    p=dict(timeline_version=1,timeline_digest=D,source_digest=D,caption_digest=D,audio_digest=D,evidence_ref="p")
    f=dict(p); f.update(evidence_ref="f",threshold_set_version="v1",structural_match=True,perceptual_result="pass"); f.update(changes)
    return p,f

def test_rpc_001_valid_parity(): p,f=parity(); assert evaluate_render_parity(p,f,approved_threshold_version="v1").valid

def test_rpc_002_timeline_mismatch(): p,f=parity(timeline_version=2); assert not evaluate_render_parity(p,f,approved_threshold_version="v1").valid

def test_rpc_003_caption_mismatch(): p,f=parity(caption_digest="x"); assert not evaluate_render_parity(p,f,approved_threshold_version="v1").valid

def test_rpc_004_policy_change(): p,f=parity(threshold_set_version="v2"); assert evaluate_render_parity(p,f,approved_threshold_version="v1").code == "RENDER_PARITY_POLICY_CHANGED"

def test_rpc_005_structural_mismatch(): p,f=parity(structural_match=False); assert not evaluate_render_parity(p,f,approved_threshold_version="v1").valid

def test_rpc_006_encoding_only_variance(): p,f=parity(perceptual_result="encoding_only_variance"); assert evaluate_render_parity(p,f,approved_threshold_version="v1").valid


def test_fwl_001_no_network_imports():
    import app.timeline_receipt_validator as m; assert "requests" not in m.__dict__ and "httpx" not in m.__dict__

def test_fwl_002_no_db_imports():
    import app.quality_release_gate as m; assert "sqlalchemy" not in m.__dict__

def test_fwl_003_no_worker_imports():
    import app.platform_rule_evaluator as m; assert "worker" not in m.__dict__

def test_fwl_004_no_provider_imports():
    import app.render_parity_contract as m; assert "provider" not in m.__dict__

def test_fwl_005_no_subprocess_imports():
    import app.render_parity_contract as m; assert "subprocess" not in m.__dict__

def test_fwl_006_no_secret_env_reads():
    import inspect, app.timeline_receipt_validator as m; s=inspect.getsource(m); assert "os.environ" not in s and "getenv(" not in s

def test_fwl_007_no_retry_or_fallback_execution():
    import inspect, app.quality_release_gate as m; s=inspect.getsource(m).lower(); assert "sleep(" not in s and "requests." not in s
