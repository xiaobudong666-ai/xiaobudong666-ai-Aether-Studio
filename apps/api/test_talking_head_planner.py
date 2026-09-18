from app.talking_head_planner import SubtitleCue, build_talking_head_timeline


def test_p0_talking_head_defaults_to_vertical_output():
    timeline = build_talking_head_timeline(
        video_material_id="video-1",
        audio_material_id="audio-1",
        duration_ms=10_000,
    )
    assert timeline["output"] == {"aspect": "9:16", "width": 1080, "height": 1920}
    assert [track["type"] for track in timeline["tracks"]] == ["video", "audio"]


def test_p0_talking_head_adds_subtitle_track_without_material_dependency():
    timeline = build_talking_head_timeline(
        video_material_id="video-1",
        audio_material_id=None,
        duration_ms=5_000,
        subtitles=(SubtitleCue("第一句", 0, 2_000), SubtitleCue("第二句", 2_000, 3_000)),
    )
    subtitle_track = timeline["tracks"][-1]
    assert subtitle_track["type"] == "subtitle"
    assert [clip["text"] for clip in subtitle_track["clips"]] == ["第一句", "第二句"]


def test_p0_talking_head_rejects_subtitle_outside_duration():
    try:
        build_talking_head_timeline(
            video_material_id="video-1",
            audio_material_id=None,
            duration_ms=1_000,
            subtitles=(SubtitleCue("越界", 900, 200),),
        )
    except ValueError as exc:
        assert "fit inside" in str(exc)
    else:
        raise AssertionError("expected fail-closed subtitle validation")


def test_p0_talking_head_supports_square_and_landscape_without_changing_default():
    square = build_talking_head_timeline(video_material_id="v", audio_material_id=None, duration_ms=1000, aspect="1:1")
    landscape = build_talking_head_timeline(video_material_id="v", audio_material_id=None, duration_ms=1000, aspect="16:9")
    assert square["output"]["width"] == square["output"]["height"] == 1080
    assert landscape["output"]["width"] == 1920
    assert landscape["output"]["height"] == 1080
