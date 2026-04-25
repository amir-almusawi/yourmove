"""Edge CV runtime — detection loop, scoring, and game event posting."""
from __future__ import annotations
import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

# Ensure parent dir is on sys.path so 'from edge.X' imports work
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

log = logging.getLogger(__name__)

SHUTDOWN = False


def _handle_shutdown(signum, _frame):
    global SHUTDOWN
    SHUTDOWN = True
    log.info("CV runtime shutdown requested (signal=%s)", signum)


def fetch_game_config(
    base_url: str, node_slug: str, auth_token: str, timeout: int = 10, ssl_verify: bool = True,
) -> dict | None:
    try:
        resp = requests.get(
            f"{base_url.rstrip('/')}/api/nodes/{node_slug}/operator/stream-status",
            headers={"Authorization": f"Bearer {auth_token}"},
            timeout=timeout,
            verify=ssl_verify,
        )
        if not resp.ok:
            log.error("Failed to fetch stream-status: HTTP %s", resp.status_code)
            return None
        data = resp.json()
        return data.get("game_config")
    except Exception as exc:
        log.error("Failed to fetch game config: %s", exc)
        return None


def post_game_event(
    base_url: str, node_slug: str, auth_token: str,
    event_payload: dict, timeout: int = 10, ssl_verify: bool = True,
) -> bool:
    try:
        resp = requests.post(
            f"{base_url.rstrip('/')}/api/nodes/{node_slug}/operator/game-events",
            headers={"Authorization": f"Bearer {auth_token}"},
            json=event_payload,
            timeout=timeout,
            verify=ssl_verify,
        )
        return resp.ok
    except Exception as exc:
        log.error("Failed to post game event: %s", exc)
        return False


def report_capability(
    base_url: str, node_slug: str, auth_token: str,
    benchmark_result: dict, timeout: int = 10, ssl_verify: bool = True,
) -> bool:
    try:
        resp = requests.post(
            f"{base_url.rstrip('/')}/api/nodes/{node_slug}/operator/edge-capability",
            headers={"Authorization": f"Bearer {auth_token}"},
            json=benchmark_result,
            timeout=timeout,
            verify=ssl_verify,
        )
        return resp.ok
    except Exception as exc:
        log.error("Failed to report capability: %s", exc)
        return False


def build_overlay_events(game_events: list[dict], clip_start_ms: int) -> list[dict]:
    overlay_events = []
    for evt in game_events:
        ts_ms = evt.get("timestamp_ms", 0)
        offset_s = max(0, (ts_ms - clip_start_ms) / 1000.0)
        target_name = (evt.get("target") or {}).get("name", "")
        score = (evt.get("score") or {}).get("final", 0)
        reaction = evt.get("reaction", "")
        text = f"+{score} {target_name} {reaction}!" if score >= 0 else f"{score} {target_name} {reaction}!"
        overlay_events.append({
            "time_offset_s": round(offset_s, 2),
            "text": text.strip(),
            "type": "score_popup",
        })
    return overlay_events


def filter_detections(
    detections: list[dict],
    cv_settings: dict,
    frame_shape: tuple[int, int],
    has_classifier: bool = False,
) -> list[dict]:
    """Apply exclusion pipeline: confidence threshold, exclusion zones, and optionally YOLO class ignore list."""
    threshold = cv_settings.get("cv_confidence_threshold", 30) / 100.0
    exclusion_zones = cv_settings.get("cv_exclusion_zones", [])
    fh, fw = frame_shape

    allowed_classes = cv_settings.get("cv_allowed_classes", [])
    allowed_set = set(allowed_classes) if allowed_classes else None
    ignored_classes = set(cv_settings.get("cv_ignored_classes", []))

    filtered = []
    for det in detections:
        if allowed_set and det["class"] not in allowed_set:
            continue
        if det["class"] in ignored_classes:
            continue
        if det["confidence"] < threshold:
            continue
        if exclusion_zones:
            bbox = det["bbox"]
            cx = ((bbox[0] + bbox[2]) / 2) / fw
            cy = ((bbox[1] + bbox[3]) / 2) / fh
            in_zone = False
            for zone in exclusion_zones:
                if (zone["x"] <= cx <= zone["x"] + zone["w"] and
                        zone["y"] <= cy <= zone["y"] + zone["h"]):
                    in_zone = True
                    break
            if in_zone:
                continue
        filtered.append(det)
    return filtered


def scale_bbox(bbox: list[int], src_shape: tuple[int, int], dst_w: int, dst_h: int) -> list[int]:
    """Scale bounding box from source frame coords to destination resolution."""
    src_h, src_w = src_shape
    sx = dst_w / src_w
    sy = dst_h / src_h
    return [int(bbox[0] * sx), int(bbox[1] * sy), int(bbox[2] * sx), int(bbox[3] * sy)]


_classifier_model = None
_classifier_classes = None
_classifier_version = None


