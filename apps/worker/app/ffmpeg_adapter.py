from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any, Sequence

logger = logging.getLogger("worker.ffmpeg")


class MediaProcessingError(RuntimeError):
    """Raised when a real FFmpeg/ffprobe operation cannot be completed."""


class FFmpegAdapter:
    """Execute bounded, auditable media operations with FFmpeg.

    Callers pass argument arrays rather than shell fragments.  This keeps file
    names and metadata out of a shell interpreter and gives every failure a
    typed boundary with a short diagnostic.
    """

    def __init__(
        self,
        target_width: int = 854,
        target_height: int = 480,
        ffmpeg_binary: str = "ffmpeg",
        ffprobe_binary: str = "ffprobe",
        timeout_seconds: int = 1_800,
    ):
        if target_width < 2 or target_height < 2:
            raise ValueError("target dimensions must be positive")
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")

        self.target_width = target_width
        self.target_height = target_height
        self.ffmpeg_binary = ffmpeg_binary
        self.ffprobe_binary = ffprobe_binary
        self.timeout_seconds = timeout_seconds
        logger.info(
            "FFmpegAdapter initialized for a %sx%s proxy target",
            target_width,
            target_height,
        )

    def capabilities(self) -> dict[str, Any]:
        return {
            "ffmpeg": shutil.which(self.ffmpeg_binary) is not None,
            "ffprobe": shutil.which(self.ffprobe_binary) is not None,
            "proxy": {"width": self.target_width, "height": self.target_height},
            "operations": ["probe", "proxy", "extract_audio"],
        }

    def _run(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                list(command),
                check=True,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise MediaProcessingError(
                f"Required media binary is unavailable: {command[0]}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise MediaProcessingError(
                f"Media operation exceeded {self.timeout_seconds} seconds"
            ) from exc
        except subprocess.CalledProcessError as exc:
            diagnostic = (exc.stderr or exc.stdout or "FFmpeg failed").strip()
            if len(diagnostic) > 2_000:
                diagnostic = diagnostic[-2_000:]
            raise MediaProcessingError(diagnostic) from exc

    @staticmethod
    def _require_input(input_path: str | Path) -> Path:
        resolved = Path(input_path).expanduser().resolve()
        if not resolved.is_file():
            raise MediaProcessingError(f"Input media does not exist: {resolved}")
        return resolved

    @staticmethod
    def _prepare_output(output_path: str | Path) -> Path:
        resolved = Path(output_path).expanduser().resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        if resolved.exists() and resolved.is_dir():
            raise MediaProcessingError(f"Output path is a directory: {resolved}")
        return resolved

    def probe_media(self, input_path: str | Path) -> dict[str, Any]:
        source = self._require_input(input_path)
        result = self._run(
            [
                self.ffprobe_binary,
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(source),
            ]
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise MediaProcessingError("ffprobe returned invalid JSON") from exc

        video = next(
            (stream for stream in payload.get("streams", []) if stream.get("codec_type") == "video"),
            None,
        )
        audio = next(
            (stream for stream in payload.get("streams", []) if stream.get("codec_type") == "audio"),
            None,
        )
        duration_raw = payload.get("format", {}).get("duration")
        return {
            "duration_seconds": float(duration_raw) if duration_raw is not None else None,
            "format_name": payload.get("format", {}).get("format_name"),
            "size_bytes": int(payload.get("format", {}).get("size", 0) or 0),
            "video": (
                {
                    "codec": video.get("codec_name"),
                    "width": video.get("width"),
                    "height": video.get("height"),
                    "pixel_format": video.get("pix_fmt"),
                }
                if video
                else None
            ),
            "audio": (
                {
                    "codec": audio.get("codec_name"),
                    "sample_rate": int(audio.get("sample_rate", 0) or 0),
                    "channels": audio.get("channels"),
                }
                if audio
                else None
            ),
        }

    def create_480p_proxy(self, input_path: str, output_path: str) -> bool:
        source = self._require_input(input_path)
        destination = self._prepare_output(output_path)
        scale_and_pad = (
            f"scale={self.target_width}:{self.target_height}:"
            "force_original_aspect_ratio=decrease,"
            f"pad={self.target_width}:{self.target_height}:"
            "(ow-iw)/2:(oh-ih)/2:color=black"
        )
        self._run(
            [
                self.ffmpeg_binary,
                "-y",
                "-hide_banner",
                "-i",
                str(source),
                "-vf",
                scale_and_pad,
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-r",
                "24",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                str(destination),
            ]
        )
        if not destination.is_file() or destination.stat().st_size == 0:
            raise MediaProcessingError("FFmpeg completed without a proxy output")
        logger.info("Created real proxy %s from %s", destination, source)
        return True

    def extract_audio(self, video_path: str, audio_output_path: str) -> bool:
        source = self._require_input(video_path)
        destination = self._prepare_output(audio_output_path)
        self._run(
            [
                self.ffmpeg_binary,
                "-y",
                "-hide_banner",
                "-i",
                str(source),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(destination),
            ]
        )
        if not destination.is_file() or destination.stat().st_size == 0:
            raise MediaProcessingError("FFmpeg completed without an audio output")
        logger.info("Extracted real audio %s from %s", destination, source)
        return True
