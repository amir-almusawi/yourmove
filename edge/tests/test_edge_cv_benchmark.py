import numpy as np
import pytest


class TestDetermineTier:
    def test_zero_fps_tier_0(self):
        from edge.edge_cv_benchmark import determine_tier
        assert determine_tier(0.0) == 0

    def test_low_fps_tier_0(self):
        from edge.edge_cv_benchmark import determine_tier
        assert determine_tier(0.5) == 0

    def test_medium_fps_tier_1(self):
        from edge.edge_cv_benchmark import determine_tier
        assert determine_tier(5.0) == 1

    def test_boundary_fps_tier_1(self):
        from edge.edge_cv_benchmark import determine_tier
        assert determine_tier(1.0) == 1

    def test_high_fps_tier_2(self):
        from edge.edge_cv_benchmark import determine_tier
        assert determine_tier(15.0) == 2

    def test_boundary_tier_2(self):
        from edge.edge_cv_benchmark import determine_tier
        assert determine_tier(10.1) == 2


class TestRunBenchmark:
    def test_benchmark_returns_result(self):
        from edge.edge_cv_benchmark import run_benchmark
        result = run_benchmark(model_name="yolov8n", num_frames=3)
        assert "fps" in result
        assert "tier" in result
        assert "model_name" in result
        assert "hardware_summary" in result
        assert result["fps"] > 0
        assert result["tier"] in (0, 1, 2)
        assert result["model_name"] == "yolov8n"
