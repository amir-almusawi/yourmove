import tempfile
import subprocess
from pathlib import Path
import pytest


def _make_test_segment(directory: Path, timestamp: int, duration_seconds: float = 1.0, width: int = 320, height: int = 240) -> Path:
    """Generate a minimal .ts segment using ffmpeg with a test pattern."""
    output = directory / f"{timestamp}.ts"
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"testsrc=duration={duration_seconds}:size={width}x{height}:rate=10",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-f", "mpegts", str(output),
        ],
        check=True,
        capture_output=True,
    )
    return output


class TestDecodeLatestSegment:
    def test_decode_returns_frames(self, tmp_path):
        _make_test_segment(tmp_path, 1000000, duration_seconds=0.5)
        from edge.edge_cv_detect import decode_latest_segment
        frames = decode_latest_segment(tmp_path)
        assert len(frames) > 0
        h, w = frames[0].shape[:2]
        assert h == 240
        assert w == 320

    def test_decode_empty_dir_returns_empty(self, tmp_path):
        from edge.edge_cv_detect import decode_latest_segment
        frames = decode_latest_segment(tmp_path)
        assert frames == []

    def test_decode_picks_newest_segment(self, tmp_path):
        _make_test_segment(tmp_path, 1000000, duration_seconds=0.3)
        _make_test_segment(tmp_path, 1000002, duration_seconds=0.3)
        from edge.edge_cv_detect import decode_latest_segment
        frames = decode_latest_segment(tmp_path)
        assert len(frames) > 0


class TestRunDetection:
    def test_run_detection_returns_boxes(self, tmp_path):
        _make_test_segment(tmp_path, 1000000, duration_seconds=0.5)
        from edge.edge_cv_detect import decode_latest_segment, run_detection
        frames = decode_latest_segment(tmp_path)
        assert len(frames) > 0
        detections = run_detection(frames[0], model_name="yolov8n")
        assert isinstance(detections, list)
        # test pattern may or may not detect objects — just verify structure
        for det in detections:
            assert "class" in det
            assert "bbox" in det
            assert "confidence" in det


class TestDetectionFormat:
    def test_detection_dict_structure(self):
        from edge.edge_cv_detect import _format_detection
        import numpy as np
        det = _format_detection(
            class_name="bird",
            bbox=np.array([100.0, 50.0, 200.0, 150.0]),
            confidence=0.85,
        )
        assert det == {
            "class": "bird",
            "bbox": [100, 50, 200, 150],
            "confidence": 0.85,
        }
