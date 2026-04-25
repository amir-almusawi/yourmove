"""Clip overlay compositing via ffmpeg drawtext filters."""
from __future__ import annotations
import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def _escape_drawtext(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "'\\''").replace(":", "\\:").replace("%", "%%")


def build_overlay_filter(events: list[dict], width: int, height: int) -> str:
    if not events:
        return ""
    filters = []
    for i, event in enumerate(events):
        t = float(event.get("time_offset_s", 0))
        text = _escape_drawtext(event.get("text", ""))
        event_type = event.get("type", "score_popup")
        show_start = max(0, t)
        show_end = t + 2.0

        if event_type == "score_popup":
            x_pos = f"(w-text_w)/2"
            y_pos = f"h*0.3-text_h"
            fontsize = max(24, int(height * 0.06))
        else:
            x_pos = f"(w-text_w)/2"
            y_pos = f"h*0.85"
            fontsize = max(18, int(height * 0.04))

        f = (
            f"drawtext=text='{text}'"
            f":fontsize={fontsize}"
            f":fontcolor=white"
            f":borderw=2"
            f":bordercolor=black"
            f":x={x_pos}"
            f":y={y_pos}"
            f":enable='between(t,{show_start:.2f},{show_end:.2f})'"
        )
        filters.append(f)
    return ",".join(filters)


def composite_overlay(
    input_path: Path,
    output_path: Path,
    events: list[dict],
    width: int = 640,
    height: int = 480,
) -> bool:
    filter_str = build_overlay_filter(events, width, height)
    if not filter_str:
        shutil.copy2(input_path, output_path)
        return True

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-i", str(input_path),
        "-vf", filter_str,
        "-c:v", "libx264", "-preset", "veryfast",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error("overlay composite failed: %s", result.stderr)
        shutil.copy2(input_path, output_path)
        return True
    return output_path.exists() and output_path.stat().st_size > 0
