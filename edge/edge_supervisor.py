#!/usr/bin/env python3
from __future__ import annotations
import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import tempfile
import signal
import atexit
from pathlib import Path

# Ensure parent dir is on sys.path so 'from edge.X' imports work
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from datetime import datetime, timezone

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SHUTDOWN_REQUESTED = False


def now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(message: str) -> None:
    print(f"[{now_iso()}] {message}", flush=True)


def request_shutdown(signum, _frame) -> None:
    global SHUTDOWN_REQUESTED
    SHUTDOWN_REQUESTED = True
    log(f"shutdown_requested signal={signum}")


def run_shell(command: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def probe_rtsp(rtsp_url: str, timeout_seconds: int) -> tuple[bool, str]:
    cmd = [
        "ffprobe",
        "-rtsp_transport",
        "tcp",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        rtsp_url,
    ]
    try:
        result = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return False, "ffprobe timeout"
    output = (result.stdout or "").strip()
    if result.returncode == 0 and "video" in output:
        return True, output
    return False, output or f"ffprobe exit {result.returncode}"


def container_running(container_name: str) -> bool:
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _docker_logs_recent(container_name: str, since: str = "30s") -> str | None:
    result = subprocess.run(
        ["docker", "logs", "--since", since, container_name],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=5,
    )
    if result.returncode != 0:
        return None
    return result.stdout or ""


# Per-source stats appear inside GValueArray quoted strings where `=` and `(` are
# backslash-escaped (e.g. `packets-sent\=\(guint64\)113`). Accept both forms.
_GST_UINT64_RE = lambda key: re.compile(rf'{re.escape(key)}\\?=\\?\(guint64\\?\)(\d+)')
_PACKETS_SENT_RE = _GST_UINT64_RE("packets-sent")
_BYTES_RECEIVED_RE = _GST_UINT64_RE("bytes-received")


def whip_packets_sent(container_name: str) -> int | None:
    """Parse the latest packets-sent count from GStreamer whipsink stats in docker logs."""
    output = _docker_logs_recent(container_name)
    if output is None:
        return None
    best = None
    for match in _PACKETS_SENT_RE.finditer(output):
        val = int(match.group(1))
        if best is None or val > best:
            best = val
    return best


def rtsp_bytes_received(container_name: str) -> int | None:
    """Max bytes-received seen on the rtspsrc side.

    The stall whipsink can't detect: RTSP source dies mid-session (camera reboot,
    network blip) but whipsink keeps emitting RTCP/keepalive packets, so packets-sent
    ticks up while zero media flows. Sampling rtspsrc's own counter catches that.
    """
    output = _docker_logs_recent(container_name)
    if output is None:
        return None
    best = None
    for line in output.splitlines():
        if "GstRTSPSrc:rtspsrc0" not in line:
            continue
        for match in _BYTES_RECEIVED_RE.finditer(line):
            val = int(match.group(1))
            if best is None or val > best:
                best = val
    return best


def _http_get_json(url: str, auth_token: str, timeout_seconds: int, ssl_verify: bool) -> tuple[dict | None, str]:
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {auth_token}"},
            timeout=timeout_seconds,
            verify=ssl_verify,
        )
        if response.ok:
            return response.json(), ""
        return None, f"http {response.status_code}: {response.text}"
    except Exception as exc:
        return None, str(exc)


def fetch_stream_status(base_url: str, node_slug: str, auth_token: str, timeout_seconds: int, ssl_verify: bool) -> tuple[dict | None, str]:
    return _http_get_json(
        f"{base_url.rstrip('/')}/api/nodes/{node_slug}/operator/stream-status",
        auth_token,
        timeout_seconds,
        ssl_verify,
    )


def runtime_clip_settings(config: dict, status_payload: dict | None) -> tuple[float, float, float]:
    clip_cfg = dict((status_payload or {}).get("clip_capture_settings") or {})
    pre_roll = clip_cfg.get("pre_roll_seconds", config.get("clip_pre_roll_seconds", 3))
    post_roll = clip_cfg.get("post_roll_seconds", config.get("clip_post_roll_seconds", 6))
    trigger_offset = clip_cfg.get("trigger_offset_seconds", config.get("clip_trigger_offset_seconds", 0))
    try:
        pre_roll = float(pre_roll)
    except Exception:
        pre_roll = 3.0
    try:
        post_roll = float(post_roll)
    except Exception:
        post_roll = 6.0
    try:
        trigger_offset = float(trigger_offset)
    except Exception:
        trigger_offset = 0.0
    return pre_roll, post_roll, trigger_offset


def fetch_clip_jobs(base_url: str, node_slug: str, auth_token: str, timeout_seconds: int, ssl_verify: bool) -> tuple[list[dict], str, dict | None]:
    payload, error = _http_get_json(
        f"{base_url.rstrip('/')}/api/nodes/{node_slug}/operator/clip-jobs",
        auth_token,
        timeout_seconds,
        ssl_verify,
    )
    if error:
        return [], error, None
    return list((payload or {}).get("jobs") or []), "", (payload or {}).get("aim_context")


