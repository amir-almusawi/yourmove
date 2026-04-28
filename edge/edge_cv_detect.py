"""Frame decoding from RTSP/segment buffer and YOLO detection with tracking."""
from __future__ import annotations
import logging
import subprocess
import threading
import time
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)


class RTSPFrameSource:
    """Continuously grabs frames from an RTSP stream in a background thread.
    grab_frame() always returns the most recent frame with no buffering delay."""

    def __init__(self, rtsp_url: str):
        self._url = rtsp_url
        self._cap = None
        self._frame = None
        self._lock = threading.Lock()
        self._running = False
        self._thread = None

    def start(self) -> bool:
        self._cap = cv2.VideoCapture(self._url, cv2.CAP_FFMPEG)
        if not self._cap.isOpened():
            log.error("Failed to open RTSP stream: %s", self._url)
            return False
        self._running = True
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        log.info("RTSP frame source started: %s", self._url)
        return True

    def _reader(self):
        while self._running:
            ret, frame = self._cap.read()
            if ret:
                with self._lock:
                    self._frame = frame
            else:
                log.warning("RTSP read failed, reconnecting...")
                self._cap.release()
                time.sleep(1)
                self._cap = cv2.VideoCapture(self._url, cv2.CAP_FFMPEG)

    def grab_frame(self) -> np.ndarray | None:
        with self._lock:
            return self._frame

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
        if self._cap:
            self._cap.release()

_model_cache: dict[str, object] = {}

_custom_model_path: str | None = None
_custom_model_classes: list[str] | None = None
_custom_model_name_remap: dict[str, str] | None = None


_custom_model_source: str | None = None  # "world" or "finetuned"


def generate_world_model(class_names: list[str], output_path: str,
                         prompts: list[str] | None = None) -> list[str]:
    """Generate a YOLO-World model with set_classes, strip text encoder, save.
    First call downloads CLIP weights (~600MB). Returns class names.
    If prompts is provided, those are used as CLIP text prompts instead of
    class_names, and detections are remapped back to class_names."""
    global _custom_model_path, _custom_model_classes, _custom_model_source, _custom_model_name_remap
    from ultralytics import YOLO
    model = YOLO("yolov8s-worldv2.pt")
    model.set_classes(prompts or class_names)
    model.save(output_path)
    saved = YOLO(output_path)
    _model_cache["__custom__"] = saved
    _custom_model_path = output_path
    if prompts:
        _custom_model_name_remap = {p: n for p, n in zip(prompts, class_names)}
        _custom_model_classes = list(dict.fromkeys(class_names))
    else:
        _custom_model_name_remap = None
        _custom_model_classes = list(saved.names.values())
    _custom_model_source = "world"
    log.info("YOLO-World model generated: classes=%s prompts=%s -> %s",
             class_names, prompts or class_names, output_path)
    return _custom_model_classes


def load_custom_yolo(model_path: str, source: str = "finetuned") -> list[str]:
    """Load a custom YOLO model (.pt). Returns class names."""
    global _custom_model_path, _custom_model_classes, _custom_model_source, _custom_model_name_remap
    from ultralytics import YOLO
    model = YOLO(model_path)
    _model_cache["__custom__"] = model
    _custom_model_path = model_path
    _custom_model_classes = list(model.names.values())
    _custom_model_source = source
    _custom_model_name_remap = None
    return _custom_model_classes


def unload_custom_yolo():
    """Revert to generic YOLO model."""
    global _custom_model_path, _custom_model_classes, _custom_model_source, _custom_model_name_remap
    _model_cache.pop("__custom__", None)
    _custom_model_path = None
    _custom_model_classes = None
    _custom_model_source = None
    _custom_model_name_remap = None


def get_custom_yolo_classes() -> list[str] | None:
    """Return custom YOLO class names if a custom model is loaded."""
    return _custom_model_classes


def get_custom_yolo_source() -> str | None:
    """Return 'world' or 'finetuned' or None."""
    return _custom_model_source


def decode_latest_segment(segment_dir: Path, max_frames: int = 5) -> list[np.ndarray]:
    segments = sorted(
        (s for s in segment_dir.glob("*.ts") if s.stat().st_size > 1000),
        key=lambda p: p.stem,
    )
    if not segments:
        return []
    latest = segments[-1]
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0",
             str(latest)],
            capture_output=True, text=True, timeout=5,
        )
        if probe.returncode != 0 or not probe.stdout.strip():
            return []
        first_line = probe.stdout.strip().splitlines()[0]
        dims = first_line.split(",")
        width, height = int(dims[0]), int(dims[1])
    except Exception:
        return []

    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "quiet", "-i", str(latest),
             "-frames:v", str(max_frames), "-f", "rawvideo", "-pix_fmt", "bgr24",
             "pipe:"],
            capture_output=True, timeout=10,
        )
        if proc.returncode != 0 or not proc.stdout:
            return []
    except Exception:
        return []

    frame_size = width * height * 3
    raw = proc.stdout
    frames = []
    for i in range(min(max_frames, len(raw) // frame_size)):
        frame = np.frombuffer(raw[i * frame_size:(i + 1) * frame_size], dtype=np.uint8)
        frames.append(frame.reshape(height, width, 3))
    return frames


def _get_model(model_name: str):
    if "__custom__" in _model_cache:
        return _model_cache["__custom__"]
    if model_name not in _model_cache:
        from ultralytics import YOLO
        _model_cache[model_name] = YOLO(f"{model_name}.pt")
    return _model_cache[model_name]


def _remap_class(name: str) -> str:
    if _custom_model_name_remap:
        return _custom_model_name_remap.get(name, name)
    return name


def _format_detection(class_name: str, bbox: np.ndarray, confidence: float) -> dict:
    return {
        "class": class_name,
        "bbox": [int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])],
        "confidence": round(float(confidence), 4),
    }


def run_detection(frame: np.ndarray, model_name: str = "yolov8n", conf: float = 0.25) -> list[dict]:
    model = _get_model(model_name)
    results = model(frame, verbose=False, conf=conf)
    detections = []
    for result in results:
        for box in result.boxes:
            class_id = int(box.cls[0])
            class_name = _remap_class(model.names[class_id])
            detections.append(_format_detection(
                class_name=class_name,
                bbox=box.xyxy[0].cpu().numpy(),
                confidence=float(box.conf[0]),
            ))
    return detections


def run_tracking(frame: np.ndarray, model_name: str = "yolov8n", conf: float = 0.25) -> list[dict]:
    """Run YOLO with ByteTrack — returns detections with persistent track_id.
    Falls back gracefully when tracker can't assign IDs."""
    model = _get_model(model_name)
    results = model.track(frame, verbose=False, conf=conf, persist=True)
    detections = []
    for result in results:
        has_ids = result.boxes.id is not None
        for i, box in enumerate(result.boxes):
            class_id = int(box.cls[0])
            class_name = _remap_class(model.names[class_id])
            det = _format_detection(
                class_name=class_name,
                bbox=box.xyxy[0].cpu().numpy(),
                confidence=float(box.conf[0]),
            )
            if has_ids:
                det["track_id"] = int(result.boxes.id[i])
            detections.append(det)
    return detections
