"""Edge-side accuracy-based scoring from crosshair position vs target bounding boxes."""
from __future__ import annotations
import math


def pan_tilt_to_crosshair(aim_config: dict) -> tuple[float, float]:
    """Convert turret pan/tilt to normalized (0-1) crosshair position on frame.

    Mirrors the frontend updateCrosshair() logic in node.js:3498-3531.
    Returns (x, y) where (0,0) is top-left and (1,1) is bottom-right.
    """
    pan = aim_config.get("pan", 0.0)
    tilt = aim_config.get("tilt", 0.0)
    pan_min = aim_config.get("pan_min", -90.0)
    pan_max = aim_config.get("pan_max", 90.0)
    tilt_min = aim_config.get("tilt_min", -45.0)
    tilt_max = aim_config.get("tilt_max", 45.0)

    if aim_config.get("pan_inverted"):
        pan = -pan
        pan_min, pan_max = -pan_max, -pan_min
    if aim_config.get("tilt_inverted"):
        tilt = -tilt
        tilt_min, tilt_max = -tilt_max, -tilt_min

    cal_enabled = aim_config.get("crosshair_calibration_enabled", False)
    cal_points = aim_config.get("crosshair_calibration_points")

    if cal_enabled and cal_points and len(cal_points) > 0 and "impact_y_pct" in cal_points[0]:
        sorted_pts = sorted(cal_points, key=lambda p: p["tilt"])
        if tilt <= sorted_pts[0]["tilt"]:
            pt = sorted_pts[0]
        elif tilt >= sorted_pts[-1]["tilt"]:
            pt = sorted_pts[-1]
        else:
            pt = sorted_pts[0]
            for i in range(len(sorted_pts) - 1):
                if sorted_pts[i]["tilt"] <= tilt <= sorted_pts[i + 1]["tilt"]:
                    f = (tilt - sorted_pts[i]["tilt"]) / max(1e-9, sorted_pts[i + 1]["tilt"] - sorted_pts[i]["tilt"])
                    pt = {
                        "impact_x_pct": sorted_pts[i]["impact_x_pct"] + f * (sorted_pts[i + 1]["impact_x_pct"] - sorted_pts[i]["impact_x_pct"]),
                        "impact_y_pct": sorted_pts[i]["impact_y_pct"] + f * (sorted_pts[i + 1]["impact_y_pct"] - sorted_pts[i]["impact_y_pct"]),
                    }
                    break
        return (pt["impact_x_pct"] / 100.0, pt["impact_y_pct"] / 100.0)

    offset_x = aim_config.get("crosshair_offset_x_pct", 0.0)
    offset_y = aim_config.get("crosshair_offset_y_pct", 0.0)

    pan_span = max(1.0, pan_max - pan_min)
    tilt_span = max(1.0, tilt_max - tilt_min)

    x_pct = 5.0 + (((pan - pan_min) / pan_span) * 90.0) + offset_x
    y_pct = 95.0 - (((tilt - tilt_min) / tilt_span) * 90.0) + offset_y

    x_pct = max(0.0, min(100.0, x_pct))
    y_pct = max(0.0, min(100.0, y_pct))

    return (x_pct / 100.0, y_pct / 100.0)


def score_targets_by_accuracy(
    targets: list[dict],
    crosshair: tuple[float, float],
    frame_size: tuple[int, int],
    hit_base: int = 75,
    miss_points: int = 5,
    target_points: dict | None = None,
) -> dict:
    """Score all targets by crosshair accuracy. Returns best hit or miss.

    crosshair: (x, y) normalized 0-1
    targets: list of detection dicts with 'bbox' [x1,y1,x2,y2] in pixels
    frame_size: (width, height) of the detection frame
    """
    if not targets:
        return {
            "event": "miss",
            "reaction": "miss",
            "target": {},
            "score": {"base": miss_points, "multipliers": {}, "final": miss_points},
            "accuracy": 0.0,
        }

    fw, fh = frame_size
    cx_px = crosshair[0] * fw
    cy_px = crosshair[1] * fh

    best_hit = None

    for t in targets:
        role = (t.get("roster_entry") or {}).get("role", "target")
        if role != "target":
            continue
        bbox = t.get("bbox")
        if not bbox or len(bbox) < 4:
            continue

        x1, y1, x2, y2 = bbox[0], bbox[1], bbox[2], bbox[3]
        if cx_px < x1 or cx_px > x2 or cy_px < y1 or cy_px > y2:
            continue

        center_x = (x1 + x2) / 2.0
        center_y = (y1 + y2) / 2.0
        half_w = (x2 - x1) / 2.0
        half_h = (y2 - y1) / 2.0
        half_diag = math.sqrt(half_w ** 2 + half_h ** 2)

        dist = math.sqrt((cx_px - center_x) ** 2 + (cy_px - center_y) ** 2)
        accuracy = max(0.0, 1.0 - (dist / max(half_diag, 1e-6)))
        roster = t.get("roster_entry") or {}
        target_name = roster.get("name", "")
        target_base = hit_base
        if target_points and target_name in target_points:
            target_base = int(target_points[target_name])
        final = int(target_base * accuracy)

        if best_hit is None or accuracy > best_hit["accuracy"]:
            best_hit = {
                "event": "hit",
                "reaction": "hit",
                "target": {
                    "roster_id": roster.get("id"),
                    "name": roster.get("name", ""),
                    "confidence": t.get("confidence", 0),
                },
                "score": {"base": target_base, "multipliers": {}, "final": final},
                "accuracy": accuracy,
                "bbox": bbox,
            }

    if best_hit:
        return best_hit

    return {
        "event": "miss",
        "reaction": "miss",
        "target": {},
        "score": {"base": miss_points, "multipliers": {}, "final": miss_points},
        "accuracy": 0.0,
    }


def compute_score(
    base: int,
    scoring_config: dict,
    multiplier_values: dict,
) -> dict:
    """Apply template multipliers to a base score."""
    allow_negative = scoring_config.get("allow_negative", False)
    allowed = set(scoring_config.get("multipliers", []))

    applied = {}
    for name, value in multiplier_values.items():
        if name in allowed:
            applied[name] = value

    final = float(base)
    for mult in applied.values():
        final *= mult
    final = int(final)

    if not allow_negative and final < 0:
        final = 0

    return {"base": base, "multipliers": applied, "final": final}
