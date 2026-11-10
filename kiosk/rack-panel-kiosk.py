#!/usr/bin/env python3
"""rack-panel-kiosk: local side of a rack-panel touch screen (runs as root on the kiosk machine).

Stdlib only, listens on loopback. Serves the rack-panel page and proxies its
read API (/api/*, GET only) from the server (same origin for Chromium, last page kept for when the server
is down), stores the screen settings, drives the backlight (night mode, wake on
touch, presence through the front camera) and runs the three local actions: restart display, reboot, power off.
Holds the admin code (PBKDF2 hash, set on the screen) and, once it matches, asks rack-panel to restart
a VM/LXC or a Docker container (rack-panel only takes that from this machine's address).
"""
import hashlib
import hmac
import glob
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("UPSTREAM", "http://127.0.0.1:3006").rstrip("/")
LISTEN = os.environ.get("LISTEN", "127.0.0.1:3007")
STATE_DIR = os.environ.get("STATE_DIR", "/var/lib/rack-panel-kiosk")
# Wi-Fi interface for the Settings screen: WIFI_IF, else the first wireless one.
WIFI = os.environ.get("WIFI_IF") or next((p.split("/")[4] for p in sorted(glob.glob("/sys/class/net/*/wireless"))), "")
SETTINGS_FILE = os.path.join(STATE_DIR, "settings.json")
PAGE_FILE = os.path.join(STATE_DIR, "page.html")
ADMIN_FILE = os.path.join(STATE_DIR, "admin.json")
# Admin code: 4 to 8 digits; 5 wrong codes in a row lock it for 5 min (on-screen keypad, no keyboard).
MAX_FAILS, LOCK_S = 5, 300
# Presence (front camera): 160x120 grey frames at 2/s from ffmpeg, kept in memory only (never written,
# never sent). A frame where at least motion_pct % (setting, 3 by default) of the sampled pixels changed by
# over PIX_DELTA = someone. Measured on a Surface Pro 3 webcam: idle noise at most 1.1 % while the exposure settles,
# then ~0 %; someone walking by 6 to 24 %.
CAMERA = os.environ.get("CAMERA", "/dev/video0")
PIX_DELTA = 25
W, H = 160, 120
SLIDES = ["overview", "nodes", "hosts", "services", "docker", "storage", "network"]
# Slides that existed before settings.json recorded which ones it knew: a slide added later
# is shown on screens whose saved list predates it (the user never turned it off).
FIRST_SLIDES = ["overview", "nodes", "hosts", "services", "storage", "network"]

# Screens with two layouts (rings or bubbles), chosen with the button on the screen; absent = automatic.
VIEWS = {"hosts", "services", "docker"}
VIEW_MODES = {"tiles", "circles"}
LANGS = ("", "en", "fr")  # page language; "" = the server's default
# name: (default, min, max); booleans and the slide list are handled apart.
NUMBERS = {
    "brightness": (70, 5, 100), "night_start": (23, 0, 23), "night_end": (7, 0, 23), "wake_min": (5, 1, 60),
    "slide_s": (20, 5, 300), "idle_s": (120, 10, 900), "away_min": (10, 1, 120), "motion_pct": (3, 1, 30),
    "disk": (85, 50, 99), "temp": (85, 50, 110), "sw_temp": (75, 40, 100), "backup_h": (26, 2, 240),
}
ACTIONS = {
    "restart-display": ["systemctl", "restart", "kiosk"],
    "reboot": ["systemctl", "reboot"],
    "poweroff": ["systemctl", "poweroff"],
}


def defaults():
    d = {k: v[0] for k, v in NUMBERS.items()}
    d.update(night=False, presence=True, slides=list(SLIDES), views={}, lang="")
    return d


