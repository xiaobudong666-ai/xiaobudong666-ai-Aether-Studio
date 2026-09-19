from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.timeline_render import build_render_payload


def _project(output=None):
    timeline = {
        "version": "1.1",
        "tracks": [{
            "id": "video",
            "name": "视频",
            "type": "video",
            "clips": [{
                "id": "clip",
                "trackId": "video",
                "materialId": "material-video",
                "start": {"value": 0, "timescale": 1},
                "duration": {"value": 10, "timescale": 1},
                "sourceIn": {"value": 0, "timescale": 1},
            }],
        }],
    }
    if output is not None:
        timeline["output"] = output
    return SimpleNamespace(
        id="project-1",
        timeline=timeline,
        materials=[{"id": "material-video", "type": "video"}],
    )


def test_render_uses_vertical_timeline_output_when_present():
    payload, duration = build_render_payload(_project({"aspect": "9:16", "width": 1080, "height": 1920}))
    assert duration == 10
    assert payload["canonicalTimeline"]["output"]["width"] == 1080
    assert payload["canonicalTimeline"]["output"]["height"] == 1920


def test_render_preserves_legacy_landscape_default_when_output_is_omitted():
    payload, _ = build_render_payload(_project())
    assert payload["canonicalTimeline"]["output"]["width"] == 1920
    assert payload["canonicalTimeline"]["output"]["height"] == 1080


def test_render_rejects_noncanonical_output_dimensions():
    with pytest.raises(HTTPException) as exc_info:
        build_render_payload(_project({"aspect": "9:16", "width": 7680, "height": 7680}))
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "UNSUPPORTED_OUTPUT_DIMENSIONS"


def test_render_rejects_aspect_dimension_mismatch():
    with pytest.raises(HTTPException) as exc_info:
        build_render_payload(_project({"aspect": "9:16", "width": 1920, "height": 1080}))
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "OUTPUT_ASPECT_MISMATCH"
