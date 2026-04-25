"""Tests for accuracy-based scoring."""
import pytest
from edge.edge_cv_score import (
    pan_tilt_to_crosshair,
    score_targets_by_accuracy,
    compute_score,
)


class TestPanTiltToCrosshair:
    """Convert turret pan/tilt to normalized 0-1 crosshair position."""

    def test_center_position(self):
        aim_config = {
            "pan": 0.0, "tilt": 0.0,
            "pan_min": -90.0, "pan_max": 90.0,
            "tilt_min": -45.0, "tilt_max": 45.0,
        }
        x, y = pan_tilt_to_crosshair(aim_config)
        assert abs(x - 0.5) < 0.01
        assert abs(y - 0.5) < 0.01

    def test_top_left_corner(self):
        aim_config = {
            "pan": -90.0, "tilt": 45.0,
            "pan_min": -90.0, "pan_max": 90.0,
            "tilt_min": -45.0, "tilt_max": 45.0,
        }
        x, y = pan_tilt_to_crosshair(aim_config)
        assert abs(x - 0.05) < 0.01  # 5% margin like frontend
        assert abs(y - 0.05) < 0.01

    def test_bottom_right_corner(self):
        aim_config = {
            "pan": 90.0, "tilt": -45.0,
            "pan_min": -90.0, "pan_max": 90.0,
            "tilt_min": -45.0, "tilt_max": 45.0,
        }
        x, y = pan_tilt_to_crosshair(aim_config)
        assert abs(x - 0.95) < 0.01
        assert abs(y - 0.95) < 0.01

    def test_with_axis_inversion(self):
        aim_config = {
            "pan": 45.0, "tilt": 0.0,
            "pan_min": -90.0, "pan_max": 90.0,
            "tilt_min": -45.0, "tilt_max": 45.0,
            "pan_inverted": True, "tilt_inverted": False,
        }
        x, y = pan_tilt_to_crosshair(aim_config)
        assert x < 0.5

    def test_with_crosshair_offset(self):
        aim_config = {
            "pan": 0.0, "tilt": 0.0,
            "pan_min": -90.0, "pan_max": 90.0,
            "tilt_min": -45.0, "tilt_max": 45.0,
            "crosshair_offset_x_pct": 5.0,
            "crosshair_offset_y_pct": -3.0,
        }
        x, y = pan_tilt_to_crosshair(aim_config)
        assert abs(x - 0.55) < 0.01  # shifted right 5%
        assert abs(y - 0.47) < 0.01  # shifted up 3%

    def test_with_calibration_points(self):
        aim_config = {
            "pan": 0.0, "tilt": 10.0,
            "pan_min": -90.0, "pan_max": 90.0,
            "tilt_min": -45.0, "tilt_max": 45.0,
            "crosshair_calibration_enabled": True,
            "crosshair_calibration_points": [
                {"tilt": 0.0, "impact_x_pct": 48.0, "impact_y_pct": 60.0},
                {"tilt": 20.0, "impact_x_pct": 50.0, "impact_y_pct": 40.0},
            ],
        }
        x, y = pan_tilt_to_crosshair(aim_config)
        assert abs(x - 0.49) < 0.02
        assert abs(y - 0.50) < 0.02


class TestScoreTargetsByAccuracy:
    """Score targets based on crosshair-to-center distance."""

    def _target(self, bbox, name="chicken", roster_id=1, confidence=0.9):
        return {
            "bbox": bbox, "confidence": confidence,
            "roster_entry": {"id": roster_id, "name": name},
        }

    def test_direct_center_hit(self):
        target = self._target([100, 100, 300, 300])
        crosshair = (200 / 640, 200 / 480)
        frame_size = (640, 480)
        result = score_targets_by_accuracy(
            [target], crosshair, frame_size, hit_base=100, miss_points=5,
        )
        assert result["event"] == "hit"
        assert result["score"]["base"] == 100
        assert result["score"]["final"] >= 90

    def test_edge_of_bbox_hit(self):
        target = self._target([100, 100, 300, 300])
        crosshair = (295 / 640, 295 / 480)
        frame_size = (640, 480)
        result = score_targets_by_accuracy(
            [target], crosshair, frame_size, hit_base=100, miss_points=5,
        )
        assert result["event"] == "hit"
        assert result["score"]["final"] < 50

    def test_miss_outside_bbox(self):
        target = self._target([100, 100, 300, 300])
        crosshair = (50 / 640, 50 / 480)
        frame_size = (640, 480)
        result = score_targets_by_accuracy(
            [target], crosshair, frame_size, hit_base=100, miss_points=5,
        )
        assert result["event"] == "miss"
        assert result["score"]["final"] == 5

    def test_no_targets(self):
        result = score_targets_by_accuracy(
            [], (0.5, 0.5), (640, 480), hit_base=100, miss_points=5,
        )
        assert result["event"] == "miss"

    def test_multiple_targets_best_wins(self):
        t1 = self._target([0, 0, 100, 100], name="far", roster_id=1)
        t2 = self._target([200, 200, 400, 400], name="close", roster_id=2)
        crosshair = (300 / 640, 300 / 480)
        frame_size = (640, 480)
        result = score_targets_by_accuracy(
            [t1, t2], crosshair, frame_size, hit_base=100, miss_points=5,
        )
        assert result["event"] == "hit"
        assert result["target"]["name"] == "close"
        assert result["score"]["final"] >= 90

    def test_blocked_detection(self):
        t = self._target([100, 100, 300, 300])
        t["roster_entry"]["role"] = "block"
        crosshair = (200 / 640, 200 / 480)
        frame_size = (640, 480)
        result = score_targets_by_accuracy(
            [t], crosshair, frame_size, hit_base=100, miss_points=5,
        )
        assert result["event"] == "miss"


class TestComputeScore:
    """Apply multipliers to base score."""

    def test_no_multipliers(self):
        result = compute_score(75, {}, {})
        assert result == {"base": 75, "multipliers": {}, "final": 75}

    def test_with_streak_multiplier(self):
        scoring_config = {"multipliers": ["streak"]}
        result = compute_score(100, scoring_config, {"streak": 1.5})
        assert result["final"] == 150
        assert result["multipliers"] == {"streak": 1.5}

    def test_disallowed_multiplier_ignored(self):
        scoring_config = {"multipliers": ["streak"]}
        result = compute_score(100, scoring_config, {"streak": 1.5, "bounty": 2.0})
        assert result["final"] == 150
        assert "bounty" not in result["multipliers"]

    def test_negative_clamped_by_default(self):
        result = compute_score(-50, {}, {})
        assert result["final"] == 0

    def test_negative_allowed(self):
        scoring_config = {"allow_negative": True}
        result = compute_score(-50, scoring_config, {})
        assert result["final"] == -50
