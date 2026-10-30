"""Offline-only G72 talking-head adapter candidate.

This adapter is deliberately NOT wired into runtime selection. It uses only the
existing native DeterministicFakeAdapter. Output is a synthetic test pattern,
not real lip-sync. Trusted API scope and asset rights MUST be checked before use.
No paid provider calls, credentials, or outbound HTTP calls are implemented here.
"""
from __future__ import annotations

from typing import BinaryIO

from .deterministic_fake_adapter import DeterministicFakeAdapter, DeterministicFakeError
from .talking_head_provider_adapter import (
    TalkingHeadJobSpec,
    TalkingHeadProviderAdapter,
    TalkingHeadProviderError,
)


class DeterministicFakeTalkingHeadAdapter(TalkingHeadProviderAdapter):
    """Translation adapter for non-production G72 acceptance testing only."""

    enabled = True
    provider = "deterministic-fake-talking-head"
    network = "none"

    def __init__(self, generator: DeterministicFakeAdapter) -> None:
        if not isinstance(generator, DeterministicFakeAdapter):
            raise TypeError("Local deterministic fake generator required")
        self._generator = generator

    def submit(self, job_spec: TalkingHeadJobSpec) -> str:
        job_spec.validate()
        if (
            not job_spec.image_asset_id
            or job_spec.image_url
            or job_spec.avatar_id
            or job_spec.audio_url
            or job_spec.audio_asset_id
        ):
            raise TalkingHeadProviderError(
                "Local test requires an authorized image asset ID and synthetic voice",
                code="FAKE_VISUAL_OR_AUDIO_SCOPE_INVALID",
                retryable=False,
            )
        if not job_spec.voice_id:
            raise TalkingHeadProviderError(
                "A synthetic voice seed is required",
                code="FAKE_VOICE_REQUIRED",
                retryable=False,
            )
        try:
            return self._generator.generate_video(
                subject=job_spec.text,
                aspect=job_spec.aspect_ratio,
                voice_name=job_spec.voice_id,
                video_concat_mode="sequential",
                video_clip_duration=job_spec.duration_seconds,
            )
        except DeterministicFakeError as exc:
            raise TalkingHeadProviderError(
                "Synthetic video generation rejected",
                code="FAKE_SUBMIT_FAILED",
                retryable=False,
            ) from exc

    def status(self, job_id: str) -> dict:
        try:
            return self._generator.get_task_status(job_id)
        except DeterministicFakeError as exc:
            raise TalkingHeadProviderError(
                "Synthetic job status unavailable",
                code="FAKE_STATUS_UNKNOWN",
                retryable=False,
            ) from exc

    def artifact(self, job_id: str) -> BinaryIO:
        try:
            return self._generator.stream_artifact(job_id)
        except DeterministicFakeError as exc:
            raise TalkingHeadProviderError(
                "Synthetic artifact unavailable",
                code="FAKE_ARTIFACT_UNAVAILABLE",
                retryable=False,
            ) from exc
