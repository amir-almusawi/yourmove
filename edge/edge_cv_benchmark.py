"""Hardware benchmark to determine CV capability tier."""
from __future__ import annotations
import platform
import time

import numpy as np


def determine_tier(fps: float) -> int:
    if fps > 10:
        return 2
    if fps >= 1:
        return 1
    return 0


def _hardware_summary() -> str:
    import os
    arch = platform.machine()
    try:
        mem_bytes = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        mem_gb = round(mem_bytes / (1024 ** 3), 1)
    except Exception:
        mem_gb = "?"
    gpu = ""
    try:
        import torch
        if torch.cuda.is_available():
            gpu = f" + {torch.cuda.get_device_name(0)}"
    except Exception:
        pass
    return f"{arch}, {mem_gb}GB RAM{gpu}"


def run_benchmark(model_name: str = "yolov8s", num_frames: int = 10) -> dict:
    from ultralytics import YOLO
    model = YOLO(f"{model_name}.pt")

    test_frame = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)

    # Warm up
    model(test_frame, verbose=False)

    start = time.monotonic()
    for _ in range(num_frames):
        model(test_frame, verbose=False)
    elapsed = time.monotonic() - start

    fps = num_frames / elapsed if elapsed > 0 else 0.0
    tier = determine_tier(fps)

    return {
        "fps": round(fps, 2),
        "tier": tier,
        "cv_tier": tier,
        "inference_fps": round(fps, 2),
        "model_name": model_name,
        "hardware_summary": _hardware_summary(),
    }