def load_classifier(model_path):
    """Load a trained crop classifier model."""
    global _classifier_model, _classifier_classes, _classifier_version
    import torch
    from torchvision import models
    import torch.nn as nn

    checkpoint = torch.load(str(model_path), map_location="cpu", weights_only=False)
    class_names = checkpoint["class_names"]
    num_classes = checkpoint["num_classes"]

    model = models.mobilenet_v3_small(weights=None)
    model.classifier[-1] = nn.Linear(model.classifier[-1].in_features, num_classes)
    model.load_state_dict(checkpoint["model_state_dict"])
    model.eval()

    _classifier_model = model
    _classifier_classes = class_names
    return class_names


def classify_crop(crop_img):
    """Classify a single crop image. Returns (class_name, confidence)."""
    if _classifier_model is None:
        return None, 0.0
    import torch
    from torchvision import transforms
    import cv2

    transform = transforms.Compose([
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])

    resized = cv2.resize(crop_img, (224, 224))
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
    from PIL import Image
    pil_img = Image.fromarray(rgb)
    tensor = transform(pil_img).unsqueeze(0)

    with torch.no_grad():
        outputs = _classifier_model(tensor)
        probs = torch.softmax(outputs, dim=1)
        confidence, predicted = probs.max(1)

    class_name = _classifier_classes[predicted.item()]
    return class_name, confidence.item()


_track_labels: dict[int, tuple[str, float]] = {}
_track_bboxes: dict[int, list[int]] = {}
_rtsp_source = None

def run_detection_loop(
    segment_dir: Path,
    game_config: dict,
    tier: int,
    detection_interval_s: float = 0.5,
    rtsp_url: str | None = None,
) -> dict | None:
    """Run one detection pass with tracking. Uses RTSP if available, falls back to segments."""
    global _rtsp_source
    if tier < 1:
        return None

    from edge.edge_cv_detect import decode_latest_segment, run_tracking, RTSPFrameSource
    from edge.edge_cv_roster import match_detection_to_roster

    frame = None
    if rtsp_url:
        if _rtsp_source is None:
            _rtsp_source = RTSPFrameSource(rtsp_url)
            if not _rtsp_source.start():
                _rtsp_source = None
        if _rtsp_source:
            frame = _rtsp_source.grab_frame()

    if frame is None:
        frames = decode_latest_segment(segment_dir, max_frames=1)
        if not frames:
            return None
        frame = frames[0]
    template_config = game_config.get("template_config", {})
    model_name = template_config.get("detection", {}).get("model", "yolov8n")
    roster = game_config.get("roster", [])

    cv_settings = game_config.get("cv_settings", {})
    yolo_conf = cv_settings.get("cv_confidence_threshold", 25) / 100.0
    detections = run_tracking(frame, model_name=model_name, conf=max(0.05, yolo_conf))
    detections = filter_detections(detections, cv_settings, frame.shape[:2], has_classifier=_classifier_model is not None)

    from edge.edge_cv_detect import get_custom_yolo_classes
    custom_yolo_classes = get_custom_yolo_classes()
    if custom_yolo_classes:
        for det in detections:
            if det["class"] in custom_yolo_classes:
                det["classifier_label"] = det["class"]
                det["classifier_confidence"] = det["confidence"]

    targets_in_frame = []
    blocked = False
    for det in detections:
        matched = match_detection_to_roster(frame, det, roster)
        if matched:
            entry = {**det, "roster_entry": matched}
            if matched.get("role") == "block":
                blocked = True
            elif matched.get("role") == "target":
                targets_in_frame.append(entry)

    if _classifier_model is not None:
        classifier_conf = cv_settings.get("cv_classifier_confidence", 0.8)
        class_conf_overrides = cv_settings.get("cv_class_confidence_overrides", {})
        seen_track_ids = set()
        for det in detections:
            track_id = det.get("track_id")
            bbox = det["bbox"]
            x1, y1 = max(0, bbox[0]), max(0, bbox[1])
            x2, y2 = min(frame.shape[1], bbox[2]), min(frame.shape[0], bbox[3])
            if x2 - x1 < 10 or y2 - y1 < 10:
                continue

            if det.get("classifier_label"):
                if track_id is not None:
                    _track_labels[track_id] = (det["classifier_label"], det["classifier_confidence"])
                    seen_track_ids.add(track_id)
                continue

            if track_id is not None and track_id in _track_labels:
                cached_label, cached_conf = _track_labels[track_id]
                det["classifier_label"] = cached_label
                det["classifier_confidence"] = cached_conf
                seen_track_ids.add(track_id)
                continue

            crop = frame[y1:y2, x1:x2]
            cls_name, cls_conf = classify_crop(crop)
            if cls_name:
                threshold = class_conf_overrides.get(cls_name, classifier_conf)
                if cls_conf >= threshold:
                    det["classifier_label"] = cls_name
                    det["classifier_confidence"] = round(cls_conf, 4)
                    if track_id is not None:
                        _track_labels[track_id] = (cls_name, round(cls_conf, 4))
                        seen_track_ids.add(track_id)
                else:
                    det["classifier_label_candidate"] = cls_name
                    det["classifier_confidence"] = round(cls_conf, 4)

        current_track_ids = {d.get("track_id") for d in detections if d.get("track_id") is not None}
        for det in detections:
            tid = det.get("track_id")
            if tid is not None:
                _track_bboxes[tid] = det["bbox"]

        lost_track_crops = []
        stale = [tid for tid in _track_labels if tid not in seen_track_ids
                 and tid not in current_track_ids]
        for tid in stale:
            label, conf = _track_labels[tid]
            if label != "__ignore__" and tid in _track_bboxes:
                lost_track_crops.append({
                    "class": "lost_track",
                    "bbox": _track_bboxes[tid],
                    "confidence": 0.0,
                    "classifier_label_candidate": label,
                    "classifier_confidence": conf,
                    "track_id": tid,
                    "lost_track": True,
                })
            del _track_labels[tid]
            _track_bboxes.pop(tid, None)

        targets_in_frame = [t for t in targets_in_frame if t.get("classifier_label") != "__ignore__"]

    return {
        "targets": targets_in_frame,
        "all_detections": detections,
        "lost_track_crops": lost_track_crops if _classifier_model is not None else [],
        "blocked": blocked,
        "frame_shape": frame.shape[:2],
        "detection_count": len(detections),
        "timestamp_ms": int(time.time() * 1000),
        "frame": frame,
    }


