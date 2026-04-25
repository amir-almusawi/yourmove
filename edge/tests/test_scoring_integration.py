"""Integration test: full scoring flow from cv-state + aim config to game event."""
from edge.edge_cv_runtime import evaluate_interaction


class TestEvaluateInteractionAccuracy:
    def _cv_state(self, targets=None, frame_size=(640, 480)):
        return {
            "targets": targets or [],
            "all_detections": [],
            "blocked": False,
            "detection_count": len(targets or []),
            "frame_size": list(frame_size),
            "tier": 1,
        }

    def _target(self, bbox, name="Browney", roster_id=1):
        return {
            "class": "bird",
            "bbox": bbox,
            "confidence": 0.92,
            "roster_entry": {"id": roster_id, "name": name, "role": "target"},
            "track_id": 1,
        }

    def _game_config(self, hit_base=100, miss_points=5):
        return {
            "template_type": "blast",
            "template_config": {
                "type": "blast",
                "scoring": {
                    "hit_base_points": hit_base,
                    "miss_points": miss_points,
                    "multipliers": [],
                    "allow_negative": False,
                },
            },
        }

    def _aim_config(self, pan=0.0, tilt=0.0):
        return {
            "pan": pan, "tilt": tilt,
            "pan_min": -90.0, "pan_max": 90.0,
            "tilt_min": -45.0, "tilt_max": 45.0,
        }

    def test_center_hit_scores_high(self):
        target = self._target([220, 140, 420, 340])
        cv_state = self._cv_state([target])
        aim = self._aim_config(pan=0.0, tilt=0.0)
        result = evaluate_interaction(cv_state, self._game_config(), 1, aim_config=aim)
        assert result is not None
        assert result["event"] == "hit"
        assert result["score"]["final"] >= 80
        assert result["target"]["name"] == "Browney"

    def test_no_targets_is_miss(self):
        cv_state = self._cv_state([])
        aim = self._aim_config()
        result = evaluate_interaction(cv_state, self._game_config(), 1, aim_config=aim)
        assert result["event"] == "miss"
        assert result["score"]["final"] == 5

    def test_crosshair_outside_target_is_miss(self):
        target = self._target([0, 0, 100, 100])
        cv_state = self._cv_state([target])
        aim = self._aim_config(pan=80.0, tilt=-40.0)
        result = evaluate_interaction(cv_state, self._game_config(), 1, aim_config=aim)
        assert result["event"] == "miss"

    def test_blocked_returns_zero(self):
        cv_state = self._cv_state([])
        cv_state["blocked"] = True
        result = evaluate_interaction(cv_state, self._game_config(), 1)
        assert result["event"] == "blocked"
        assert result["score"]["final"] == 0

    def test_no_aim_config_defaults_to_center(self):
        target = self._target([220, 140, 420, 340])
        cv_state = self._cv_state([target])
        result = evaluate_interaction(cv_state, self._game_config(), 1, aim_config=None)
        assert result["event"] == "hit"
        assert result["score"]["final"] >= 80

    def test_custom_base_points(self):
        target = self._target([220, 140, 420, 340])
        cv_state = self._cv_state([target])
        aim = self._aim_config()
        result = evaluate_interaction(cv_state, self._game_config(hit_base=200), 1, aim_config=aim)
        assert result["score"]["base"] == 200
        assert result["score"]["final"] >= 160
