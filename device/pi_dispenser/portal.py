"""Captive portal Flask server for Pi dispenser provisioning."""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Callable

import requests as http_requests
from flask import Flask, request, render_template, redirect

from device.pi_dispenser.config import load_config, save_config

log = logging.getLogger(__name__)

PROVISION_URL = "https://yourmove.live/api/device/provision"


def _start_https_redirect():
    """Tiny HTTPS server on 443 that redirects everything to the HTTP portal."""
    import ssl
    import threading
    from http.server import HTTPServer, BaseHTTPRequestHandler

    cert_path = Path("/etc/yourmove/portal.pem")
    if not cert_path.exists():
        log.warning("No TLS cert at %s — skipping HTTPS redirect", cert_path)
        return

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://192.168.4.1/")
            self.end_headers()
        do_HEAD = do_GET
        def log_message(self, *args):
            pass

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile="/etc/yourmove/portal.crt", keyfile="/etc/yourmove/portal.key")
    try:
        server = HTTPServer(("0.0.0.0", 443), RedirectHandler)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        log.info("HTTPS redirect server started on :443")
    except Exception as e:
        log.warning("Failed to start HTTPS redirect: %s", e)


def create_app(
    *,
    config_path: Path,
    hardware_serial: str,
    camera_detected: bool = False,
    networks: list[str] | None = None,
    on_provisioned: Callable | None = None,
) -> Flask:
    _start_https_redirect()
    scanned_networks = networks or []
    app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates"))
    app.logger.setLevel(logging.DEBUG)

    @app.after_request
    def log_request(response):
        log.info("%s %s → %s", request.method, request.url, response.status_code)
        return response

    @app.route("/")
    def portal_root():
        claim = request.args.get("claim", "")
        return render_template(
            "setup.html",
            serial=hardware_serial,
            camera_detected=camera_detected,
            networks=scanned_networks,
            claim_code=claim,
            ssid="",
            message="",
            success=False,
        )

    @app.route("/generate_204")
    @app.route("/gen_204")
    def android_captive():
        return redirect("http://192.168.4.1/", code=302)

    @app.route("/<path:path>")
    def captive_catch_all(path):
        return redirect("http://192.168.4.1/", code=302)

    @app.route("/provision", methods=["POST"])
    def provision():
        ssid = (request.form.get("ssid") or "").strip()
        password = request.form.get("password", "")
        claim_code = (request.form.get("claim_code") or "").strip()

        if not ssid:
            return render_template(
                "setup.html", serial=hardware_serial,
                camera_detected=camera_detected, networks=scanned_networks,
                claim_code=claim_code, ssid=ssid,
                message="Wi-Fi network is required.", success=False,
            )

        cfg = load_config(config_path)
        cfg["provisioned"] = True
        cfg["wifi_ssid"] = ssid
        cfg["wifi_password"] = password
        cfg["claim_code"] = claim_code
        cfg["hardware_serial"] = hardware_serial
        save_config(cfg, config_path)

        log.info("WiFi and claim code saved — scheduling reboot")

        if on_provisioned:
            import threading
            threading.Timer(3.0, on_provisioned).start()

        return render_template(
            "setup.html", serial=hardware_serial,
            camera_detected=camera_detected, networks=[],
            claim_code=claim_code, ssid=ssid,
            message="Settings saved! The device is rebooting to connect to your Wi-Fi and finish setup. This network will disappear in a few seconds.",
            success=True,
        )

    return app


_wifi_cache: dict = {"networks": [], "ts": 0.0}
_WIFI_CACHE_TTL = 30.0


def _scan_wifi() -> list[str]:
    import time
    now = time.monotonic()
    if _wifi_cache["networks"] and (now - _wifi_cache["ts"]) < _WIFI_CACHE_TTL:
        return _wifi_cache["networks"]
    try:
        result = subprocess.run(
            ["iwlist", "wlan0", "scan"],
            capture_output=True, text=True, timeout=15,
        )
        networks = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("ESSID:"):
                ssid = line.split(":", 1)[1].strip('"')
                if ssid and ssid not in networks:
                    networks.append(ssid)
        _wifi_cache["networks"] = sorted(networks)
        _wifi_cache["ts"] = now
        return _wifi_cache["networks"]
    except Exception as e:
        log.warning("WiFi scan failed: %s", e)
        return _wifi_cache["networks"] or []


def _test_wifi(ssid: str, password: str) -> tuple[bool, str]:
    try:
        subprocess.run(
            ["wpa_cli", "-i", "wlan0", "disconnect"],
            capture_output=True, timeout=5,
        )
        conf = f'network={{\n  ssid="{ssid}"\n  psk="{password}"\n}}\n'
        tmp = Path("/tmp/wpa_test.conf")
        tmp.write_text(conf)
        result = subprocess.run(
            ["wpa_supplicant", "-i", "wlan0", "-c", str(tmp), "-B"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return False, result.stderr.strip() or "wpa_supplicant failed"

        import time
        for _ in range(15):
            time.sleep(1)
            check = subprocess.run(
                ["wpa_cli", "-i", "wlan0", "status"],
                capture_output=True, text=True, timeout=5,
            )
            if "wpa_state=COMPLETED" in check.stdout:
                subprocess.run(
                    ["dhclient", "-1", "wlan0"],
                    capture_output=True, timeout=15,
                )
                return True, ""
        return False, "Connection timed out"
    except Exception as e:
        return False, str(e)


def _call_provision_api(
    hardware_serial: str, claim_code: str = ""
) -> tuple[bool, int, dict | str]:
    """Returns (success, http_status, data_or_error)."""
    try:
        payload = {
            "hardware_serial": hardware_serial,
            "firmware_version": "pi-dispenser-0.1.0",
            "runtime_type": "pi_gateway",
        }
        if claim_code:
            payload["claim_code"] = claim_code
        r = http_requests.post(PROVISION_URL, json=payload, timeout=15)
        if r.status_code != 200:
            return False, r.status_code, r.text[:200]
        data = r.json()
        if not data.get("platform_node_id") or not data.get("mqtt_username"):
            return False, r.status_code, "Missing credentials in response"
        return True, r.status_code, data
    except Exception as e:
        return False, 0, str(e)