def evaluate_interaction(
    detection_state: dict | None,
    game_config: dict,
    interaction_id: int,
    aim_config: dict | None = None,
) -> dict | None:
    """After a fire event, evaluate what happened and compute a game event."""
    if not detection_state:
        return None

    template_config = game_config.get("template_config", {})
    scoring_config = template_config.get("scoring", {})
    template_type = template_config.get("type", game_config.get("template_type", "blast"))
    targets = detection_state.get("targets", [])

    if detection_state.get("blocked"):
        return {
            "interaction_id": interaction_id,
            "template_type": template_type,
            "event": "blocked",
            "reaction": "absent",
            "target": {},
            "score": {"base": 0, "multipliers": {}, "final": 0},
            "detection": {"tier": 0},
        }

    hit_base = scoring_config.get("hit_base_points", 75)
    miss_points = scoring_config.get("miss_points", 5)

    if aim_config and ("pan" in aim_config):
        from edge.edge_cv_score import pan_tilt_to_crosshair
        crosshair = pan_tilt_to_crosshair(aim_config)
    else:
        crosshair = (0.5, 0.5)

    # frame_size/source_resolution from cv-state is (h, w) per OpenCV convention;
    # scoring expects (width, height), so swap.
    _fs = detection_state.get("frame_size") or detection_state.get("source_resolution") or (480, 640)
    if isinstance(_fs, dict):
        frame_size = (_fs.get("width", 640), _fs.get("height", 480))
    elif isinstance(_fs, (list, tuple)) and len(_fs) >= 2:
        frame_size = (int(_fs[1]), int(_fs[0]))
    else:
        frame_size = (640, 480)

    target_points = scoring_config.get("target_points")

    from edge.edge_cv_score import score_targets_by_accuracy, compute_score
    result = score_targets_by_accuracy(
        targets, crosshair, frame_size,
        hit_base=hit_base, miss_points=miss_points,
        target_points=target_points,
    )

    final_score = compute_score(
        result["score"]["final"], scoring_config, {},
    )

    event_data = {
        "interaction_id": interaction_id,
        "template_type": template_type,
        "event": result["event"],
        "reaction": result["reaction"],
        "target": result.get("target", {}),
        "score": final_score,
        "detection": {
            "tier": detection_state.get("tier", 1),
            "bbox": result.get("bbox"),
        },
        "accuracy": round(result.get("accuracy", 0.0), 3),
        "timestamp_ms": int(time.time() * 1000),
    }

    if result["event"] == "hit":
        log.info(
            "SCORE: %s %s accuracy=%.0f%% score=%d",
            result["target"].get("name", "?"),
            result["event"],
            result.get("accuracy", 0) * 100,
            final_score["final"],
        )
    else:
        log.info("SCORE: miss score=%d", final_score["final"])

    return event_data


from edge.edge_cv_embed import compute_embedding, embedding_to_b64, EmbeddingCache

_embedding_caches: dict[str, EmbeddingCache] = {}
EMBEDDING_CACHE_MAX = 200
EMBEDDING_DEDUP_THRESHOLD = 0.88

_MIN_CROP_INTERVAL = 0.0


def _get_embedding_cache(node_slug: str) -> EmbeddingCache:
    if node_slug not in _embedding_caches:
        _embedding_caches[node_slug] = EmbeddingCache(
            max_size=EMBEDDING_CACHE_MAX, threshold=EMBEDDING_DEDUP_THRESHOLD,
        )
    return _embedding_caches[node_slug]


def _rotate_frames(frames_dir: Path, max_frames: int = 5000):
    frames = sorted(frames_dir.glob("*.jpg"), key=lambda p: p.stem)
    excess = len(frames) - max_frames
    if excess <= 0:
        return
    for old in frames[:excess]:
        old.unlink(missing_ok=True)
    log.info("Rotated %d old training frames", excess)


