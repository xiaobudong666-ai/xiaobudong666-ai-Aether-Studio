from __future__ import annotations

import math
from fractions import Fraction
from typing import Any

from fastapi import HTTPException


def fraction(value: dict[str, int]) -> Fraction:
    return Fraction(value["value"], value["timescale"])


def timeline_duration(timeline: dict[str, Any]) -> Fraction:
    maximum = Fraction(0)
    for track in timeline.get("tracks", []):
        for clip in track.get("clips", []):
            maximum = max(maximum, fraction(clip["start"]) + fraction(clip["duration"]))
    return maximum


def build_render_payload(project) -> tuple[dict[str, Any], int]:
    materials = {material["id"]: material for material in project.materials}
    has_video = False
    normalized_tracks: list[dict[str, Any]] = []
    for order, track in enumerate(project.timeline.get("tracks", [])):
        clips: list[dict[str, Any]] = []
        for clip in track.get("clips", []):
            material = materials.get(clip.get("materialId"))
            if track.get("type") != "subtitle" and material is None:
                continue
            if track.get("type") == "video" and material and material.get("type") in {"video", "image"}:
                has_video = True
            clips.append(
                {
                    "id": clip["id"],
                    "materialId": clip.get("materialId"),
                    "start": clip["start"],
                    "duration": clip["duration"],
                    "sourceIn": clip["sourceIn"],
                    "volume": clip.get("volume", 1.0),
                    "opacity": clip.get("opacity", 1.0),
                    "x": clip.get("x", 0),
                    "y": clip.get("y", 0),
                    "width": clip.get("width"),
                    "height": clip.get("height"),
                    "text": clip.get("text"),
                }
            )
        normalized_tracks.append(
            {
                "id": track["id"],
                "type": track["type"],
                "order": order,
                "clips": clips,
            }
        )

    if not has_video:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "NO_RENDERABLE_VIDEO",
                "message": "Upload a video and place it on a video track before rendering",
            },
        )

    duration = timeline_duration(project.timeline)
    if duration <= 0:
        raise HTTPException(
            status_code=422,
            detail={"code": "EMPTY_TIMELINE", "message": "Timeline duration must be positive"},
        )
    duration_rational = {
        "value": duration.numerator,
        "timescale": duration.denominator,
    }
    payload = {
        "projectId": project.id,
        "canonicalTimeline": {
            "version": "1.1",
            "duration": duration_rational,
            "output": {
                "width": 1920,
                "height": 1080,
                "fps": {"value": 24, "timescale": 1},
                "backgroundColor": "black",
            },
            "materials": [
                {"id": material["id"], "type": material["type"]}
                for material in project.materials
            ],
            "tracks": normalized_tracks,
        },
        "mode": "preview",
        "grade": "auto",
        "normalizeAudio": True,
    }
    return payload, math.ceil(float(duration))
