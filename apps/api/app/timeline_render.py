from __future__ import annotations

import math
from fractions import Fraction
from typing import Any

from fastapi import HTTPException


CANONICAL_OUTPUTS = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
}


def fraction(value: dict[str, int]) -> Fraction:
    return Fraction(value["value"], value["timescale"])


def timeline_duration(timeline: dict[str, Any]) -> Fraction:
    maximum = Fraction(0)
    for track in timeline.get("tracks", []):
        for clip in track.get("clips", []):
            maximum = max(maximum, fraction(clip["start"]) + fraction(clip["duration"]))
    return maximum


def canonical_output(timeline: dict[str, Any]) -> tuple[int, int]:
    timeline_output = timeline.get("output") or {}
    if not timeline_output:
        return CANONICAL_OUTPUTS["16:9"]

    output_width = timeline_output.get("width")
    output_height = timeline_output.get("height")
    expected_aspect = timeline_output.get("aspect")
    matched_aspect = next(
        (
            aspect
            for aspect, dimensions in CANONICAL_OUTPUTS.items()
            if dimensions == (output_width, output_height)
        ),
        None,
    )
    if matched_aspect is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UNSUPPORTED_OUTPUT_DIMENSIONS",
                "message": "输出尺寸必须使用受支持的 16:9、9:16 或 1:1 P0 画布",
            },
        )
    if expected_aspect is not None and expected_aspect != matched_aspect:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "OUTPUT_ASPECT_MISMATCH",
                "message": "输出比例与画布尺寸不一致",
            },
        )
    return output_width, output_height


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
                "message": "请先上传视频并将其添加到视频轨道后再渲染",
            },
        )

    duration = timeline_duration(project.timeline)
    if duration <= 0:
        raise HTTPException(
            status_code=422,
            detail={"code": "EMPTY_TIMELINE", "message": "时间线必须包含有效时长"},
        )
    duration_rational = {
        "value": duration.numerator,
        "timescale": duration.denominator,
    }
    output_width, output_height = canonical_output(project.timeline)

    payload = {
        "projectId": project.id,
        "canonicalTimeline": {
            "version": "1.1",
            "duration": duration_rational,
            "output": {
                "width": output_width,
                "height": output_height,
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