def extract_and_upload_crops(
    frame,
    detections: list[dict],
    node_slug: str,
    base_url: str,
    auth_token: str,
    crops_dir: Path,
    source: str = "idle",
    interaction_id: int | None = None,
    ssl_verify: bool = True,
    min_crop_size: int = 20,
    classifier_confidence: float = 0.8,
    class_confidence_overrides: dict | None = None,
    locked_classes: set | None = None,
    frames_dir: Path | None = None,
) -> None:
    """Crop detected objects from the frame and upload to the platform."""
    import cv2
    crops_payload = []
    frame_id = None

    for i, det in enumerate(detections):
        bbox = det["bbox"]
        x1 = max(0, bbox[0])
        y1 = max(0, bbox[1])
        x2 = min(frame.shape[1], bbox[2])
        y2 = min(frame.shape[0], bbox[3])
        if x2 - x1 < min_crop_size or y2 - y1 < min_crop_size:
            log.debug("SKIP: too small %dx%d (min=%d)", x2 - x1, y2 - y1, min_crop_size)
            continue
        crop_img = frame[y1:y2, x1:x2]
        resized = cv2.resize(crop_img, (224, 224))

        cls_label = det.get("classifier_label") or det.get("classifier_label_candidate")
        cls_conf = det.get("classifier_confidence", 0)
        if locked_classes and cls_label in locked_classes and cls_conf >= classifier_confidence:
            log.debug("SKIP: locked %s", cls_label)
            continue

        emb = compute_embedding(resized)
        cache = _get_embedding_cache(node_slug)
        if cache.is_duplicate(emb):
            log.debug("SKIP: embedding dup (cosine > %.2f)", EMBEDDING_DEDUP_THRESHOLD)
            continue
        cache.register(emb)
        emb_b64 = embedding_to_b64(emb)
        _, buf = cv2.imencode(".jpg", resized, [cv2.IMWRITE_JPEG_QUALITY, 75])
        crop_bytes = buf.tobytes()

        ts = int(time.time() * 1000)
        crop_id = f"{ts}_{i}"
        storage_key = f"crops/{node_slug}/{crop_id}.jpg"

        if frame_id is None and frames_dir is not None:
            frame_id = str(ts)
            frame_path = frames_dir / f"{frame_id}.jpg"
            try:
                cv2.imwrite(str(frame_path), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                _rotate_frames(frames_dir, max_frames=5000)
            except Exception as exc:
                log.warning("Frame save failed: %s", exc)
                frame_id = None

        local_path = crops_dir / f"{crop_id}.jpg"
        try:
            local_path.write_bytes(crop_bytes)
        except OSError as exc:
            log.warning("Failed to save crop locally at %s: %s", local_path, exc)
            continue

        thumb = cv2.resize(resized, (80, 80))
        _, thumb_buf = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, 70])
        thumb_bytes = thumb_buf.tobytes()

        try:
            requests.post(
                f"{base_url.rstrip('/')}/api/nodes/{node_slug}/operator/cv-crop-image?crop_id={crop_id}&thumbnail=1",
                headers={"Authorization": f"Bearer {auth_token}", "Content-Type": "image/jpeg"},
                data=thumb_bytes,
                timeout=5,
                verify=ssl_verify,
            )
        except Exception as exc:
            log.warning("Thumbnail upload failed for crop %s: %s", crop_id, exc)

        crop_entry = {
            "storage_key": storage_key,
            "yolo_class": det["class"],
            "confidence": det["confidence"],
            "bbox": det["bbox"],
            "frame_shape": list(frame.shape[:2]),
            "source": source,
            "interaction_id": interaction_id,
            "predicted_label": det.get("classifier_label", det.get("classifier_label_candidate", "")),
            "predicted_confidence": det.get("classifier_confidence", 0),
            "embedding": emb_b64,
        }
        if frame_id:
            crop_entry["frame_id"] = frame_id
        crops_payload.append(crop_entry)

    log.debug("extract_crops: %d dets in, %d crops out (source=%s)", len(detections), len(crops_payload), source)
    if crops_payload:
        try:
            resp = requests.post(
                f"{base_url.rstrip('/')}/api/nodes/{node_slug}/operator/cv-crops",
                headers={"Authorization": f"Bearer {auth_token}"},
                json={"crops": crops_payload},
                timeout=5,
                verify=ssl_verify,
            )
            log.info("Crop upload: %d crops, status=%d", len(crops_payload), resp.status_code)
        except Exception as exc:
            log.warning("Crop upload failed: %s", exc)


