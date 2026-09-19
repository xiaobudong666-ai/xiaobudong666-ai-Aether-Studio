"""Deterministic P0 talking-head timeline planner.

Pure orchestration only: no Provider, network, database, Worker, retry, or publish side effects.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Aspect = Literal["16:9", "9:16", "1:1"]

_ASPECT_OUTPUT = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
}


@dataclass(frozen=True)
class SubtitleCue:
    text: str
    start_ms: int
    duration_ms: int


def _rt(milliseconds: int) -> dict[str, int]:
    if milliseconds < 0:
        raise ValueError("milliseconds must be non-negative")
    return {"value": milliseconds, "timescale": 1000}


def build_talking_head_timeline(
    *,
    video_material_id: str,
    audio_material_id: str | None,
    duration_ms: int,
    aspect: Aspect = "9:16",
    subtitles: tuple[SubtitleCue, ...] = (),
) -> dict:
    """Build a Canonical Timeline v1.1 payload for a talking-head composition."""
    if not video_material_id.strip():
        raise ValueError("video_material_id is required")
    if audio_material_id is not None and not audio_material_id.strip():
        raise ValueError("audio_material_id must not be blank")
    if duration_ms <= 0:
        raise ValueError("duration_ms must be positive")
    if aspect not in _ASPECT_OUTPUT:
        raise ValueError("unsupported aspect")

    width, height = _ASPECT_OUTPUT[aspect]
    duration = _rt(duration_ms)
    tracks: list[dict] = [{
        "id": "talking-head-video",
        "name": "口播人物",
        "type": "video",
        "clips": [{
            "id": "talking-head-video-clip",
            "trackId": "talking-head-video",
            "materialId": video_material_id,
            "start": _rt(0),
            "duration": duration,
            "sourceIn": _rt(0),
            # When an explicit narration track exists it is authoritative. Mute any
            # embedded source audio to prevent double-mix/echo in Canonical Render.
            "volume": 0.0 if audio_material_id is not None else 1.0,
            "x": 0,
            "y": 0,
            "width": width,
            "height": height,
        }],
    }]

    if audio_material_id is not None:
        tracks.append({
            "id": "talking-head-audio",
            "name": "口播声音",
            "type": "audio",
            "clips": [{
                "id": "talking-head-audio-clip",
                "trackId": "talking-head-audio",
                "materialId": audio_material_id,
                "start": _rt(0),
                "duration": duration,
                "sourceIn": _rt(0),
                "volume": 1.0,
            }],
        })

    subtitle_clips: list[dict] = []
    for index, cue in enumerate(subtitles):
        text = cue.text.strip()
        if not text:
            raise ValueError("subtitle text must not be blank")
        if cue.start_ms < 0 or cue.duration_ms <= 0 or cue.start_ms + cue.duration_ms > duration_ms:
            raise ValueError("subtitle cue must fit inside timeline duration")
        subtitle_clips.append({
            "id": f"talking-head-subtitle-{index + 1}",
            "trackId": "talking-head-subtitles",
            "materialId": f"subtitle:{index + 1}",
            "start": _rt(cue.start_ms),
            "duration": _rt(cue.duration_ms),
            "sourceIn": _rt(0),
            "text": text,
        })
    if subtitle_clips:
        tracks.append({
            "id": "talking-head-subtitles",
            "name": "口播字幕",
            "type": "subtitle",
            "clips": subtitle_clips,
        })

    return {
        "version": "1.1",
        "output": {"aspect": aspect, "width": width, "height": height},
        "tracks": tracks,
    }
