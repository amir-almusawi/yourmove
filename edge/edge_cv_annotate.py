"""Live CV overlay renderer — draws bounding boxes onto video frames.

AnnotatedStreamPublisher accepts pre-annotated frames from the detection
loop and pipes them to FFmpeg NVENC → go2rtc RTSP.  Single decode path:
the detection loop reads RTSP once, runs YOLO, draws boxes on the same
frame, then pushes to the publisher.  GStreamer WHIP consumes the result.
"""
from __future__ import annotations
import cv2
import logging
import numpy as np
import subprocess
import time

log = logging.getLogger(__name__)

_COLORS_BGR = [
    (0, 165, 255),   # orange
    (0, 255, 0),     # green
    (255, 0, 255),   # magenta
    (255, 255, 0),   # cyan
    (0, 255, 255),   # yellow
    (255, 0, 0),     # blue
    (128, 0, 255),   # purple
    (0, 200, 200),   # dark yellow
]

_POINTS_COLOR_BGR = (80, 255, 180)
_LABEL_BG_BGR = (18, 22, 30)
_LABEL_SHADOW_BGR = (0, 0, 0)


def _color_for_label(label: str, label_colors: dict[str, str] | None = None) -> tuple[int, int, int]:
    if label_colors and label in label_colors:
        hex_color = label_colors[label].lstrip("#")
        if len(hex_color) == 6:
            r, g, b = int(hex_color[:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
            return (b, g, r)
    return _COLORS_BGR[hash(label) % len(_COLORS_BGR)]


def _draw_text_with_shadow(
    frame: np.ndarray,
    text: str,
    origin: tuple[int, int],
    font: int,
    font_scale: float,
    color: tuple[int, int, int],
    thickness: int,
) -> None:
    shadow_thickness = max(thickness + 1, 2)
    cv2.putText(frame, text, (origin[0] + 1, origin[1] + 1), font, font_scale, _LABEL_SHADOW_BGR, shadow_thickness, cv2.LINE_AA)
    cv2.putText(frame, text, origin, font, font_scale, color, thickness, cv2.LINE_AA)


def draw_detections(
    frame: np.ndarray,
    detections: list[dict],
    label_colors: dict[str, str] | None = None,
) -> np.ndarray:
    """Draw bounding boxes and labels onto a frame. Returns the annotated copy."""
    annotated = frame.copy()
    h, w = annotated.shape[:2]
    font = cv2.FONT_HERSHEY_DUPLEX
    font_scale = max(0.34, min(h, w) / 1350)
    thickness = max(1, int(min(h, w) / 700))
    pad_x = max(5, int(min(h, w) / 220))
    pad_y = max(4, int(min(h, w) / 260))
    gap = max(4, int(min(h, w) / 320))

    for det in detections:
        label = det.get("classifier_label", "")
        if not label or label == "__ignore__":
            continue
        bbox = det.get("bbox")
        if not bbox or len(bbox) < 4:
            continue

        x1, y1, x2, y2 = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
        color = _color_for_label(label, label_colors)

        cv2.rectangle(annotated, (x1, y1), (x2, y2), color, max(thickness + 1, 2), cv2.LINE_AA)

        pts = det.get("points")
        label_text = str(label)
        points_text = ""
        if pts is not None:
            points_text = f"+{pts}" if pts >= 0 else str(pts)

        (label_w, label_h), label_baseline = cv2.getTextSize(label_text, font, font_scale, thickness)
        points_w = points_h = points_baseline = 0
        if points_text:
            (points_w, points_h), points_baseline = cv2.getTextSize(points_text, font, font_scale, thickness)

        text_h = max(label_h + label_baseline, points_h + points_baseline)
        text_w = label_w + (gap + points_w if points_text else 0)
        box_h = text_h + (pad_y * 2)
        box_w = text_w + (pad_x * 2)
        box_top = max(0, y1 - box_h - 4)
        box_bottom = box_top + box_h
        box_right = min(w, x1 + box_w)
        box_left = max(0, box_right - box_w)

        overlay = annotated.copy()
        cv2.rectangle(overlay, (box_left, box_top), (box_right, box_bottom), _LABEL_BG_BGR, -1)
        cv2.addWeighted(overlay, 0.82, annotated, 0.18, 0, annotated)
        cv2.rectangle(annotated, (box_left, box_top), (box_right, box_bottom), color, 1, cv2.LINE_AA)

        text_y = box_bottom - pad_y - max(label_baseline, points_baseline)
        label_x = box_left + pad_x
        _draw_text_with_shadow(annotated, label_text, (label_x, text_y), font, font_scale, (245, 247, 250), thickness)
        if points_text:
            points_x = label_x + label_w + gap
            _draw_text_with_shadow(annotated, points_text, (points_x, text_y), font, font_scale, _POINTS_COLOR_BGR, thickness)

    return annotated


class AnnotatedStreamPublisher:
    """Pushes pre-annotated frames via FFmpeg NVENC to go2rtc's RTSP server."""

    def __init__(self, width: int, height: int, fps: int = 25,
                 rtsp_port: int = 18554, **_kwargs):
        self._w = width
        self._h = height
        self._fps = fps
        self._rtsp_url = f"rtsp://127.0.0.1:{rtsp_port}/annotated"
        self._running = False
        self._ffmpeg: subprocess.Popen | None = None

    def start(self) -> bool:
        if self._running:
            return True
        self._running = True
        log.info("Annotated stream publisher → %s", self._rtsp_url)
        return True

    def push_frame(self, frame: np.ndarray) -> None:
        if not self._running:
            return
        if frame.shape[1] != self._w or frame.shape[0] != self._h:
            frame = cv2.resize(frame, (self._w, self._h))
        if self._ffmpeg is None or self._ffmpeg.poll() is not None:
            self._ffmpeg = self._start_ffmpeg()
        try:
            self._ffmpeg.stdin.write(frame.tobytes())
        except BrokenPipeError:
            log.warning("FFmpeg pipe broken, restarting encoder")
            self._kill_ffmpeg()

    def stop(self) -> None:
        self._running = False
        self._kill_ffmpeg()

    @property
    def rtsp_url(self) -> str:
        return self._rtsp_url

    def _start_ffmpeg(self) -> subprocess.Popen:
        cmd = [
            "ffmpeg", "-y",
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "-s", f"{self._w}x{self._h}",
            "-use_wallclock_as_timestamps", "1",
            "-i", "pipe:0",
            "-c:v", "h264_nvenc",
            "-preset", "p1",
            "-tune", "ull",
            "-rc", "cbr",
            "-b:v", "1500k",
            "-g", str(self._fps),
            "-r", str(self._fps),
            "-vsync", "cfr",
            "-f", "rtsp",
            "-rtsp_transport", "tcp",
            self._rtsp_url,
        ]
        log.info("Starting annotated stream FFmpeg encoder (NVENC CFR %dfps)", self._fps)
        return subprocess.Popen(
            cmd, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )

    def _kill_ffmpeg(self) -> None:
        if self._ffmpeg:
            try:
                self._ffmpeg.stdin.close()
            except Exception:
                pass
            try:
                self._ffmpeg.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._ffmpeg.kill()
            self._ffmpeg = None
            self._ffmpeg = None