def main() -> int:
    global _rtsp_source
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
    parser = argparse.ArgumentParser(description="yourmove edge CV runtime")
    parser.add_argument("--config", required=True, help="Path to edge supervisor config JSON")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser()
    with open(config_path) as f:
        config = json.load(f)

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    node_slug = config["node_slug"]
    base_url = config.get("base_url", "https://yourmove.live")
    auth_token = config.get("auth_token", "")
    ssl_verify = bool(config.get("ssl_verify", True))
    cv_enabled = bool(config.get("cv_enabled", False))
    state_dir = Path(config.get("state_dir") or "~/.local/state/yourmove-edge").expanduser()
    segment_dir = state_dir / f"{node_slug}-segments"
    crops_dir = state_dir / "crops" / node_slug
    crops_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = state_dir / "frames" / node_slug
    frames_dir.mkdir(parents=True, exist_ok=True)

    use_go2rtc = bool(config.get("rtsp_main_url"))
    go2rtc_port = int(config.get("go2rtc_port", 8554))
    cv_rtsp_sub = f"rtsp://127.0.0.1:{go2rtc_port}/sub" if use_go2rtc else config.get("rtsp_url")
    cv_rtsp_main = f"rtsp://127.0.0.1:{go2rtc_port}/main" if use_go2rtc else config.get("rtsp_url")
    cv_rtsp_url = cv_rtsp_sub
    sub_width = int(config.get("sub_stream_width", 640))
    sub_height = int(config.get("sub_stream_height", 360))

    if not cv_enabled:
        log.info("CV disabled in config, exiting")
        return 0

    if not auth_token:
        log.error("No auth_token in config, cannot communicate with platform")
        return 1

    # 1. Fetch game config
    game_config = fetch_game_config(base_url, node_slug, auth_token, ssl_verify=ssl_verify)
    if not game_config:
        log.warning("No game config from platform — CV will wait for config")
        return 0

    log.info("Game config loaded: template=%s roster=%d entries",
             game_config.get("template_type"), len(game_config.get("roster", [])))

    # 2. Benchmark
    from edge.edge_cv_benchmark import run_benchmark
    benchmark = run_benchmark(
        model_name=game_config.get("template_config", {}).get("detection", {}).get("model", "yolov8s"),
        num_frames=10,
    )
    log.info("Benchmark: fps=%.1f tier=%d hardware=%s", benchmark["fps"], benchmark["tier"], benchmark["hardware_summary"])

    # 3. Report capability
    report_capability(base_url, node_slug, auth_token, benchmark, ssl_verify=ssl_verify)

    tier = benchmark["tier"]
    if tier < 1:
        log.warning("Hardware tier %d — CV disabled, streaming and clips continue normally", tier)
        return 0

    # 3.5. Check for existing classifier model
    cv_settings_init = (game_config or {}).get("cv_settings", {})
    active_model_key = cv_settings_init.get("active_model_key")
    if active_model_key:
        model_cache_path = Path(f"/tmp/cv-models/{node_slug}/classifier.pth")
        model_key_path = Path(f"/tmp/cv-models/{node_slug}/classifier.key")
        cached_key = model_key_path.read_text().strip() if model_key_path.exists() else ""
        if not model_cache_path.exists() or cached_key != active_model_key:
            model_cache_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                resp = requests.get(
                    f"{base_url.rstrip('/')}/api/nodes/{node_slug}/cv-model",
                    params={"key": active_model_key},
                    headers={"Authorization": f"Bearer {auth_token}"},
                    timeout=30,
                    verify=ssl_verify,
                )
                resp.raise_for_status()
                model_cache_path.write_bytes(resp.content)
                model_key_path.write_text(active_model_key)
                log.info("Downloaded classifier model: %s", active_model_key)
            except Exception as e:
                log.warning("Failed to download classifier model: %s", e)
        if model_cache_path.exists():
            try:
                class_names = load_classifier(model_cache_path)
                log.info("Loaded classifier: classes=%s", class_names)
            except Exception as e:
                log.warning("Failed to load classifier: %s", e)

    log.info("Frames dir: %s (%d existing)", frames_dir, len(list(frames_dir.glob("*.jpg"))))

    # 3.6. Load custom YOLO: fine-tuned model from server, or bootstrap YOLO-World from labels
    active_yolo_key = cv_settings_init.get("active_yolo_model_key")
    if active_yolo_key:
        yolo_cache_path = Path(f"/tmp/cv-models/{node_slug}/yolo-custom.pt")
        yolo_key_path = Path(f"/tmp/cv-models/{node_slug}/yolo-custom.key")
        cached_key = yolo_key_path.read_text().strip() if yolo_key_path.exists() else ""
        if not yolo_cache_path.exists() or cached_key != active_yolo_key:
            yolo_cache_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                resp = requests.get(
                    f"{base_url.rstrip('/')}/api/nodes/{node_slug}/cv-model",
                    params={"key": active_yolo_key},
                    headers={"Authorization": f"Bearer {auth_token}"},
                    timeout=60,
                    verify=ssl_verify,
                )
                resp.raise_for_status()
                yolo_cache_path.write_bytes(resp.content)
                yolo_key_path.write_text(active_yolo_key)
                log.info("Downloaded fine-tuned YOLO model: %s", active_yolo_key)
            except Exception as e:
                log.warning("Failed to download fine-tuned YOLO model: %s", e)
        if yolo_cache_path.exists():
            try:
                from edge.edge_cv_detect import load_custom_yolo
                yolo_classes = load_custom_yolo(str(yolo_cache_path), source="finetuned")
                log.info("Loaded fine-tuned YOLO: classes=%s", yolo_classes)
            except Exception as e:
                log.warning("Failed to load fine-tuned YOLO: %s", e)
    else:
        custom_labels = cv_settings_init.get("cv_custom_labels", [])
        if custom_labels:
            world_path = Path(f"/tmp/cv-models/{node_slug}/yolo-world.pt")
            world_labels_path = Path(f"/tmp/cv-models/{node_slug}/yolo-world-labels.txt")
            cached_labels = world_labels_path.read_text().strip() if world_labels_path.exists() else ""
            labels_key = ",".join(sorted(custom_labels))
            if not world_path.exists() or cached_labels != labels_key:
                world_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    from edge.edge_cv_detect import generate_world_model
                    log.info("Generating YOLO-World model for labels: %s", custom_labels)
                    generate_world_model(custom_labels, str(world_path))
                    world_labels_path.write_text(labels_key)
                except Exception as e:
                    log.warning("Failed to generate YOLO-World model: %s", e)
            elif world_path.exists():
                try:
                    from edge.edge_cv_detect import load_custom_yolo
                    load_custom_yolo(str(world_path), source="world")
                    log.info("Loaded cached YOLO-World model: labels=%s", custom_labels)
                except Exception as e:
                    log.warning("Failed to load YOLO-World model: %s", e)

    from edge.edge_cv_detect import get_custom_yolo_classes, get_custom_yolo_source
    _custom_yolo_active = get_custom_yolo_classes() is not None

    # 4. Detection loop with backpressure
    log.info("Starting detection loop at tier %d", tier)
    cv_state_path = state_dir / f"{node_slug}-cv-state.json"
    last_config_refresh = time.time()

    training_process = None
    training_job_id = None
    failed_training_job_id = None
    training_job_type = "classifier"
    cv_python = sys.executable

    _BACKPRESSURE_SLOTS = 2
    post_queue: deque[dict] = deque(maxlen=_BACKPRESSURE_SLOTS)
    post_ready = threading.Event()
    post_ready.set()
    last_snapshot_post = 0.0
    last_crop_capture = 0.0

    def _post_thread():
        """Drains post_queue, sends CV state to server. Runs until SHUTDOWN."""
        nonlocal last_snapshot_post
        while not SHUTDOWN:
            post_ready.wait(timeout=0.5)
            if not post_queue:
                post_ready.clear()
                continue
            payload = post_queue.pop()
            post_queue.clear()
            post_ready.clear()
            try:
                requests.post(
                    f"{base_url.rstrip('/')}/api/nodes/{node_slug}/operator/cv-state",
                    headers={"Authorization": f"Bearer {auth_token}"},
                    json=payload,
                    timeout=3,
                    verify=ssl_verify,
                )
            except Exception:
                pass
            now = time.time()
            if now - last_snapshot_post >= 10.0:
                try:
                    import cv2
                    from edge.edge_cv_detect import decode_latest_segment
                    snap_frames = decode_latest_segment(segment_dir, max_frames=1)
                    if snap_frames:
                        _, buf = cv2.imencode(".jpg", snap_frames[0], [cv2.IMWRITE_JPEG_QUALITY, 60])
                        requests.post(
                            f"{base_url.rstrip('/')}/api/nodes/{node_slug}/operator/cv-snapshot",
                            headers={"Authorization": f"Bearer {auth_token}", "Content-Type": "image/jpeg"},
                            data=buf.tobytes(),
                            timeout=3,
                            verify=ssl_verify,
                        )
                        last_snapshot_post = now
                except Exception:
                    pass

    poster = threading.Thread(target=_post_thread, daemon=True)
    poster.start()

    while not SHUTDOWN:
        now_loop = time.time()
        if now_loop - last_config_refresh >= 30.0:
            refreshed = fetch_game_config(base_url, node_slug, auth_token, ssl_verify=ssl_verify)
            if refreshed:
                game_config = refreshed
                wanted_url = cv_rtsp_main if refreshed.get("cv_settings", {}).get("cv_use_main_stream") else cv_rtsp_sub
                if wanted_url != cv_rtsp_url:
                    log.info("Switching CV stream: %s", wanted_url)
                    cv_rtsp_url = wanted_url
                    if _rtsp_source:
                        _rtsp_source.stop()
                        _rtsp_source = None
                from edge.edge_cv_detect import get_custom_yolo_source
                if get_custom_yolo_source() != "finetuned":
                    new_labels = refreshed.get("cv_settings", {}).get("cv_custom_labels", [])
                    world_labels_path = Path(f"/tmp/cv-models/{node_slug}/yolo-world-labels.txt")
                    cached_labels = world_labels_path.read_text().strip() if world_labels_path.exists() else ""
                    labels_key = ",".join(sorted(new_labels)) if new_labels else ""
                    if labels_key and labels_key != cached_labels:
                        world_path = Path(f"/tmp/cv-models/{node_slug}/yolo-world.pt")
                        world_path.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            from edge.edge_cv_detect import generate_world_model
                            log.info("Labels changed, regenerating YOLO-World: %s", new_labels)
                            generate_world_model(new_labels, str(world_path))
                            world_labels_path.write_text(labels_key)
                            _custom_yolo_active = True
                        except Exception as e:
                            log.warning("Failed to regenerate YOLO-World: %s", e)
            last_config_refresh = now_loop

        detection_state = run_detection_loop(segment_dir, game_config, tier, rtsp_url=cv_rtsp_url)
        if not detection_state:
            time.sleep(0.1)
            continue

        frame_shape = detection_state.get("frame_shape", (360, 640))

        def _bbox(det):
            return det["bbox"]

        overlay_frame_size = list(frame_shape)
        _sc = game_config.get("template_config", {}).get("scoring", {})
        _tp = _sc.get("target_points", {})
        _hb = _sc.get("hit_base_points", 75)

        def _det_points(d):
            lbl = d.get("classifier_label") or d.get("class", "")
            return int(_tp.get(lbl, _hb))

        serializable = {
            "targets": [
                {
                    "class": t["class"],
                    "bbox": _bbox(t),
                    "confidence": t["confidence"],
                    "roster_entry": {
                        "name": t.get("roster_entry", {}).get("name", ""),
                        "role": t.get("roster_entry", {}).get("role", ""),
                    },
                    "classifier_label": t.get("classifier_label", ""),
                    "classifier_confidence": t.get("classifier_confidence", 0),
                    "track_id": t.get("track_id"),
                    "points": _det_points(t),
                }
                for t in detection_state.get("targets", [])
            ],
            "all_detections": [
                {
                    "class": d["class"],
                    "bbox": _bbox(d),
                    "confidence": d["confidence"],
                    "classifier_label": d.get("classifier_label", ""),
                    "classifier_confidence": d.get("classifier_confidence", 0),
                    "track_id": d.get("track_id"),
                    "points": _det_points(d),
                }
                for d in detection_state.get("all_detections", [])
            ],
            "blocked": detection_state.get("blocked", False),
            "detection_count": detection_state.get("detection_count", 0),
            "timestamp_ms": detection_state.get("timestamp_ms", 0),
            "tier": tier,
            "frame_size": overlay_frame_size,
            "source_resolution": list(frame_shape),
            "yolo_training_frames": len(list(frames_dir.glob("*.jpg"))),
            "custom_yolo_active": _custom_yolo_active,
            "custom_yolo_source": get_custom_yolo_source(),
        }
        try:
            cv_state_path.write_text(json.dumps(serializable))
        except Exception:
            pass

        # Backpressure: enqueue for POST thread, detect next frame immediately
        if len(post_queue) < _BACKPRESSURE_SLOTS:
            post_queue.append(serializable)
            post_ready.set()
        # else: queue full, server is behind — skip this post, keep detecting

        now = time.time()
        cv_settings = (game_config or {}).get("cv_settings", {})
        min_crop_size_setting = cv_settings.get("cv_min_crop_size", 20)
        classifier_conf = cv_settings.get("cv_classifier_confidence", 0.8)
        class_conf_overrides = cv_settings.get("cv_class_confidence_overrides", {})

        is_capturing = cv_settings.get("cv_capturing")
        if is_capturing is None:
            old_mode = cv_settings.get("cv_capture_mode", "off")
            is_capturing = old_mode != "off"

        locked_classes = set(cv_settings.get("cv_locked_classes", []))
        quota_remaining = cv_settings.get("cv_crop_quota_remaining", 500)

        fire_marker = state_dir / f"{node_slug}-fire-marker.json"
        interaction_crop_source = "idle"
        interaction_id_for_crop = None
        if fire_marker.exists():
            try:
                marker = json.loads(fire_marker.read_text())
                interaction_crop_source = "interaction"
                interaction_id_for_crop = marker.get("interaction_id")
                fire_marker.unlink(missing_ok=True)
            except Exception:
                pass

        should_capture = False
        if interaction_crop_source == "interaction":
            should_capture = True
        elif is_capturing and quota_remaining > 0 and now - last_crop_capture >= _MIN_CROP_INTERVAL:
            should_capture = True

        if should_capture:
            crop_frame = detection_state.get("frame")
            crop_dets = detection_state.get("all_detections", [])
            if crop_frame is not None and crop_dets:
                try:
                    extract_and_upload_crops(
                        frame=crop_frame,
                        detections=crop_dets,
                        node_slug=node_slug,
                        base_url=base_url,
                        auth_token=auth_token,
                        crops_dir=crops_dir,
                        source=interaction_crop_source,
                        interaction_id=interaction_id_for_crop,
                        ssl_verify=ssl_verify,
                        min_crop_size=min_crop_size_setting,
                        classifier_confidence=classifier_conf,
                        class_confidence_overrides=class_conf_overrides,
                        locked_classes=locked_classes,
                        frames_dir=frames_dir,
                    )
                except Exception:
                    pass
                last_crop_capture = now

        lost_crops = detection_state.get("lost_track_crops", [])
        if lost_crops:
            log.info("Lost tracks: %d (capturing=%s quota=%d)", len(lost_crops), is_capturing, quota_remaining)
        if is_capturing and quota_remaining > 0 and lost_crops:
            crop_frame = detection_state.get("frame")
            if crop_frame is not None:
                try:
                    extract_and_upload_crops(
                        frame=crop_frame,
                        detections=lost_crops,
                        node_slug=node_slug,
                        base_url=base_url,
                        auth_token=auth_token,
                        crops_dir=crops_dir,
                        source="lost_track",
                        interaction_id=None,
                        ssl_verify=ssl_verify,
                        min_crop_size=min_crop_size_setting,
                        classifier_confidence=0.0,
                        class_confidence_overrides={},
                        locked_classes=set(),
                        frames_dir=frames_dir,
                    )
                except Exception:
                    pass

        cv_settings_loop = (game_config or {}).get("cv_settings", {})
        active_job = cv_settings_loop.get("active_training_job_id")
        if active_job and not training_process and active_job != failed_training_job_id:
            training_job_id = active_job
            training_job_type = "classifier"
            manifest_key = cv_settings_loop.get("active_training_manifest_key", "")
            model_version = cv_settings_loop.get("active_model_version", 0) + 1
            log.info("Launching training job %d (v%d)", active_job, model_version)
            train_env = os.environ.copy()
            train_env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
            training_process = subprocess.Popen([
                cv_python, "-m", "edge.edge_cv_train",
                "--base-url", base_url,
                "--node-slug", node_slug,
                "--auth-token", auth_token,
                "--job-id", str(active_job),
                "--manifest-key", manifest_key,
                "--model-version", str(model_version),
                "--crops-dir", str(crops_dir),
            ] + (["--no-ssl-verify"] if not ssl_verify else []),
            env=train_env)

        # YOLO training job launch
        yolo_job = cv_settings_loop.get("active_yolo_training_job_id")
        if yolo_job and not training_process and yolo_job != failed_training_job_id:
            training_job_id = yolo_job
            training_job_type = "yolo"
            yolo_manifest_key = cv_settings_loop.get("active_yolo_training_manifest_key", "")
            yolo_version = cv_settings_loop.get("active_yolo_model_version", 0) + 1
            log.info("Launching YOLO training job %d (v%d)", yolo_job, yolo_version)
            train_env = os.environ.copy()
            train_env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
            training_process = subprocess.Popen([
                cv_python, "-m", "edge.edge_cv_yolo_train",
                "--base-url", base_url,
                "--node-slug", node_slug,
                "--auth-token", auth_token,
                "--job-id", str(yolo_job),
                "--manifest-key", yolo_manifest_key,
                "--model-version", str(yolo_version),
                "--frames-dir", str(frames_dir),
            ] + (["--no-ssl-verify"] if not ssl_verify else []),
            env=train_env)

        if training_process and training_process.poll() is not None:
            exit_code = training_process.returncode
            log.info("Training process exited with code %d (type=%s)", exit_code, training_job_type)
            training_process = None
            if exit_code == 0 and training_job_id:
                if training_job_type == "yolo":
                    best_path = Path(f"/tmp/cv-yolo-training/job-{training_job_id}/train/weights/best.pt")
                    if best_path.exists():
                        cache_path = Path(f"/tmp/cv-models/{node_slug}/yolo-custom.pt")
                        cache_path.parent.mkdir(parents=True, exist_ok=True)
                        import shutil
                        shutil.copy2(str(best_path), str(cache_path))
                        try:
                            from edge.edge_cv_detect import load_custom_yolo
                            yolo_classes = load_custom_yolo(str(cache_path), source="finetuned")
                            log.info("Fine-tuned YOLO hot-swapped: classes=%s", yolo_classes)
                            _custom_yolo_active = True
                            world_path = Path(f"/tmp/cv-models/{node_slug}/yolo-world.pt")
                            world_path.unlink(missing_ok=True)
                            Path(f"/tmp/cv-models/{node_slug}/yolo-world-labels.txt").unlink(missing_ok=True)
                        except Exception as e:
                            log.error("Failed to load fine-tuned YOLO: %s", e)
                else:
                    model_path = Path(f"/tmp/cv-training/job-{training_job_id}/model.pth")
                    if model_path.exists():
                        try:
                            class_names = load_classifier(model_path)
                            log.info("Classifier hot-swapped: classes=%s", class_names)
                        except Exception as e:
                            log.error("Failed to load classifier: %s", e)
            elif exit_code != 0 and training_job_id:
                log.error("Training job %d failed with exit code %d", training_job_id, exit_code)
                failed_training_job_id = training_job_id
                try:
                    from edge.edge_cv_train import report_progress
                    report_progress(base_url, node_slug, auth_token, training_job_id,
                                    "failed", error=f"Training process crashed (exit code {exit_code})",
                                    ssl_verify=ssl_verify)
                except Exception:
                    pass
            training_job_id = None
            training_job_type = "classifier"

        if detection_state["targets"]:
            log.debug("Detected %d targets in frame", len(detection_state["targets"]))

        # Backpressure: if queue is full, wait briefly for POST thread to drain
        if len(post_queue) >= _BACKPRESSURE_SLOTS:
            time.sleep(0.02)

    log.info("CV runtime shutting down")
    if _rtsp_source:
        _rtsp_source.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
