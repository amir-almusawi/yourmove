"""Color-signature matching: map YOLO detections to operator-defined roster entries."""
from __future__ import annotations
import math

import cv2
import numpy as np


def hex_to_rgb(hex_color: str) -> tuple[int, int, int] | None:
    hex_color = hex_color.strip().lstrip("#")
    if len(hex_color) != 6:
        return None
    return (int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16))


def extract_dominant_color(crop: np.ndarray) -> tuple[int, int, int]:
    resized = cv2.resize(crop, (16, 16))
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
    avg = rgb.reshape(-1, 3).mean(axis=0)
    return (int(avg[0]), int(avg[1]), int(avg[2]))


def color_distance(c1: tuple[int, int, int], c2: tuple[int, int, int]) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(c1, c2)))


MAX_COLOR_DISTANCE = 180.0


def match_detection_to_roster(
    frame: np.ndarray,
    detection: dict,
    roster: list[dict],
) -> dict | None:
    det_class = detection["class"]
    candidates = [r for r in roster if r.get("detection_class") == det_class]
    if not candidates:
        candidates = roster

    x1, y1, x2, y2 = detection["bbox"]
    h, w = frame.shape[:2]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    if x2 <= x1 or y2 <= y1:
        return None

    crop = frame[y1:y2, x1:x2]
    dominant = extract_dominant_color(crop)

    best_match = None
    best_distance = MAX_COLOR_DISTANCE
    for entry in candidates:
        entry_rgb = hex_to_rgb(entry.get("color_hex", ""))
        if entry_rgb is None:
            if best_match is None:
                best_match = entry
            continue
        d = color_distance(dominant, entry_rgb)
        if d < best_distance:
            best_distance = d
            best_match = entry

    return best_match