def upload_clip_job(base_url: str, node_slug: str, interaction_id: int, auth_token: str, clip_path: Path, timeout_seconds: int, ssl_verify: bool) -> tuple[bool, str]:
    try:
        with clip_path.open("rb") as fh:
            response = requests.post(
                f"{base_url.rstrip('/')}/api/nodes/{node_slug}/operator/clip-jobs/{interaction_id}/upload",
                headers={"Authorization": f"Bearer {auth_token}"},
                files={"clip": (clip_path.name, fh, "video/mp4")},
                timeout=timeout_seconds,
                verify=ssl_verify,
            )
        if response.ok:
            return True, response.text
        return False, f"http {response.status_code}: {response.text}"
    except Exception as exc:
        return False, str(exc)


def fail_clip_job(base_url: str, node_slug: str, interaction_id: int, auth_token: str, detail: str, timeout_seconds: int, ssl_verify: bool) -> tuple[bool, str]:
    try:
        response = requests.post(
            f"{base_url.rstrip('/')}/api/nodes/{node_slug}/operator/clip-jobs/{interaction_id}/fail",
            headers={"Authorization": f"Bearer {auth_token}"},
            json={"detail": detail},
            timeout=timeout_seconds,
            verify=ssl_verify,
        )
        if response.ok:
            return True, response.text
        return False, f"http {response.status_code}: {response.text}"
    except Exception as exc:
        return False, str(exc)


def post_edge_heartbeat(base_url: str, node_slug: str, auth_token: str, payload: dict, timeout_seconds: int, ssl_verify: bool) -> tuple[bool, str]:
    try:
        response = requests.post(
            f"{base_url.rstrip('/')}/api/nodes/{node_slug}/operator/edge-heartbeat",
            headers={"Authorization": f"Bearer {auth_token}"},
            json=payload,
            timeout=timeout_seconds,
            verify=ssl_verify,
        )
        if response.ok:
            return True, response.text
        return False, f"http {response.status_code}: {response.text}"
    except Exception as exc:
        return False, str(exc)


def load_json(path: Path, default):
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def save_json(path: Path, payload) -> None:
    ensure_parent(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    tmp.replace(path)


def parse_job_timestamp(value: str) -> float | None:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def probe_media_duration(path: Path) -> float:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return 0.0
        return float((result.stdout or "0").strip() or "0")
    except Exception:
        return 0.0



def start_segment_recorder(rtsp_url: str, segment_dir: Path, segment_time_seconds: int) -> subprocess.Popen:
    """Start ffmpeg reading RTSP and saving MPEG-TS segments with video+audio."""
    segment_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-use_wallclock_as_timestamps",
        "1",
        "-rtsp_transport",
        "tcp",
        "-fflags",
        "+genpts+discardcorrupt",
        "-i",
        rtsp_url,
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-f",
        "segment",
        "-segment_time",
        str(max(1, segment_time_seconds)),
        "-segment_atclocktime",
        "1",
        "-reset_timestamps",
        "1",
        "-strftime",
        "1",
        "-segment_format",
        "mpegts",
        str(segment_dir / "%s.ts"),
    ]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop_process(proc: subprocess.Popen | None, name: str, timeout_seconds: float = 8.0) -> None:
    if proc is None:
        return
    try:
        if proc.poll() is not None:
            return
        proc.terminate()
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.2)
        proc.kill()
    except Exception as exc:
        log(f"{name}_stop_error={exc}")


def start_cv_runtime(config_path: Path, log_path: Path | None = None) -> subprocess.Popen | None:
    cv_script = Path(__file__).parent / "edge_cv_runtime.py"
    if not cv_script.exists():
        log("cv_runtime_not_found path=" + str(cv_script))
        return None
    import shutil
    cv_python = shutil.which("python3.10") or shutil.which("python3.11") or shutil.which("python3.12") or sys.executable
    try:
        if log_path:
            log_fh = open(log_path, "a")
        else:
            log_fh = subprocess.DEVNULL
        proc = subprocess.Popen(
            [cv_python, str(cv_script), "--config", str(config_path)],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )
        if log_path and log_fh != subprocess.DEVNULL:
            log_fh.close()
        log(f"cv_runtime_started pid={proc.pid}")
        return proc
    except Exception as exc:
        log(f"cv_runtime_start_failed error={exc}")
        return None


def newest_segment_age_seconds(segment_dir: Path) -> float | None:
    newest_mtime = None
    for seg in segment_dir.glob("*.ts"):
        try:
            mtime = seg.stat().st_mtime
        except Exception:
            continue
        newest_mtime = mtime if newest_mtime is None or mtime > newest_mtime else newest_mtime
    if newest_mtime is None:
        return None
    return max(0.0, time.time() - newest_mtime)