def clean(current, patch):
    """Merge a settings patch into current settings, ignoring unknown keys and bad values."""
    out = dict(current)
    for key, value in patch.items():
        if key in NUMBERS and isinstance(value, (int, float)) and not isinstance(value, bool):
            _, lo, hi = NUMBERS[key]
            out[key] = int(min(hi, max(lo, value)))
        elif key in ("night", "presence") and isinstance(value, bool):
            out[key] = value
        elif key == "lang" and value in LANGS:
            out[key] = value
        elif key == "views" and isinstance(value, dict):
            out[key] = {k: v for k, v in value.items() if k in VIEWS and v in VIEW_MODES}
        elif key == "slides" and isinstance(value, list):
            kept = [s for s in SLIDES if s in value]
            out[key] = kept or list(SLIDES)  # never an empty carousel
    return out


def load(saved):
    """Settings from settings.json, slides added since it was written switched on."""
    s = clean(defaults(), saved)
    known = saved.get("known_slides", FIRST_SLIDES)
    new = [x for x in SLIDES if x not in known]
    if new and "slides" in saved:
        s["slides"] = [x for x in SLIDES if x in s["slides"] or x in new]
    return s


def hash_code(code, salt):
    return hashlib.pbkdf2_hmac("sha256", code.encode(), salt, 200_000).hex()


def valid_code(code):
    return isinstance(code, str) and code.isascii() and code.isdigit() and 4 <= len(code) <= 8


class Admin:
    """The admin code: salted hash in admin.json (root only), wrong attempts counted in memory."""

    def __init__(self, path=ADMIN_FILE):
        self.path, self.fails, self.locked_until = path, 0, 0.0
        try:
            with open(path, encoding="utf-8") as f:
                self.stored = json.load(f)
        except (OSError, ValueError):
            self.stored = None

    def locked(self, now=None):
        return max(0, round(self.locked_until - (now or time.time())))

    def check(self, code, now=None):
        """None when the code is right, else the reason (no code yet, locked, wrong)."""
        now = now or time.time()
        if not self.stored:
            return "no admin code: set one in Settings"
        if self.locked(now):
            return f"too many attempts, try again in {self.locked(now)} s"
        if valid_code(code) and hmac.compare_digest(
                hash_code(code, bytes.fromhex(self.stored["salt"])), self.stored["hash"]):
            self.fails = 0
            return None
        self.fails += 1
        if self.fails >= MAX_FAILS:
            self.fails, self.locked_until = 0, now + LOCK_S
        return "wrong code"

    def set(self, old, new):
        """Set or change the code (the current one is needed once set). None or the reason."""
        if not valid_code(new):
            return "4 to 8 digits"
        if self.stored:
            err = self.check(old)
            if err:
                return err
        salt = os.urandom(16)
        self.stored = {"salt": salt.hex(), "hash": hash_code(new, salt)}
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
            json.dump(self.stored, f)
        os.replace(tmp, self.path)
        return None


def relayable(path):
    """Only rack-panel's read API goes upstream: /api/<name>, no traversal, no other prefix."""
    return path.startswith("/api/") and path != "/api/state" and all(
        part and part not in (".", "..") for part in path[len("/api/"):].split("/")) and "%" not in path


def motion(prev, cur, step=4):
    """Share (%) of sampled pixels that changed noticeably between two grey frames."""
    n = range(0, len(cur), step)
    return 100 * sum(1 for i in n if abs(cur[i] - prev[i]) > PIX_DELTA) / len(n)


def in_night(hour, start, end):
    if start == end:
        return False
    return start <= hour < end if start < end else hour >= start or hour < end


