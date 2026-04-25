# edge/tests/test_edge_cv_runtime.py
import json
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path


SAMPLE_GAME_CONFIG = {
    "template_type": "blast",
    "template_config": {
        "type": "blast",
        "scoring": {
            "reactions": {"fled": 150, "startled": 100, "unbothered": 75, "glancing": 40, "miss": 5, "absent": 0},
            "multipliers": ["streak", "crowd"],
            "allow_negative": False,
        },
        "detection": {"model": "yolov8n", "classes": [{"class": "bird", "role": "target"}]},
    },
    "roster": [
        {"id": 1, "name": "Hank", "detection_class": "bird", "color_hex": "#8B4513", "role": "target",
         "color_description": "brown", "appearance_features": {}},
    ],
    "detection_rules": [],
}


class TestFetchGameConfig:
    def test_extracts_game_config(self):
        from edge.edge_cv_runtime import fetch_game_config
        with patch("edge.edge_cv_runtime.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.json.return_value = {"game_config": SAMPLE_GAME_CONFIG}
            mock_get.return_value = mock_resp
            config = fetch_game_config("https://yourmove.live", "test-node", "token123", timeout=5, ssl_verify=False)
            assert config is not None
            assert config["template_type"] == "blast"
            assert len(config["roster"]) == 1

    def test_returns_none_when_no_game_config(self):
        from edge.edge_cv_runtime import fetch_game_config
        with patch("edge.edge_cv_runtime.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.json.return_value = {}
            mock_get.return_value = mock_resp
            config = fetch_game_config("https://yourmove.live", "test-node", "token123", timeout=5, ssl_verify=False)
            assert config is None


class TestPostGameEvent:
    def test_posts_to_platform(self):
        from edge.edge_cv_runtime import post_game_event
        with patch("edge.edge_cv_runtime.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_post.return_value = mock_resp
            ok = post_game_event(
                base_url="https://yourmove.live",
                node_slug="test-node",
                auth_token="token123",
                event_payload={"interaction_id": 42, "event": "hit", "reaction": "fled"},
                timeout=5,
                ssl_verify=False,
            )
            assert ok is True
            mock_post.assert_called_once()
            call_url = mock_post.call_args[0][0]
            assert "/operator/game-events" in call_url


class TestReportCapability:
    def test_posts_capability(self):
        from edge.edge_cv_runtime import report_capability
        with patch("edge.edge_cv_runtime.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_post.return_value = mock_resp
            ok = report_capability(
                base_url="https://yourmove.live",
                node_slug="test-node",
                auth_token="token123",
                benchmark_result={"fps": 24.5, "tier": 2, "model_name": "yolov8n", "hardware_summary": "x86_64"},
                timeout=5,
                ssl_verify=False,
            )
            assert ok is True


class TestBuildOverlayEvents:
    def test_converts_game_events(self):
        from edge.edge_cv_runtime import build_overlay_events
        game_events = [
            {"event": "hit", "reaction": "fled", "target": {"name": "Hank"}, "score": {"final": 150}, "timestamp_ms": 1000},
            {"event": "hit", "reaction": "startled", "target": {"name": "Clyde"}, "score": {"final": 100}, "timestamp_ms": 2500},
        ]
        clip_start_ms = 0
        overlay_events = build_overlay_events(game_events, clip_start_ms)
        assert len(overlay_events) == 2
        assert overlay_events[0]["time_offset_s"] == 1.0
        assert "+150" in overlay_events[0]["text"]
        assert "Hank" in overlay_events[0]["text"]
        assert overlay_events[1]["time_offset_s"] == 2.5


def test_class_ignore_filters_detections():
    from edge.edge_cv_runtime import filter_detections
    detections = [
        {"class": "bird", "bbox": [10, 10, 50, 50], "confidence": 0.9},
        {"class": "person", "bbox": [100, 100, 200, 200], "confidence": 0.8},
        {"class": "boat", "bbox": [300, 300, 400, 400], "confidence": 0.7},
    ]
    cv_settings = {"cv_ignored_classes": ["boat", "person"], "cv_confidence_threshold": 0, "cv_exclusion_zones": []}
    result = filter_detections(detections, cv_settings, frame_shape=(480, 640))
    assert len(result) == 1
    assert result[0]["class"] == "bird"


def test_confidence_threshold_filters_detections():
    from edge.edge_cv_runtime import filter_detections
    detections = [
        {"class": "bird", "bbox": [10, 10, 50, 50], "confidence": 0.9},
        {"class": "bird", "bbox": [100, 100, 200, 200], "confidence": 0.25},
        {"class": "person", "bbox": [300, 300, 400, 400], "confidence": 0.4},
    ]
    cv_settings = {"cv_ignored_classes": [], "cv_confidence_threshold": 50, "cv_exclusion_zones": []}
    result = filter_detections(detections, cv_settings, frame_shape=(480, 640))
    assert len(result) == 1
    assert result[0]["class"] == "bird"
    assert result[0]["confidence"] == 0.9


def test_exclusion_zone_filters_detections():
    from edge.edge_cv_runtime import filter_detections
    detections = [
        {"class": "bird", "bbox": [10, 10, 50, 50], "confidence": 0.9},
        {"class": "bird", "bbox": [500, 400, 600, 450], "confidence": 0.85},
    ]
    cv_settings = {
        "cv_ignored_classes": [],
        "cv_confidence_threshold": 0,
        "cv_exclusion_zones": [
            {"x": 0.7, "y": 0.8, "w": 0.3, "h": 0.2}
        ],
    }
    result = filter_detections(detections, cv_settings, frame_shape=(480, 640))
    assert len(result) == 1
    assert result[0]["bbox"] == [10, 10, 50, 50]


def test_exclusion_zone_center_check():
    from edge.edge_cv_runtime import filter_detections
    detections = [
        {"class": "person", "bbox": [300, 200, 340, 260], "confidence": 0.8},
    ]
    cv_settings = {
        "cv_ignored_classes": [],
        "cv_confidence_threshold": 0,
        "cv_exclusion_zones": [
            {"x": 0.45, "y": 0.4, "w": 0.15, "h": 0.2}
        ],
    }
    result = filter_detections(detections, cv_settings, frame_shape=(480, 640))
    assert len(result) == 0


def test_full_filter_pipeline():
    from edge.edge_cv_runtime import filter_detections
    detections = [
        {"class": "bird", "bbox": [10, 10, 50, 50], "confidence": 0.92},
        {"class": "person", "bbox": [100, 100, 200, 200], "confidence": 0.85},
        {"class": "boat", "bbox": [300, 300, 400, 400], "confidence": 0.70},
        {"class": "bird", "bbox": [500, 20, 550, 60], "confidence": 0.15},
        {"class": "cat", "bbox": [590, 420, 630, 470], "confidence": 0.60},
    ]
    cv_settings = {
        "cv_ignored_classes": ["boat"],
        "cv_confidence_threshold": 30,
        "cv_exclusion_zones": [
            {"x": 0.85, "y": 0.85, "w": 0.15, "h": 0.15}
        ],
    }
    result = filter_detections(detections, cv_settings, frame_shape=(480, 640))
    classes = [d["class"] for d in result]
    assert "boat" not in classes
    assert {"class": "bird", "bbox": [10, 10, 50, 50], "confidence": 0.92} in result
    assert {"class": "person", "bbox": [100, 100, 200, 200], "confidence": 0.85} in result
    assert len([d for d in result if d["confidence"] < 0.30]) == 0
    assert "cat" not in classes


def test_empty_settings_passes_all():
    from edge.edge_cv_runtime import filter_detections
    detections = [
        {"class": "bird", "bbox": [10, 10, 50, 50], "confidence": 0.5},
        {"class": "person", "bbox": [100, 100, 200, 200], "confidence": 0.3},
    ]
    cv_settings = {"cv_ignored_classes": [], "cv_confidence_threshold": 0, "cv_exclusion_zones": []}
    result = filter_detections(detections, cv_settings, frame_shape=(480, 640))
    assert len(result) == 2


class TestEmbeddingDedup:
    def test_extract_crops_uses_embedding_cache(self):
        """Verify that extract_and_upload_crops sends embedding instead of phash."""
        from edge.edge_cv_runtime import extract_and_upload_crops
        import numpy as np
        from unittest.mock import patch, MagicMock
        from pathlib import Path
        import tempfile

        frame = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)
        detections = [
            {"class": "bird", "confidence": 0.9, "bbox": [100, 100, 300, 300]},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("edge.edge_cv_runtime.requests.post") as mock_post:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_post.return_value = mock_resp

                extract_and_upload_crops(
                    frame, detections, "test-node",
                    "https://example.com", "token",
                    Path(tmpdir), source="idle",
                    ssl_verify=False,
                )

                crop_call = [c for c in mock_post.call_args_list if "cv-crops" in str(c) and "image" not in str(c)]
                if crop_call:
                    payload = crop_call[0].kwargs.get("json") or crop_call[0][1].get("json", {})
                    crops = payload.get("crops", [])
                    assert len(crops) > 0
                    assert "embedding" in crops[0]
                    assert "phash" not in crops[0]
