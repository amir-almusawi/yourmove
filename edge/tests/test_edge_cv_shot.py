from unittest.mock import MagicMock, patch


def test_classify_crosshair_shot_prefers_positive_over_none():
    from edge.edge_cv_shot import classify_crosshair_shot

    fake_crops = [
        {"scale": 0.18, "crop_box": [10, 10, 50, 50], "crop_img": object(), "model_img": object()},
        {"scale": 0.28, "crop_box": [20, 20, 70, 70], "crop_img": object(), "model_img": object()},
    ]
    with patch("edge.edge_cv_shot.shot_classifier_loaded", return_value=True), \
         patch("edge.edge_cv_shot.build_crosshair_crops", return_value=fake_crops), \
         patch("edge.edge_cv_shot.classify_crop", side_effect=[("background", 0.97), ("chicken", 0.83)]):
        result = classify_crosshair_shot(
            frame=object(),
            crosshair=(0.5, 0.5),
            scoring_config={"target_points": {"chicken": 100}, "hit_base_points": 75, "miss_points": 5},
            cv_settings={"cv_shot_classifier_confidence": 0.65},
        )

    assert result is not None
    assert result["label"] == "chicken"
    assert result["points"] == 100
    assert result["accepted"] is True


def test_classify_crosshair_shot_rejects_low_confidence():
    from edge.edge_cv_shot import classify_crosshair_shot

    fake_crops = [
        {"scale": 0.18, "crop_box": [10, 10, 50, 50], "crop_img": object(), "model_img": object()},
    ]
    with patch("edge.edge_cv_shot.shot_classifier_loaded", return_value=True), \
         patch("edge.edge_cv_shot.build_crosshair_crops", return_value=fake_crops), \
         patch("edge.edge_cv_shot.classify_crop", return_value=("cat", 0.42)):
        result = classify_crosshair_shot(
            frame=object(),
            crosshair=(0.5, 0.5),
            scoring_config={"target_points": {"cat": -50}, "hit_base_points": 75, "miss_points": 5},
            cv_settings={"cv_shot_classifier_confidence": 0.65},
        )

    assert result is not None
    assert result["label"] == "cat"
    assert result["points"] == -50
    assert result["accepted"] is False


def test_extract_and_upload_shot_crops_posts_payload():
    from edge.edge_cv_runtime import extract_and_upload_shot_crops
    from pathlib import Path
    import tempfile

    class DummyCrop:
        size = 1

    fake_crops = [
        {"scale": 0.18, "crop_box": [10, 10, 50, 50], "crop_img": DummyCrop(), "model_img": DummyCrop()},
        {"scale": 0.28, "crop_box": [20, 20, 70, 70], "crop_img": DummyCrop(), "model_img": DummyCrop()},
    ]
    fake_buffer = MagicMock()
    fake_buffer.tobytes.return_value = b"jpg"

    class DummyFrame:
        shape = (360, 640, 3)

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("edge.edge_cv_shot.build_crosshair_crops", return_value=fake_crops), \
             patch("edge.edge_cv_shot.shot_classifier_loaded", return_value=True), \
             patch("edge.edge_cv_shot.classify_crop", side_effect=[("chicken", 0.91), ("background", 0.84)]), \
             patch("cv2.resize", side_effect=lambda img, _size: img), \
             patch("cv2.imencode", return_value=(True, fake_buffer)), \
             patch("edge.edge_cv_runtime.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_post.return_value = mock_resp
            stats = extract_and_upload_shot_crops(
                frame=DummyFrame(),
                crosshair=(0.5, 0.5),
                node_slug="test-node",
                base_url="https://example.com",
                auth_token="token",
                crops_dir=Path(tmpdir),
                interaction_id=123,
                ssl_verify=False,
                cv_settings={},
            )

        assert stats["shot_crops_captured"] == 2
        metadata_calls = [c for c in mock_post.call_args_list if "cv-crops" in c.args[0]]
        assert metadata_calls
        payload = metadata_calls[0].kwargs["json"]["crops"]
        assert payload[0]["yolo_class"] == "__shot__"
        assert payload[0]["source"] == "shot"
        assert payload[0]["interaction_id"] == 123