class Agent:
    def __init__(self):
        self.lock = threading.Lock()
        self.woke_until = 0.0
        self.seen = time.time()  # last motion in front of the camera (or touch)
        self.frame_at = 0.0      # last camera frame: the camera counts only while it delivers
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                self.settings = load(json.load(f))
        except (OSError, ValueError):
            self.settings = defaults()

    def save(self, patch):
        with self.lock:
            self.settings = clean(self.settings, patch)
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = SETTINGS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(dict(self.settings, known_slides=SLIDES), f, indent=1)
            os.replace(tmp, SETTINGS_FILE)
        self.apply()
        return self.settings

    def camera_ok(self, now):
        return self.settings["presence"] and now - self.frame_at < 30

    def dim(self, now=None):
        """Screen off at night, or when nobody has been in front of the camera for away_min."""
        now = now or time.time()
        s = self.settings
        if now <= self.woke_until:
            return False
        night = s["night"] and in_night(time.localtime(now).tm_hour, s["night_start"], s["night_end"])
        cam = self.camera_ok(now)
        present = cam and now - self.seen <= s["away_min"] * 60
        return (night or cam) and not present

    def wake(self):
        self.woke_until = time.time() + self.settings["wake_min"] * 60
        self.seen = time.time()
        self.apply()

    def camera(self):
        """Watch the front camera while presence is on; a dead camera never turns the screen off."""
        size = W * H
        while True:
            if not self.settings["presence"]:
                time.sleep(5)
                continue
            try:
                p = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-f", "v4l2", "-input_format", "yuyv422",
                                      "-video_size", f"{W}x{H}", "-framerate", "5", "-i", CAMERA,
                                      "-vf", "fps=2,format=gray", "-f", "rawvideo", "-"],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except OSError:  # ffmpeg missing
                time.sleep(60)
                continue
            prev, n = None, 0
            try:
                while self.settings["presence"]:
                    cur = p.stdout.read(size)
                    if len(cur) < size:
                        break
                    self.frame_at, n = time.time(), n + 1
                    if n > 6 and prev and motion(prev, cur) >= self.settings["motion_pct"]:  # first frames: exposure settling
                        was = self.dim()
                        self.seen = time.time()
                        if was:
                            self.apply()
                    prev = cur
            finally:
                p.kill()
                p.wait()
            time.sleep(5)

    def apply(self):
        for path in glob.glob("/sys/class/backlight/*/brightness")[:1]:
            top = int(open(os.path.join(os.path.dirname(path), "max_brightness")).read())
            want = 0 if self.dim() else max(1, top * self.settings["brightness"] // 100)
            if int(open(path).read()) != want:
                with open(path, "w") as f:
                    f.write(str(want))

    def loop(self):
        while True:
            try:
                self.apply()
            except OSError:
                pass
            time.sleep(20)


def read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def run(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def parse_iw(text):
    """`iw dev X link` output -> {key: value}; some lines are a bare "key:"."""
    pairs = (line.partition(":") for line in text.splitlines())
    return {k.strip(): v.strip() for k, sep, v in pairs if sep}


def system():
    ip = run("ip", "-4", "-o", "addr", "show", "scope", "global")
    wifi = parse_iw(run("iw", "dev", WIFI, "link"))
    bat = (glob.glob("/sys/class/power_supply/BAT*") or [None])[0]
    ac = (glob.glob("/sys/class/power_supply/AC*/online") or [None])[0]
    return {
        "ip": ip.split()[3].split("/")[0] if ip.split() else None,
        "ssid": wifi.get("SSID"),
        "signal": wifi.get("signal"),
        "battery": int(read(bat + "/capacity", "0")) if bat else None,
        "battery_status": read(bat + "/status") if bat else None,
        "ac": read(ac) == "1" if ac else None,
        "uptime": float(read("/proc/uptime", "0").split()[0]),
    }


agent = Agent()
admin = Admin()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path in ("/", "/index.html"):
            return self.page()
        if url.path == "/api/state":
            s = agent.settings
            qs = urllib.parse.urlencode({k: s[k] for k in ("disk", "temp", "sw_temp", "backup_h")})
            try:
                with urllib.request.urlopen(f"{UPSTREAM}/api/state?{qs}", timeout=10) as r:
                    return self.reply(200, r.read(), "application/json")
            except Exception as exc:
                return self.json(502, {"error": f"server unreachable ({exc})"})
        if relayable(url.path):
            return self.relay(f"{UPSTREAM}{url.path}?{url.query}")
        if url.path == "/local/status":
            now = time.time()
            return self.json(200, {"settings": agent.settings, "dim": agent.dim(), "system": system(),
                                   "slides": SLIDES, "presence": {"camera": agent.camera_ok(now),
                                                                  "seen_s": round(now - agent.seen)},
                                   "admin": {"set": bool(admin.stored), "locked_s": admin.locked(now)}})
        self.reply(404, b"not found\n", "text/plain")

    def do_POST(self):
        # Custom header: a cross-site page can't send it without a preflight we never answer.
        if self.headers.get("X-Rack-Panel") != "1":
            return self.json(403, {"error": "forbidden"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else {}
        except ValueError:
            return self.json(400, {"error": "bad json"})
        path = urllib.parse.urlsplit(self.path).path
        if path == "/local/settings" and isinstance(body, dict):
            return self.json(200, {"settings": agent.save(body), "dim": agent.dim()})
        if path == "/local/wake":
            agent.wake()
            return self.json(200, {"dim": agent.dim()})
        if path == "/local/code" and isinstance(body, dict):
            err = admin.set(body.get("old"), body.get("new"))
            return self.json(400 if err else 200, {"error": err} if err else {"ok": True})
        if path == "/local/restart" and isinstance(body, dict):
            err = admin.check(body.get("code"))
            if err:
                return self.json(403, {"error": err})
            return self.restart({"kind": body.get("kind"), "name": body.get("name")})
        if path == "/local/action" and body.get("action") in ACTIONS:
            # Delayed so the answer reaches the page before Chromium or the host goes away.
            subprocess.Popen(["systemd-run", "--on-active=2", *ACTIONS[body["action"]]])
            return self.json(200, {"ok": True})
        self.json(404, {"error": "not found"})

    def restart(self, what):
        """POST to rack-panel, which checks the name and holds the Proxmox token and the SSH key."""
        req = urllib.request.Request(f"{UPSTREAM}/api/restart", data=json.dumps(what).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=70) as r:
                return self.reply(200, r.read(), "application/json")
        except urllib.error.HTTPError as exc:
            return self.reply(exc.code, exc.read(), "application/json")
        except Exception as exc:
            return self.json(502, {"error": f"server unreachable ({exc})"})

    def relay(self, target):
        """GET-only pass-through to rack-panel, upstream status kept (404 = unknown name)."""
        try:
            with urllib.request.urlopen(target, timeout=15) as r:
                return self.reply(200, r.read(), "application/json")
        except urllib.error.HTTPError as exc:
            return self.reply(exc.code, exc.read(), "application/json")
        except Exception as exc:
            return self.json(502, {"error": f"server unreachable ({exc})"})

    def page(self):
        try:
            with urllib.request.urlopen(UPSTREAM + "/", timeout=10) as r:
                body = r.read()
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(PAGE_FILE, "wb") as f:
                f.write(body)
        except Exception:
            try:
                with open(PAGE_FILE, "rb") as f:  # server down: last known page, settings still work
                    body = f.read()
            except OSError:
                body = b"<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=10>" \
                       b"<body style='background:#0b0e13;color:#e7ecf3;font:24px system-ui;padding:40px'>" \
                       b"rack-panel server unreachable, retrying in 10 s."
        self.reply(200, body, "text/html; charset=utf-8")

    def json(self, code, obj):
        self.reply(code, json.dumps(obj).encode(), "application/json")

    def reply(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        if not (len(args) > 1 and str(args[1]).startswith(("2", "3"))):
            super().log_message(fmt, *args)


if __name__ == "__main__":
    threading.Thread(target=agent.loop, daemon=True).start()
    threading.Thread(target=agent.camera, daemon=True).start()
    host, port = LISTEN.rsplit(":", 1)
    ThreadingHTTPServer((host, int(port)), Handler).serve_forever()