def cleanup_old_segments(segment_dir: Path, keep_seconds: int) -> None:
    cutoff = time.time() - max(keep_seconds, 10)
    for seg in segment_dir.glob("*.ts"):
        try:
            ts = int(seg.stem)
        except Exception:
            continue
        if ts < cutoff:
            try:
                seg.unlink()
            except Exception:
                pass


def build_clip_from_segments(
    segment_dir: Path,
    fired_at_ts: float,
    pre_roll_seconds: float,
    post_roll_seconds: float,
    trigger_offset_seconds: float,
    segment_time_seconds: int,
    output_path: Path,
) -> tuple[bool, str]:
    effective_ts = fired_at_ts + trigger_offset_seconds
    segment_padding = max(10, segment_time_seconds * 3)
    target_duration = max(1, pre_roll_seconds + post_roll_seconds)
    window_start = int(effective_ts - max(0, pre_roll_seconds) - segment_padding)
    window_end = int(effective_ts + max(0, post_roll_seconds) + segment_padding)
    selected: list[Path] = []
    latest_ts = None
    for seg in sorted(segment_dir.glob("*.ts")):
        try:
            ts = int(seg.stem)
        except Exception:
            continue
        latest_ts = ts if latest_ts is None or ts > latest_ts else latest_ts
        if window_start <= ts <= window_end:
            selected.append(seg)
    if latest_ts is None or latest_ts < int(effective_ts + post_roll_seconds + segment_padding):
        return False, "waiting for post-roll segments"
    if not selected:
        return False, "no buffer segments for clip window"

    first_segment_ts = min(int(s.stem) for s in selected)
    seek_to = max(0.0, effective_ts - pre_roll_seconds - first_segment_ts)

    with tempfile.TemporaryDirectory(prefix="yourmove-edge-clip-") as tmpdir:
        concat_path = Path(tmpdir) / "segments.txt"
        concat_path.write_text("".join(f"file '{seg.as_posix()}'\n" for seg in selected), encoding="utf-8")
        transcode_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-fflags",
            "+genpts",
            "-i",
            str(concat_path),
            "-ss",
            f"{seek_to:.3f}",
            "-t",
            f"{target_duration:.3f}",
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-tune",
            "zerolatency",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        transcode_result = subprocess.run(transcode_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if transcode_result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
            duration = probe_media_duration(output_path)
            min_duration = max(2.5, target_duration * 0.5)
            if duration >= min_duration:
                return True, f"transcode:{duration:.2f}s"
            try:
                output_path.unlink(missing_ok=True)
            except Exception:
                pass
            return False, f"clip too short ({duration:.2f}s < {min_duration:.2f}s)"
        raw = (transcode_result.stdout or "").strip()[:1000]
        sanitized = re.sub(r'/[^\s:]+/', '<path>/', raw)
        return False, sanitized


def restart_publisher(restart_command: str) -> tuple[bool, str]:
    result = run_shell(restart_command)
    return result.returncode == 0, (result.stdout or "").strip()


# ---------------------------------------------------------------------------
# go2rtc fan-out layer
# ---------------------------------------------------------------------------

def generate_go2rtc_config(config: dict, output_path: Path) -> None:
    """Generate go2rtc.yaml from edge node config."""
    rtsp_main = config.get("rtsp_main_url", "")
    rtsp_sub = config["rtsp_url"]
    go2rtc_port = config.get("go2rtc_port", 8554)
    api_port = config.get("go2rtc_api_port", 1984)

    yaml_content = f"""streams:
  main:
    - {rtsp_main}
  sub:
    - {rtsp_sub}

rtsp:
  listen: ":{go2rtc_port}"

api:
  listen: ":{api_port}"

webrtc:
  listen: ""
"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml_content)


def build_go2rtc_docker_command(config: dict, config_dir: Path) -> list[str]:
    """Build the docker run command for the go2rtc+GStreamer container."""
    container_name = config["container_name"]
    go2rtc_config_path = config_dir / "go2rtc.yaml"
    docker_image = config.get("go2rtc_image", "ym-edge-go2rtc:latest")
    whip_endpoint = config.get("whip_endpoint", "")
    whip_token = config.get("whip_token", "")
    go2rtc_port = config.get("go2rtc_port", 8554)
    api_port = config.get("go2rtc_api_port", 1984)

    sub_rtsp = f"rtsp://127.0.0.1:{go2rtc_port}/sub"
    gst_pipeline = (
        f"gst-launch-1.0 -e -v "
        f"rtspsrc location={sub_rtsp} protocols=tcp latency=50 drop-on-latency=true "
        f"! rtph264depay ! h264parse config-interval=-1 "
        f"! rtph264pay pt=96 config-interval=1 aggregate-mode=zero-latency "
        f"! queue "
        f"! whipsink whip-endpoint={whip_endpoint} auth-token={whip_token} "
        f"stun-server=stun://stun.l.google.com:19302 use-link-headers=false"
    )

    return [
        "docker", "run", "-d",
        "--name", container_name,
        "--restart", "unless-stopped",
        "--network", "host",
        "-v", f"{go2rtc_config_path}:/config/go2rtc.yaml:ro",
        "-e", f"GST_PIPELINE={gst_pipeline}",
        "-e", f"GO2RTC_API_PORT={api_port}",
        docker_image,
    ]


def start_go2rtc_container(config: dict, config_dir: Path) -> tuple[bool, str]:
    """Stop existing container if any, generate config, start new container."""
    container_name = config["container_name"]

    subprocess.run(
        ["docker", "rm", "-f", container_name],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    generate_go2rtc_config(config, config_dir / "go2rtc.yaml")

    cmd = build_go2rtc_docker_command(config, config_dir)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        return True, result.stdout.strip()[:12]
    return False, (result.stderr or result.stdout or "").strip()


def go2rtc_streams_healthy(api_port: int = 1984) -> tuple[bool, str]:
    """Check go2rtc API for active stream producers."""
    try:
        resp = requests.get(f"http://127.0.0.1:{api_port}/api/streams", timeout=3)
        if not resp.ok:
            return False, f"api_http_{resp.status_code}"
        streams = resp.json()
        issues = []
        for name in ("main", "sub"):
            stream = streams.get(name)
            if not stream:
                issues.append(f"{name}:missing")
            elif not stream.get("producers"):
                issues.append(f"{name}:no_producers")
        if issues:
            return False, ",".join(issues)
        return True, "ok"
    except Exception as exc:
        return False, str(exc)


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def acquire_lock(lock_path: Path):
    ensure_parent(lock_path)
    fh = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.stderr.write("another yourmove-edge supervisor is already running\n")
        sys.exit(1)
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def install_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)


def main() -> int:
    parser = argparse.ArgumentParser(description="yourmove edge publisher supervisor")
    parser.add_argument("--config", required=True, help="Path to edge supervisor config JSON")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser()
    config = load_config(config_path)
    state_dir = Path(config.get("state_dir") or "~/.local/state/yourmove-edge").expanduser()
    lock_handle = acquire_lock(state_dir / f"{config['node_slug']}.lock")
    _ = lock_handle
    install_signal_handlers()

    rtsp_url = config["rtsp_url"]
    restart_command = config.get("restart_command", "")
    container_name = config["container_name"]
    node_slug = config["node_slug"]
    base_url = config.get("base_url", "https://yourmove.live")
    auth_token = config.get("auth_token", "")
    probe_interval = int(config.get("probe_interval_seconds", 15))
    probe_timeout = int(config.get("probe_timeout_seconds", 6))
    ssl_verify = bool(config.get("ssl_verify", True))
    restart_cooldown = int(config.get("restart_cooldown_seconds", 20))
    failure_threshold = int(config.get("failure_threshold", 2))
    clip_buffer_enabled = bool(config.get("clip_buffer_enabled", False))
    clip_pre_roll_seconds = float(config.get("clip_pre_roll_seconds", 3))
    clip_post_roll_seconds = float(config.get("clip_post_roll_seconds", 6))
    clip_trigger_offset_seconds = float(config.get("clip_trigger_offset_seconds", 0))
    clip_segment_seconds = int(config.get("clip_segment_seconds", 1))
    clip_buffer_window_seconds = int(config.get("clip_buffer_window_seconds", 30))
    clip_upload_timeout_seconds = int(config.get("clip_upload_timeout_seconds", 30))
    recorder_stall_threshold_seconds = int(config.get("recorder_stall_threshold_seconds", max(8, clip_segment_seconds * 6)))
    clip_stall_threshold_seconds = int(config.get("clip_stall_threshold_seconds", 90))
    clip_poll_error_threshold = int(config.get("clip_poll_error_threshold", 5))

    # go2rtc mode: enabled when rtsp_main_url is present in config
    use_go2rtc = bool(config.get("rtsp_main_url"))
    go2rtc_port = int(config.get("go2rtc_port", 8554))
    go2rtc_api_port = int(config.get("go2rtc_api_port", 1984))
    go2rtc_config_dir = state_dir / "go2rtc"
    go2rtc_stall_count = 0
    cv_log_path = state_dir / f"{node_slug}-cv.log"
    clip_state_path = state_dir / f"{node_slug}-clip-state.json"
    clip_segment_dir = state_dir / f"{node_slug}-segments"
    if clip_buffer_enabled:
        clip_segment_dir.mkdir(parents=True, exist_ok=True)

    if use_go2rtc:
        clip_recorder_source = f"rtsp://127.0.0.1:{go2rtc_port}/main"
    else:
        clip_recorder_source = rtsp_url
    cv_enabled = bool(config.get("cv_enabled", False))
    cv_process: subprocess.Popen | None = None

    consecutive_publish_failures = 0
    last_restart_at = 0.0
    last_health = ""
    clip_state = load_json(clip_state_path, {"processed_interaction_ids": [], "failed_attempts": {}})
    processed_interaction_ids = {int(x) for x in clip_state.get("processed_interaction_ids", []) if isinstance(x, int) or str(x).isdigit()}
    failed_attempts = {}
    for key, value in dict(clip_state.get("failed_attempts") or {}).items():
        try:
            failed_attempts[int(key)] = int(value)
        except Exception:
            continue
    recorder_process: subprocess.Popen | None = None
    latest_clip_result = ""
    last_clip_progress_at = time.time()
    last_clip_result_value = ""
    clip_jobs_error_count = 0
    recorder_restart_count = 0
    last_whip_packets = None
    whip_stall_count = 0
    whip_stall_threshold = int(config.get("whip_stall_threshold", 3))
    last_rtsp_bytes = None
    rtsp_stall_count = 0

    def _cleanup() -> None:
        stop_process(recorder_process, "segment_recorder")
        stop_process(cv_process, "cv_runtime")
    atexit.register(_cleanup)

    game_config_cache: dict | None = None
    aim_context_cache: dict | None = None
    game_event_posted_ids: set[int] = set()
    if cv_enabled:
        try:
            from edge.edge_cv_runtime import fetch_game_config
            game_config_cache = fetch_game_config(base_url, node_slug, auth_token, ssl_verify=ssl_verify)
        except Exception:
            pass

    if use_go2rtc:
        log(f"go2rtc mode enabled: main={config.get('rtsp_main_url')} sub={rtsp_url} port={go2rtc_port}")
    log(f"yourmove-edge supervisor started for {node_slug}")

    # In go2rtc mode, start the container on first launch
    if use_go2rtc:
        ok, detail = start_go2rtc_container(config, go2rtc_config_dir)
        if ok:
            log(f"go2rtc container started id={detail}")
            time.sleep(3)
        else:
            log(f"go2rtc container start failed: {detail}")

    if cv_enabled and clip_buffer_enabled:
        cv_process = start_cv_runtime(config_path, cv_log_path)
    while not SHUTDOWN_REQUESTED:
        running = container_running(container_name)
        if clip_buffer_enabled:
            rtsp_ok = True
            rtsp_detail = "probe-skipped"
        else:
            rtsp_ok, rtsp_detail = probe_rtsp(rtsp_url, probe_timeout)
        status_payload = None
        status_error = ""
        camera_live = None
        clip_backlog = 0
        clip_jobs_error = ""
        degraded_reason = ""

        if auth_token:
            status_payload, status_error = fetch_stream_status(base_url, node_slug, auth_token, probe_timeout, ssl_verify)
            if status_payload:
                camera_live = bool(status_payload.get("camera_participant_present")) and int(status_payload.get("camera_track_count") or 0) > 0
        current_pre_roll_seconds, current_post_roll_seconds, current_trigger_offset_seconds = runtime_clip_settings(config, status_payload)

        if not rtsp_ok:
            consecutive_publish_failures = 0
            health = f"camera-unreachable ({rtsp_detail})"
        elif not running:
            consecutive_publish_failures += 1
            health = "publisher-not-running"
        elif camera_live is False:
            consecutive_publish_failures += 1
            health = f"camera-not-live ({status_payload.get('diagnosis') if status_payload else status_error})"
        elif camera_live is True:
            consecutive_publish_failures = 0
            health = "healthy"
        else:
            consecutive_publish_failures = 0
            health = "healthy-no-remote-check"

        # go2rtc stream health check
        if use_go2rtc and running and rtsp_ok:
            go2rtc_ok, go2rtc_detail = go2rtc_streams_healthy(go2rtc_api_port)
            if not go2rtc_ok:
                go2rtc_stall_count += 1
                if go2rtc_stall_count >= failure_threshold:
                    health = f"go2rtc-unhealthy ({go2rtc_detail})"
                    consecutive_publish_failures = max(consecutive_publish_failures, failure_threshold)
                    log(f"go2rtc_unhealthy detail={go2rtc_detail} stall_count={go2rtc_stall_count}")
            else:
                go2rtc_stall_count = 0

        # Detect stale WHIP sessions: container running but no new packets sent.
        # Check rtspsrc bytes-received too — whipsink can keep ticking packets-sent
        # on keepalives after the RTSP source dies mid-session.
        if running and rtsp_ok:
            current_packets = whip_packets_sent(container_name)
            if current_packets is not None:
                if last_whip_packets is not None and current_packets <= last_whip_packets:
                    whip_stall_count += 1
                    if whip_stall_count >= whip_stall_threshold:
                        health = f"whip-stalled (packets stuck at {current_packets})"
                        consecutive_publish_failures = max(consecutive_publish_failures, failure_threshold)
                        log(f"whip_session_stalled packets_sent={current_packets} stall_count={whip_stall_count}")
                else:
                    whip_stall_count = 0
                last_whip_packets = current_packets

            current_rtsp_bytes = rtsp_bytes_received(container_name)
            if current_rtsp_bytes is not None:
                if last_rtsp_bytes is not None and current_rtsp_bytes <= last_rtsp_bytes:
                    rtsp_stall_count += 1
                    if rtsp_stall_count >= whip_stall_threshold:
                        health = f"rtsp-stalled (bytes stuck at {current_rtsp_bytes})"
                        consecutive_publish_failures = max(consecutive_publish_failures, failure_threshold)
                        log(f"rtsp_session_stalled bytes_received={current_rtsp_bytes} stall_count={rtsp_stall_count}")
                else:
                    rtsp_stall_count = 0
                last_rtsp_bytes = current_rtsp_bytes
        else:
            last_whip_packets = None
            whip_stall_count = 0
            last_rtsp_bytes = None
            rtsp_stall_count = 0

        if health != last_health:
            log(f"health={health} rtsp={'ok' if rtsp_ok else 'bad'} container={'running' if running else 'down'}")
            if status_payload:
                log("room_status=" + json.dumps({
                    "participant_count": status_payload.get("participant_count"),
                    "camera_participant_present": status_payload.get("camera_participant_present"),
                    "camera_track_count": status_payload.get("camera_track_count"),
                    "diagnosis": status_payload.get("diagnosis"),
                }))
            elif status_error:
                log(f"room_status_error={status_error}")
            last_health = health

        whip_stalled = whip_stall_count >= whip_stall_threshold
        rtsp_stalled = rtsp_stall_count >= whip_stall_threshold
        should_restart = (
            rtsp_ok
            and (not running or camera_live is False or whip_stalled or rtsp_stalled)
            and consecutive_publish_failures >= failure_threshold
            and (time.time() - last_restart_at) >= restart_cooldown
        )
        if should_restart:
            if rtsp_stalled:
                reason = "rtsp_stalled"
            elif whip_stalled:
                reason = "whip_stalled"
            else:
                reason = "publish_failure"
            log(f"restarting local publisher reason={reason}")
            last_whip_packets = None
            whip_stall_count = 0
            last_rtsp_bytes = None
            rtsp_stall_count = 0
            go2rtc_stall_count = 0
            if use_go2rtc:
                ok, output = start_go2rtc_container(config, go2rtc_config_dir)
            else:
                ok, output = restart_publisher(restart_command)
            last_restart_at = time.time()
            consecutive_publish_failures = 0
            if ok:
                log("restart command succeeded")
            else:
                log("restart command failed: " + output)

        if clip_buffer_enabled:
            if rtsp_ok and (recorder_process is None or recorder_process.poll() is not None):
                stop_process(recorder_process, "segment_recorder")
                recorder_process = start_segment_recorder(clip_recorder_source, clip_segment_dir, clip_segment_seconds)
                recorder_restart_count += 1
                log("clip buffer recorder started")
            newest_age = newest_segment_age_seconds(clip_segment_dir)
            if rtsp_ok and newest_age is not None and newest_age > recorder_stall_threshold_seconds:
                log(f"clip_recorder_stalled age_seconds={newest_age:.1f} restarting_recorder=1")
                stop_process(recorder_process, "segment_recorder")
                recorder_process = start_segment_recorder(clip_recorder_source, clip_segment_dir, clip_segment_seconds)
                recorder_restart_count += 1
            if auth_token:
                jobs, clip_jobs_error, aim_context_from_jobs = fetch_clip_jobs(base_url, node_slug, auth_token, probe_timeout, ssl_verify)
                if clip_jobs_error:
                    clip_jobs_error_count += 1
                    log(f"clip_jobs_error={clip_jobs_error}")
                else:
                    clip_jobs_error_count = 0
                    aim_context_cache = aim_context_from_jobs
                    clip_backlog = len(jobs)
                    for job in jobs:
                        interaction_id = int(job.get("interaction_id") or 0)
                        if not interaction_id or interaction_id in processed_interaction_ids:
                            continue
                        fired_at_value = str(job.get("fired_at") or "")
                        fired_at_ts = parse_job_timestamp(fired_at_value)
                        if not fired_at_ts:
                            continue
                        existing_segments = []
                        for seg in sorted(clip_segment_dir.glob("*.ts")):
                            try:
                                existing_segments.append(int(seg.stem))
                            except Exception:
                                continue
                        if existing_segments:
                            earliest_segment_ts = min(existing_segments)
                            stale_before = fired_at_ts + current_trigger_offset_seconds + current_post_roll_seconds
                            if stale_before < (earliest_segment_ts - max(2, clip_segment_seconds)):
                                fail_clip_job(
                                    base_url,
                                    node_slug,
                                    interaction_id,
                                    auth_token,
                                    "buffer window expired before clip could be assembled",
                                    clip_upload_timeout_seconds,
                                    ssl_verify,
                                )
                                processed_interaction_ids.add(interaction_id)
                                clip_state["processed_interaction_ids"] = sorted(processed_interaction_ids)[-200:]
                                failed_attempts.pop(interaction_id, None)
                                clip_state["failed_attempts"] = {str(k): v for k, v in failed_attempts.items()}
                                save_json(clip_state_path, clip_state)
                                log(f"clip_skipped_stale interaction={interaction_id}")
                                latest_clip_result = f"stale:{interaction_id}"
                                last_clip_progress_at = time.time()
                                continue
                        if cv_enabled and interaction_id not in game_event_posted_ids:
                            try:
                                fire_marker_path = state_dir / f"{node_slug}-fire-marker.json"
                                fire_marker_path.write_text(json.dumps({"interaction_id": interaction_id, "ts": time.time()}))
                            except Exception:
                                pass
                            try:
                                cv_state_path = state_dir / f"{node_slug}-cv-state.json"
                                if cv_state_path.exists():
                                    cv_state = json.loads(cv_state_path.read_text())
                                    from edge.edge_cv_runtime import evaluate_interaction, post_game_event
                                    aim_config = None
                                    if aim_context_cache:
                                        aim_config = dict(aim_context_cache)
                                        aim_config["pan"] = job.get("pan")
                                        aim_config["tilt"] = job.get("tilt")
                                    game_event = evaluate_interaction(cv_state, game_config_cache, interaction_id, aim_config=aim_config)
                                    if game_event:
                                        game_events_path = state_dir / f"{node_slug}-game-events-{interaction_id}.json"
                                        game_events_path.write_text(json.dumps([game_event]))
                                        post_game_event(base_url, node_slug, auth_token, game_event, ssl_verify=ssl_verify)
                                        log(f"game_event interaction={interaction_id} event={game_event.get('event')} score={game_event.get('score', {}).get('final', 0)}")
                                    game_event_posted_ids.add(interaction_id)
                            except Exception as exc:
                                log(f"game_event_failed interaction={interaction_id} error={exc}")
                        if time.time() < fired_at_ts + current_trigger_offset_seconds + current_post_roll_seconds + max(10, clip_segment_seconds * 3):
                            continue
                        output_path = state_dir / f"{node_slug}-clip-{interaction_id}.mp4"
                        ok, detail = build_clip_from_segments(
                            clip_segment_dir,
                            fired_at_ts,
                            current_pre_roll_seconds,
                            current_post_roll_seconds,
                            current_trigger_offset_seconds,
                            clip_segment_seconds,
                            output_path,
                        )
                        if not ok:
                            if detail != "waiting for post-roll segments":
                                log(f"clip_build_failed interaction={interaction_id} detail={detail}")
                                latest_clip_result = f"build_failed:{interaction_id}:{detail[:120]}"
                                failed_attempts[interaction_id] = failed_attempts.get(interaction_id, 0) + 1
                                clip_state["failed_attempts"] = {str(k): v for k, v in failed_attempts.items()}
                                save_json(clip_state_path, clip_state)
                                if failed_attempts[interaction_id] >= 3:
                                    fail_clip_job(
                                        base_url,
                                        node_slug,
                                        interaction_id,
                                        auth_token,
                                        detail,
                                        clip_upload_timeout_seconds,
                                        ssl_verify,
                                    )
                                    processed_interaction_ids.add(interaction_id)
                                    clip_state["processed_interaction_ids"] = sorted(processed_interaction_ids)[-200:]
                                    failed_attempts.pop(interaction_id, None)
                                    clip_state["failed_attempts"] = {str(k): v for k, v in failed_attempts.items()}
                                    save_json(clip_state_path, clip_state)
                            continue
                        if ok and cv_enabled:
                            try:
                                from edge.edge_cv_overlay import composite_overlay
                                from edge.edge_cv_runtime import build_overlay_events
                                game_events_path = state_dir / f"{node_slug}-game-events-{interaction_id}.json"
                                if game_events_path.exists():
                                    game_events = json.loads(game_events_path.read_text())
                                    clip_start_ms = int(fired_at_ts * 1000) - int(current_pre_roll_seconds * 1000)
                                    overlay_events = build_overlay_events(game_events, clip_start_ms)
                                    if overlay_events:
                                        overlay_path = state_dir / f"{node_slug}-clip-{interaction_id}-overlay.mp4"
                                        composite_overlay(output_path, overlay_path, overlay_events)
                                        if overlay_path.exists() and overlay_path.stat().st_size > 0:
                                            output_path.unlink(missing_ok=True)
                                            overlay_path.rename(output_path)
                                            log(f"clip_overlay_applied interaction={interaction_id} events={len(overlay_events)}")
                                    game_events_path.unlink(missing_ok=True)
                            except Exception as exc:
                                log(f"clip_overlay_failed interaction={interaction_id} error={exc}")
                        uploaded, upload_detail = upload_clip_job(
                            base_url,
                            node_slug,
                            interaction_id,
                            auth_token,
                            output_path,
                            clip_upload_timeout_seconds,
                            ssl_verify,
                        )
                        try:
                            output_path.unlink(missing_ok=True)
                        except Exception:
                            pass
                        if uploaded:
                            processed_interaction_ids.add(interaction_id)
                            clip_state["processed_interaction_ids"] = sorted(processed_interaction_ids)[-200:]
                            failed_attempts.pop(interaction_id, None)
                            clip_state["failed_attempts"] = {str(k): v for k, v in failed_attempts.items()}
                            save_json(clip_state_path, clip_state)
                            log(f"clip_uploaded interaction={interaction_id}")
                            latest_clip_result = f"uploaded:{interaction_id}"
                            last_clip_progress_at = time.time()
                        else:
                            log(f"clip_upload_failed interaction={interaction_id} detail={upload_detail}")
                            latest_clip_result = f"upload_failed:{interaction_id}:{upload_detail[:120]}"
                            failed_attempts[interaction_id] = failed_attempts.get(interaction_id, 0) + 1
                            clip_state["failed_attempts"] = {str(k): v for k, v in failed_attempts.items()}
                            save_json(clip_state_path, clip_state)
                            if failed_attempts[interaction_id] >= 3:
                                fail_clip_job(
                                    base_url,
                                    node_slug,
                                    interaction_id,
                                    auth_token,
                                    upload_detail,
                                    clip_upload_timeout_seconds,
                                    ssl_verify,
                                )
                                processed_interaction_ids.add(interaction_id)
                                clip_state["processed_interaction_ids"] = sorted(processed_interaction_ids)[-200:]
                                failed_attempts.pop(interaction_id, None)
                                clip_state["failed_attempts"] = {str(k): v for k, v in failed_attempts.items()}
                                save_json(clip_state_path, clip_state)
                    if clip_backlog == 0:
                        last_clip_progress_at = time.time()
            cleanup_old_segments(clip_segment_dir, clip_buffer_window_seconds)

        if latest_clip_result and latest_clip_result != last_clip_result_value:
            last_clip_result_value = latest_clip_result
            last_clip_progress_at = time.time()

        clip_stall_seconds = 0 if clip_backlog <= 0 else int(max(0, time.time() - last_clip_progress_at))
        if clip_buffer_enabled and clip_backlog > 0 and clip_stall_seconds >= clip_stall_threshold_seconds:
            degraded_reason = f"clip backlog stalled for {clip_stall_seconds}s"
            log(f"clip_backlog_stalled backlog={clip_backlog} stall_seconds={clip_stall_seconds}")
            stop_process(recorder_process, "segment_recorder")
            recorder_process = start_segment_recorder(clip_recorder_source, clip_segment_dir, clip_segment_seconds)
            recorder_restart_count += 1
            last_clip_progress_at = time.time()
        elif clip_jobs_error_count >= clip_poll_error_threshold and clip_jobs_error:
            degraded_reason = f"clip job polling failing ({clip_jobs_error_count} consecutive errors)"
        if auth_token:
            post_edge_heartbeat(
                base_url,
                node_slug,
                auth_token,
                {
                    "health": health,
                    "rtsp_ok": rtsp_ok,
                    "rtsp_detail": rtsp_detail,
                    "publisher_running": running,
                    "camera_live": camera_live,
                    "status_error": status_error,
                    "clip_buffer_enabled": clip_buffer_enabled,
                    "recorder_running": recorder_process is not None and recorder_process.poll() is None,
                    "clip_backlog": clip_backlog,
                    "latest_clip_result": latest_clip_result,
                    "clip_jobs_error": clip_jobs_error,
                    "clip_jobs_error_count": clip_jobs_error_count,
                    "clip_stall_seconds": clip_stall_seconds,
                    "recorder_restart_count": recorder_restart_count,
                    "degraded_reason": degraded_reason,
                    "whip_packets_sent": last_whip_packets,
                    "whip_stall_count": whip_stall_count,
                    "rtsp_bytes_received": last_rtsp_bytes,
                    "rtsp_stall_count": rtsp_stall_count,
                    "segment_age_seconds": newest_segment_age_seconds(clip_segment_dir),
                },
                probe_timeout,
                ssl_verify,
            )

        if cv_enabled and cv_process is not None and cv_process.poll() is not None:
            log(f"cv_runtime_exited code={cv_process.returncode}")
            cv_process = start_cv_runtime(config_path, cv_log_path)

        time.sleep(probe_interval)

    stop_process(recorder_process, "segment_recorder")
    stop_process(cv_process, "cv_runtime")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
