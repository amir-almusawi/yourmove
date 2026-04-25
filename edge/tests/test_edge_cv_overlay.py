import subprocess
import tempfile
from pathlib import Path
import pytest


def _make_test_video(path: Path, duration: float = 3.0, width: int = 640, height: int = 480) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"testsrc=duration={duration}:size={width}x{height}:rate=10",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-movflags", "+faststart",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


class TestBuildOverlayFilter:
    def test_single_hit_event(self):
        from edge.edge_cv_overlay import build_overlay_filter
        events = [
            {"time_offset_s": 1.0, "text": "+150 Hank fled!", "type": "score_popup"},
        ]
        f = build_overlay_filter(events, width=640, height=480)
        assert "drawtext" in f
        assert "+150 Hank fled!" in f

    def test_no_events_returns_empty(self):
        from edge.edge_cv_overlay import build_overlay_filter
        f = build_overlay_filter([], width=640, height=480)
        assert f == ""

    def test_multiple_events(self):
        from edge.edge_cv_overlay import build_overlay_filter
        events = [
            {"time_offset_s": 1.0, "text": "+150 Hank fled!", "type": "score_popup"},
            {"time_offset_s": 2.5, "text": "+100 Clyde startled!", "type": "score_popup"},
        ]
        f = build_overlay_filter(events, width=640, height=480)
        assert f.count("drawtext") == 2


class TestCompositeOverlay:
    def test_composite_creates_output(self, tmp_path):
        from edge.edge_cv_overlay import composite_overlay
        input_path = tmp_path / "input.mp4"
        output_path = tmp_path / "output.mp4"
        _make_test_video(input_path, duration=2.0)
        events = [
            {"time_offset_s": 0.5, "text": "+150 Hank fled!", "type": "score_popup"},
        ]
        ok = composite_overlay(input_path, output_path, events, width=640, height=480)
        assert ok is True
        assert output_path.exists()
        assert output_path.stat().st_size > 0

    def test_composite_no_events_copies(self, tmp_path):
        from edge.edge_cv_overlay import composite_overlay
        input_path = tmp_path / "input.mp4"
        output_path = tmp_path / "output.mp4"
        _make_test_video(input_path, duration=1.0)
        ok = composite_overlay(input_path, output_path, [], width=640, height=480)
        assert ok is True
        assert output_path.exists()
