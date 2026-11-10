"""python test_rack_panel_kiosk.py — settings validation and the night window."""
import importlib.util
import os
import tempfile
import time

os.environ["STATE_DIR"] = tempfile.mkdtemp()
spec = importlib.util.spec_from_file_location("agent", os.path.join(os.path.dirname(__file__), "rack-panel-kiosk.py"))
ag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ag)

d = ag.defaults()
s = ag.clean(d, {"brightness": 500, "night": "yes", "slide_s": 3, "evil": 1, "temp": True,
                 "slides": ["network", "bogus", "overview"]})
assert s["brightness"] == 100 and s["slide_s"] == 5          # clamped
assert s["night"] is False and "evil" not in s               # wrong type / unknown key ignored
assert s["temp"] == 85                                       # bool is not a number
assert s["slides"] == ["overview", "network"]                # known slides, carousel order
assert ag.clean(d, {"slides": []})["slides"] == ag.SLIDES    # never an empty carousel
assert ag.clean(d, {"lang": "fr"})["lang"] == "fr" and ag.clean(d, {"lang": "de"})["lang"] == ""

assert ag.in_night(23, 23, 7) and ag.in_night(3, 23, 7) and not ag.in_night(7, 23, 7)
assert ag.in_night(13, 12, 14) and not ag.in_night(14, 12, 14)
assert not ag.in_night(5, 6, 6)                              # empty window

iw = "Connected to 50:1a:c5:00:00:01 (on wlp1s0)\n\tSSID: LabWifi\n\tsignal: -62 dBm\n\tbss flags:\n\tdtim period: 3\n"
assert ag.parse_iw(iw)["SSID"] == "LabWifi" and ag.parse_iw(iw)["signal"] == "-62 dBm"  # bare "key:" line ok

saved = ag.agent.save({"night": True})                       # persisted to STATE_DIR
assert ag.Agent().settings["night"] is True and saved["night"] is True
# Relay: rack-panel's read API only.
assert ag.relayable("/api/detail")
for bad in ("/api/state", "/api/", "/api/../local/status", "/api/%2e%2e/x", "/local/action", "/apix", "/"):
    assert not ag.relayable(bad), bad
# A slide added after the settings were saved shows up; one switched off since stays off.
old = {"slides": ["overview", "network"]}
assert ag.load(old)["slides"] == ["overview", "docker", "network"]
assert ag.load(dict(old, known_slides=ag.SLIDES))["slides"] == ["overview", "network"]
assert ag.load({})["slides"] == ag.SLIDES
ag.agent.save({"slides": ["overview"]})
assert ag.Agent().settings["slides"] == ["overview"]          # known_slides persisted: no re-adding
# Layout per screen: known screens and modes only, kept across restarts.
v = ag.clean(d, {"views": {"hosts": "circles", "docker": "tiles", "nodes": "circles", "services": "evil"}})["views"]
assert v == {"hosts": "circles", "docker": "tiles"}, v
assert ag.clean(d, {"views": "circles"})["views"] == {}
ag.agent.save({"views": {"docker": "circles"}})
assert ag.Agent().settings["views"] == {"docker": "circles"}
# Presence: motion share between frames; the screen sleeps only when the camera works and nobody came.
assert ag.motion(bytes(100), bytes(100)) == 0 and ag.motion(bytes(100), bytes([200] * 100)) == 100
assert ag.motion(bytes(8), bytes([30, 0, 0, 0, 0, 0, 0, 0])) == 50     # every 4th pixel sampled
a = ag.Agent()
a.settings.update(night=False, presence=True, away_min=10)
t = 1_000_000.0
a.frame_at, a.seen, a.woke_until = t, t - 60, 0
assert not a.dim(t)                                            # someone a minute ago
a.seen = t - 11 * 60
assert a.dim(t)                                                # nobody for 11 min
a.frame_at = t - 120
assert not a.dim(t)                                            # camera silent: never dark because of it
a.frame_at, a.settings["presence"] = t, False
assert not a.dim(t)                                            # presence off: daytime unchanged
a.settings["presence"], a.woke_until = True, t + 60
assert not a.dim(t)                                            # touched: awake for wake_min
assert ag.clean(d, {"presence": False, "away_min": 0})["away_min"] == 1
# Admin code (lot 6): digits only, hashed, the current one needed to change it, locked after 5 misses.
ad = ag.Admin(os.path.join(os.environ["STATE_DIR"], "admin.json"))
assert ad.check("1234") == "no admin code: set one in Settings"
assert ad.set("", "12a4") and ad.set("", "123") and ad.set("", "123456789")
assert ad.set("", "2580") is None and "2580" not in open(ad.path).read()
assert oct(os.stat(ad.path).st_mode & 0o777) in ("0o600", "0o666")  # 600 on Linux (Windows ignores modes)
assert ad.check("2580") is None and ad.check("0000") == "wrong code"
assert ad.set("0000", "1111") == "wrong code" and ad.set("2580", "1111") is None
ad2 = ag.Admin(ad.path)                                         # survives a restart
assert ad2.check("1111") is None
for _ in range(5):
    ad2.check("9999")
assert ad2.check("1111").startswith("too many attempts")           # locked, even with the right code
assert ad2.check("1111", time.time() + 301) is None             # unlocked after 5 min
print("ok")
