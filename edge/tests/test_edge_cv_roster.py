# edge/tests/test_edge_cv_roster.py
import numpy as np
import pytest


SAMPLE_ROSTER = [
    {"id": 1, "name": "Hank", "color_hex": "#8B4513", "role": "target", "detection_class": "bird"},
    {"id": 2, "name": "Clyde", "color_hex": "#FFFFFF", "role": "target", "detection_class": "bird"},
    {"id": 3, "name": "Operator", "color_hex": "#0000FF", "role": "block", "detection_class": "person"},
]


class TestHexToRgb:
    def test_valid_hex(self):
        from edge.edge_cv_roster import hex_to_rgb
        assert hex_to_rgb("#8B4513") == (139, 69, 19)

    def test_white(self):
        from edge.edge_cv_roster import hex_to_rgb
        assert hex_to_rgb("#FFFFFF") == (255, 255, 255)

    def test_empty_returns_none(self):
        from edge.edge_cv_roster import hex_to_rgb
        assert hex_to_rgb("") is None


class TestDominantColor:
    def test_solid_red_crop(self):
        from edge.edge_cv_roster import extract_dominant_color
        red_patch = np.full((50, 50, 3), [0, 0, 255], dtype=np.uint8)  # BGR
        r, g, b = extract_dominant_color(red_patch)
        assert r > 200
        assert g < 50
        assert b < 50

    def test_solid_white_crop(self):
        from edge.edge_cv_roster import extract_dominant_color
        white_patch = np.full((50, 50, 3), [255, 255, 255], dtype=np.uint8)
        r, g, b = extract_dominant_color(white_patch)
        assert r > 200 and g > 200 and b > 200


class TestMatchDetectionToRoster:
    def test_matches_brown_to_hank(self):
        from edge.edge_cv_roster import match_detection_to_roster
        brown_frame = np.full((240, 320, 3), [19, 69, 139], dtype=np.uint8)  # BGR for #8B4513
        detection = {"class": "bird", "bbox": [50, 50, 150, 150], "confidence": 0.9}
        match = match_detection_to_roster(brown_frame, detection, SAMPLE_ROSTER)
        assert match is not None
        assert match["name"] == "Hank"

    def test_matches_white_to_clyde(self):
        from edge.edge_cv_roster import match_detection_to_roster
        white_frame = np.full((240, 320, 3), [255, 255, 255], dtype=np.uint8)
        detection = {"class": "bird", "bbox": [50, 50, 150, 150], "confidence": 0.9}
        match = match_detection_to_roster(white_frame, detection, SAMPLE_ROSTER)
        assert match is not None
        assert match["name"] == "Clyde"

    def test_no_match_for_wrong_class(self):
        from edge.edge_cv_roster import match_detection_to_roster
        brown_frame = np.full((240, 320, 3), [19, 69, 139], dtype=np.uint8)
        detection = {"class": "car", "bbox": [50, 50, 150, 150], "confidence": 0.9}
        match = match_detection_to_roster(brown_frame, detection, SAMPLE_ROSTER)
        assert match is None

    def test_block_role_detected(self):
        from edge.edge_cv_roster import match_detection_to_roster
        blue_frame = np.full((240, 320, 3), [255, 0, 0], dtype=np.uint8)  # BGR for blue
        detection = {"class": "person", "bbox": [50, 50, 150, 150], "confidence": 0.9}
        match = match_detection_to_roster(blue_frame, detection, SAMPLE_ROSTER)
        assert match is not None
        assert match["name"] == "Operator"
        assert match["role"] == "block"


class TestColorDistance:
    def test_same_color_zero_distance(self):
        from edge.edge_cv_roster import color_distance
        assert color_distance((255, 0, 0), (255, 0, 0)) == 0.0

    def test_black_white_max_distance(self):
        from edge.edge_cv_roster import color_distance
        d = color_distance((0, 0, 0), (255, 255, 255))
        assert d > 400
