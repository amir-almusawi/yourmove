"""Crosshair-centered shot scoring classifier utilities."""

from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)

_shot_model = None
_shot_classes = None


def load_shot_classifier(model_path: str | Path) -> list[str]:
    """Load a trained shot classifier model."""
    global _shot_model, _shot_classes

    import torch
    import torch.nn as nn
    from torchvision import models

    checkpoint = torch.load(str(model_path), map_location="cpu", weights_only=False)
    class_names = checkpoint["class_names"]
    num_classes = checkpoint["num_classes"]

    model = models.mobilenet_v3_small(weights=None)
    model.classifier[-1] = nn.Linear(model.classifier[-1].in_features, num_classes)
    model.load_state_dict(checkpoint["model_state_dict"])
    model.eval()

    _shot_model = model
    _shot_classes = class_names
    return class_names


def shot_classifier_loaded() -> bool:
    return _shot_model is not None and bool(_shot_classes)


def unload_shot_classifier() -> None:
    global _shot_model, _shot_classes
    _shot_model = None
    _shot_classes = None


def classify_crop(crop_img: np.ndarray) -> tuple[str | None, float]:
    """Classify a single crosshair-centered crop."""
    if _shot_model is None:
        return None, 0.0

    import torch
    from PIL import Image
    from torchvision import transforms

    transform = transforms.Compose([
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])

    resized = cv2.resize(crop_img, (224, 224))
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
    pil_img = Image.fromarray(rgb)
    tensor = transform(pil_img).unsqueeze(0)

    with torch.no_grad():
        outputs = _shot_model(tensor)
        probs = torch.softmax(outputs, dim=1)
        confidence, predicted = probs.max(1)

    class_name = _shot_classes[predicted.item()]
    return class_name, float(confidence.item())


def _draw_reticle(crop_img: np.ndarray) -> np.ndarray:
    """Overlay a simple reticle on the crop so the model sees the aim point."""
    out = crop_img.copy()
    h, w = out.shape[:2]
    cx = w // 2
    cy = h // 2
    outer = max(10, min(w, h) // 9)
    inner = max(4, outer // 3)
    color = (0, 255, 180)
    shadow = (16, 16, 16)

    cv2.line(out, (cx - outer, cy), (cx - inner, cy), shadow, 3, cv2.LINE_AA)
    cv2.line(out, (cx + inner, cy), (cx + outer, cy), shadow, 3, cv2.LINE_AA)
    cv2.line(out, (cx, cy - outer), (cx, cy - inner), shadow, 3, cv2.LINE_AA)
    cv2.line(out, (cx, cy + inner), (cx, cy + outer), shadow, 3, cv2.LINE_AA)
    cv2.circle(out, (cx, cy), inner, shadow, 2, cv2.LINE_AA)

    cv2.line(out, (cx - outer, cy), (cx - inner, cy), color, 1, cv2.LINE_AA)
    cv2.line(out, (cx + inner, cy), (cx + outer, cy), color, 1, cv2.LINE_AA)
    cv2.line(out, (cx, cy - outer), (cx, cy - inner), color, 1, cv2.LINE_AA)
    cv2.line(out, (cx, cy + inner), (cx, cy + outer), color, 1, cv2.LINE_AA)
    cv2.circle(out, (cx, cy), inner, color, 1, cv2.LINE_AA)
    return out


def _extract_square_crop(frame: np.ndarray, cx_px: float, cy_px: float, side_px: int) -> tuple[np.ndarray, list[int]]:
    h, w = frame.shape[:2]
    side_px = max(32, int(side_px))
    half = side_px // 2
    x1 = int(round(cx_px)) - half
    y1 = int(round(cy_px)) - half
    x2 = x1 + side_px
    y2 = y1 + side_px

    if x1 < 0:
        x2 -= x1
        x1 = 0
    if y1 < 0:
        y2 -= y1
        y1 = 0
    if x2 > w:
        shift = x2 - w
        x1 = max(0, x1 - shift)
        x2 = w
    if y2 > h:
        shift = y2 - h
        y1 = max(0, y1 - shift)
        y2 = h

    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        return frame.copy(), [0, 0, w, h]
    return crop, [int(x1), int(y1), int(x2), int(y2)]


def build_crosshair_crops(
    frame: np.ndarray,
    crosshair: tuple[float, float],
    cv_settings: dict | None = None,
) -> list[dict]:
    """Build square crops around the crosshair at configured scales."""
    cv_settings = cv_settings or {}
    frame_h, frame_w = frame.shape[:2]
    cx_px = float(max(0.0, min(1.0, crosshair[0]))) * frame_w
    cy_px = float(max(0.0, min(1.0, crosshair[1]))) * frame_h
    scales = cv_settings.get("cv_shot_crop_scales", [0.18, 0.28, 0.4])
    if not isinstance(scales, list) or not scales:
        scales = [0.18, 0.28, 0.4]
    min_dim = max(32, min(frame_w, frame_h))
    draw_reticle = bool(cv_settings.get("cv_shot_draw_reticle", True))

    crops = []
    for scale in scales:
        try:
            scale_value = float(scale)
        except (TypeError, ValueError):
            continue
        side_px = int(round(min_dim * max(0.05, scale_value)))
        crop_img, crop_box = _extract_square_crop(frame, cx_px, cy_px, side_px)
        crops.append({
            "scale": round(scale_value, 4),
            "crop_box": crop_box,
            "crop_img": crop_img,
            "model_img": _draw_reticle(crop_img) if draw_reticle else crop_img,
        })
    return crops


def classify_crosshair_shot(
    frame: np.ndarray,
    crosshair: tuple[float, float],
    scoring_config: dict | None = None,
    cv_settings: dict | None = None,
) -> dict | None:
    """Classify the local crop around the shot crosshair.

    Returns the best candidate and whether it cleared the configured threshold.
    """
    if not shot_classifier_loaded():
        return None

    scoring_config = scoring_config or {}
    cv_settings = cv_settings or {}
    threshold = float(cv_settings.get("cv_shot_classifier_confidence", 0.65))
    none_labels = {
        str(label).strip().lower()
        for label in cv_settings.get("cv_shot_none_labels", ["none", "background", "empty", "miss", "__ignore__"])
        if str(label).strip()
    }
    target_points = scoring_config.get("target_points") or {}
    hit_base = int(scoring_config.get("hit_base_points", 75))
    miss_points = int(scoring_config.get("miss_points", 5))

    best: dict | None = None
    for crop in build_crosshair_crops(frame, crosshair, cv_settings):
        label, confidence = classify_crop(crop["model_img"])
        if not label:
            continue
        normalized = label.strip().lower()
        if normalized in none_labels:
            points = miss_points
            kind = "none"
        else:
            points = int(target_points.get(label, hit_base))
            kind = "negative" if points < 0 else "positive"
        candidate = {
            "label": label,
            "confidence": round(confidence, 4),
            "crop_box": crop["crop_box"],
            "scale": crop["scale"],
            "points": int(points),
            "kind": kind,
            "accepted": confidence >= threshold,
        }
        if best is None:
            best = candidate
            continue
        best_priority = 0 if best["kind"] == "none" else 1
        cand_priority = 0 if candidate["kind"] == "none" else 1
        if cand_priority > best_priority or (cand_priority == best_priority and candidate["confidence"] > best["confidence"]):
            best = candidate

    return best
