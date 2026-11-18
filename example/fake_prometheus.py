#!/usr/bin/env python3
"""A made-up rack for trying rack-panel without your own monitoring.

Answers rack-panel's PromQL (instant and range) and Cairn Map's /api/map for a
fictitious lab: three Proxmox nodes, a NAS, a backup box, a dozen guests, two
MikroTik devices, Uptime Kuma probes and Docker containers. Values drift slowly
so the screen moves. Stdlib only.

    python3 example/fake_prometheus.py &          # 127.0.0.1:9099
    python3 rack-panel.py -c example/config.json  # http://127.0.0.1:3006
"""
import importlib.util
import json
import math
import os
import re
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
GiB, TiB = 2 ** 30, 2 ** 40

# name: (cpu, mem, disk, ram GiB, node or None for bare metal, description)
HOSTS = {
    "PVE1": (.31, .58, .41, 64, None, "Proxmox node"), "PVE2": (.22, .66, .38, 64, None, "Proxmox node"),
    "PVE3": (.12, .44, .52, 32, None, "Proxmox node"), "NAS": (.09, .71, .22, 32, None, "ZFS storage, NFS and SMB shares"),
    "BACKUP": (.04, .35, .18, 16, None, "Off-site copy of the NAS"), "KIOSK": (.18, .52, .33, 4, None, "This rack screen"),
    "WEB": (.06, .41, .35, 2, "PVE1", "Public website"), "DB": (.14, .77, .61, 8, "PVE1", "PostgreSQL"),
    "MAIL": (.03, .55, .47, 4, "PVE1", "Mail server"), "DNS": (.02, .23, .19, 1, "PVE2", "DNS resolver with ad blocking"),
    "PROXY": (.08, .36, .27, 2, "PVE2", "Reverse proxy and SSO"), "GIT": (.05, .62, .58, 4, "PVE2", "Git forge"),
    "CI": (.47, .69, .66, 8, "PVE2", "CI runner"), "MONITOR": (.11, .73, .71, 8, "PVE3", "Prometheus, Loki, Grafana"),
    "MEDIA": (.19, .48, .87, 8, "PVE3", "Media server"), "HOME": (.07, .52, .31, 4, "PVE3", "Home automation"),
    "PBS": (.03, .28, .24, 4, "PVE3", "Proxmox Backup Server"),
}
DOWN = {"LEGACY": "PVE3"}  # seen today, not answering now
TEMPS = {"PVE1": 54, "PVE2": 49, "PVE3": 46, "NAS": 41, "BACKUP": 38, "KIOSK": 47}
PROBES = [  # name, type, url, status (0 down, 1 up, 2 pending, 3 maintenance), ms
    ("Website", "http", "https://www.example.com", 1, 142), ("Webmail", "http", "https://mail.example.com", 1, 188),
    ("Git", "keyword", "https://git.example.com", 1, 96), ("Status page", "http", "https://status.example.com", 1, 121),
    ("Grafana", "http", "https://grafana.lab.home", 1, 38), ("Home automation", "http", "https://home.lab.home", 1, 44),
    ("Proxy dashboard", "http", "https://proxy.lab.home", 1, 21), ("NAS shares", "http", "https://nas.lab.home", 1, 30),
    ("Backup server", "json-query", "https://pbs.lab.home:8007", 3, None), ("Media", "http", "http://192.0.2.30:8096", 1, 64),
    ("Printer", "ping", "", 0, None), ("Ping NAS", "ping", "", 1, 1),
]
CONTAINERS = {  # host: [(name, cpu, mem MiB, restarts today)]
    "PROXY": [("traefik", .02, 80, 0), ("sso", .01, 60, 0), ("crowdsec", .03, 140, 0)],
    "MONITOR": [("prometheus", .09, 1900, 0), ("grafana", .02, 210, 0), ("loki", .05, 780, 0), ("otel-gateway", .03, 120, 0)],
    "GIT": [("forgejo", .03, 340, 0), ("forgejo-db", .02, 260, 0)],
    "HOME": [("homeassistant", .06, 520, 1), ("mosquitto", .01, 12, 0), ("zigbee2mqtt", .02, 150, 0)],
    "MEDIA": [("jellyfin", .21, 1400, 0)], "WEB": [("site", .01, 40, 0)], "CI": [("runner", .12, 300, 0)],
}
STOPPED = [("MONITOR", "alertmanager")]
POOLS = {"NAS": ("tank", 9.6 * TiB, {"tank/media": 3.9, "tank/photos": 1.6, "tank/backups": 1.1, "tank/home": .4}),
         "BACKUP": ("vault", 7.2 * TiB, {"vault/nas": 5.6})}
SWITCHES = {"CORE": ("RB5009UG+S+", 6, 47), "ACCESS": ("CRS326-24G-2S+", 3, 52)}
PORTS = {("CORE", "ether1"): 4.1e7, ("CORE", "sfp-sfpplus1"): 1.9e8, ("CORE", "ether2"): 1.2e8, ("CORE", "ether3"): 6.3e7,
         ("CORE", "ether4"): 2.7e7, ("CORE", "bond-access"): 1.9e8, ("CORE", "ether5"): 3.3e6, ("ACCESS", "ether1"): 3.1e7, ("ACCESS", "ether2"): 8.8e6, ("ACCESS", "ether5"): 2.2e6,
         ("ACCESS", "ether8"): 1.4e6, ("ACCESS", "ether24"): 9.1e5, ("ACCESS", "sfp-sfpplus1"): 1.9e8}
NEIGHBORS = [("CORE", "ACCESS", "sfp-sfpplus1,bond-access,bridge"), ("CORE", "AP1", "ether5,bridge"), ("ACCESS", "CORE", "sfp-sfpplus1,bond-core,bridge"),
             ("ACCESS", "CORE", "ether24,bridge"), ("CORE", "ACCESS", "mgmt99")]
BACKED_UP = {"WEB": 20, "DB": 20, "MAIL": 21, "DNS": 22, "PROXY": 22, "GIT": 20, "MONITOR": 21, "MEDIA": 41, "HOME": 20}
STORAGE_LINKS = {("NAS", "enp1s0f0"): 6.1e8, ("NAS", "enp1s0f1"): 2.4e8}


def wave(key, now, amp=0.12):
    """Slow drift around 1 (each element its own phase), so the screen moves between polls."""
    h = sum(map(ord, key)) % 97
    return 1 + amp * math.sin(now / 90 + h) * math.cos(now / 370 + h / 3)


def rows(now):
    """Instant rows of every state query, by rack-panel's query key."""
    r = {k: [] for k in ("cpu", "mem", "arc", "seen", "up", "fs", "temp", "fans", "net", "kuma", "kuma_ms", "wan_rx", "wan_tx",
                         "sw_cpu", "sw_temp", "port_bps", "neighbors", "nic_bps", "ct_cpu", "ct_mem", "ct_memp",
                         "ct_restarts", "ct_seen", "pbs_used", "pbs_size", "pbs_last")}
    for h, (cpu, mem, disk, ram, _, _) in HOSTS.items():
        hn, total = {"host_name": h}, ram * GiB
        r["cpu"].append((hn, min(.99, cpu * wave(h, now, .4))))
        used = min(.97, mem * wave(h + "m", now, .03))
        for st, share in (("used", used), ("cached", (1 - used) * .5), ("buffered", .01), ("free", (1 - used) * .5 - .01)):
            r["mem"].append(({**hn, "state": st}, total * share))
        r["seen"].append((hn, 1))
        r["up"].append((hn, 1))
        root = 100 * GiB if ram > 8 else 32 * GiB
        for st, v in (("used", root * disk), ("free", root * (1 - disk))):
            r["fs"].append(({**hn, "mountpoint": "/", "device": "/dev/sda2", "type": "ext4", "state": st}, v))
        r["net"] += [({**hn, "direction": "receive"}, 4e5 * (1 + cpu * 9) * wave(h + "r", now, .5)),
                     ({**hn, "direction": "transmit"}, 2e5 * (1 + cpu * 9) * wave(h + "t", now, .5))]
        if h in TEMPS:
            r["temp"].append((hn, TEMPS[h] * wave(h + "c", now, .06)))
            r["fans"] += [({**hn, "sensor": s}, rpm * wave(h + s, now, .06)) for s, rpm in (("fan1", 1450), ("fan2", 1100))]
    r["arc"].append(({"host_name": "NAS"}, 9 * GiB))
    for h in DOWN:
        r["seen"].append(({"host_name": h}, 1))
    for h, (pool, size, sets) in POOLS.items():
        used = sum(sets.values()) * TiB
        for ds, tb in sets.items():
            for st, v in (("used", tb * TiB), ("free", size - used)):
                r["fs"].append(({"host_name": h, "mountpoint": "/" + ds, "device": ds, "type": "zfs", "state": st}, v))
    for name, typ, url, st, ms in PROBES:
        m = {"monitor_name": name, "monitor_type": typ, "monitor_url": url}
        r["kuma"].append((m, st))
        if ms:
            r["kuma_ms"].append(({"monitor_name": name}, ms * wave(name, now, .3)))
    r["wan_rx"] = [({}, 186 * wave("wanr", now, .5))]
    r["wan_tx"] = [({}, 23 * wave("want", now, .5))]
    for sw, (model, cpu, temp) in SWITCHES.items():
        r["sw_cpu"].append(({"routerboard_name": sw, "board_name": model}, cpu * wave(sw, now, .5)))
        r["sw_temp"].append(({"routerboard_name": sw}, temp * wave(sw + "c", now, .04)))
    for (sw, port), bps in PORTS.items():
        r["port_bps"].append(({"routerboard_name": sw, "name": port}, bps * wave(sw + port, now, .4)))
    for sw, ident, ifs in NEIGHBORS:
        r["neighbors"].append(({"routerboard_name": sw, "identity": ident, "interface": ifs}, 1))
    for (h, dev), bps in STORAGE_LINKS.items():
        r["nic_bps"].append(({"host_name": h, "device": dev}, bps * wave(h + dev, now, .5)))
    for h, cts in CONTAINERS.items():
        for name, cpu, mib, restarts in cts:
            m = {"host_name": h, "container_name": name}
            r["ct_cpu"].append((m, cpu * wave(h + name, now, .5)))
            r["ct_mem"].append((m, mib * 2 ** 20 * wave(h + name + "m", now, .05)))
            r["ct_memp"].append((m, mib / (HOSTS[h][3] * 1024)))
            r["ct_restarts"].append((m, restarts))
            r["ct_seen"].append((m, 1))
    for h, name in STOPPED:
        r["ct_seen"].append(({"host_name": h, "container_name": name}, 1))
    r["pbs_used"] = [({"datastore": "main"}, 2.9 * TiB)]
    r["pbs_size"] = [({"datastore": "main"}, 4 * TiB)]
    r["pbs_last"] = [({"vm_name": g}, now - h * 3600) for g, h in BACKED_UP.items()]
    return r


def cairn_map():
    """A Cairn Map /api/map for the same lab: where each machine is plugged, which guests run where."""
    nodes = {}

    def add(**x):
        nodes[str(len(nodes))] = x
    for name, kind in (("CORE", "router"), ("ACCESS", "switch"), ("AP1", "ap")):
        add(family="physical", name=name, kind=kind)
    plugs = {"PVE1": ("CORE", "ether2"), "PVE2": ("CORE", "ether3"), "PVE3": ("CORE", "ether4"),
             "NAS": ("ACCESS", "ether1"), "BACKUP": ("ACCESS", "ether2"), "KIOSK": ("CORE", "ether5")}
    for i, (name, kind) in enumerate((("PVE1", "node"), ("PVE2", "node"), ("PVE3", "node"), ("NAS", "nas"),
                                       ("BACKUP", "server"), ("KIOSK", "pc"))):
        also = [f"net:core:vlan10:{plugs[name][0].lower()}:{plugs[name][1]}:52:54:00:00:00:{i:02x}"] if name in plugs else []
        if name.startswith("PVE"):
            also.append(f"net:core:vlan99:access:ether{5 + i}:52:54:00:00:99:{i:02x}")
        add(family="physical", name=name, kind=kind, also=also, attrs={"description": HOSTS[name][5]})
    for g, (*_, node, desc) in HOSTS.items():
        if node:
            add(family="hypervisor", kind="lxc" if g in ("DNS", "PROXY", "WEB") else "vm", name=g,
                path=[node.lower(), g.lower()], attrs={"description": desc})
            add(family="network", kind="server", name=g.lower(), path=["vlan10", "core", plugs[node][1], g.lower()])
    add(family="hypervisor", kind="vm", name="LEGACY", path=["pve3", "legacy"])
    add(family="network", kind="router", name="isp-box", path=["vlan10", "core", "ether1", "isp-box"])
    add(family="network", kind="server", name="kiosk", path=["vlan10", "ap1", "wlan1", "kiosk"])
    for i, (dev, kind, port) in enumerate((("pve1", "server", "ether5"), ("pve2", "server", "ether6"), ("pve3", "server", "ether7"))):
        add(family="network", kind=kind, name=dev, path=["vlan99", "access", port, dev])
    for dev, port in (("thermostat", "ether10"), ("doorbell", "ether11"), ("plug-desk", "ether5")):
        add(family="network", kind="iot", name=dev, path=["vlan20", "access" if port != "ether5" else "core", port, dev])
    return {"nodes": nodes}


# ---- PromQL the detail sheets send: answered by shape, not parsed ----
def series_labels(expr):
    if '"dir", "receive"' in expr:
        return "dir", ["receive", "transmit"]
    if "by (direction)" in expr:
        return "direction", ["receive", "transmit"]
    if "by (operation)" in expr:
        return "operation", ["read", "write"]
    if "system_cpu_load_average" in expr:
        return "__name__", [f"system_cpu_load_average_{x}" for x in ("1m", "5m", "15m")]
    if "mktxp_system_(cpu|switch|phy)_temperature" in expr:
        return "__name__", ["mktxp_system_cpu_temperature", "mktxp_system_switch_temperature"]
    if '"k", "ok"' in expr:
        return "k", ["ok", "total"]
    if "fan_rpm" in expr:  # only the hosts that report a temperature have fans to show
        name = re.search(r'host_name="([^"]+)"', expr)
        return "sensor", ["fan1", "fan2"] if name and name[1] in TEMPS else []
    return None, [None]


def base_value(expr, label):
    """A plausible level for a detail chart, from the element's state when the query names one."""
    name = (re.search(r'(?:host_name|monitor_name|routerboard_name)="([^"]+)"', expr) or [None, ""])[1]
    host = HOSTS.get(name)
    if "monitor_status" in expr:
        return next((p[3] for p in PROBES if p[0] == name), 1)
    if "monitor_response_time" in expr:
        return next((p[4] or 50 for p in PROBES if p[0] == name), 50)
    if "fan_rpm" in expr:
        return {"fan1": 1450, "fan2": 1100}[label]
    if "temperature" in expr or "hwmon" in expr:
        return TEMPS.get(name, SWITCHES.get(name, (0, 0, 45))[2])
    if "load_average" in expr:
        return {"1m": 1.4, "5m": 1.1, "15m": .9}[label[-2:] if label[-3:] != "15m" else "15m"] if label else 1
    if "count(" in expr and "monitor_status" not in expr:
        return len(HOSTS)
    if '"k", "ok"' in expr:
        return len(PROBES) - (1 if label == "ok" else 0)
    if "pbs_used" in expr and "/" in expr or "device=~" in expr and "/ (" in expr:
        return .64
    if "pbs_used" in expr or ('state="used"' in expr and "device=~" in expr):
        return 2.9 * TiB
    if "container_memory_usage_total_bytes" in expr:
        return 400 * 2 ** 20
    if "container_cpu" in expr:
        return .04
    if "system_memory_usage_bytes" in expr and "/" in expr:
        return host[1] if host else .5
    if "cpu" in expr and ("/ sum" in expr or "/ 100" in expr):
        return host[0] if host else .2
    if "mktxp_system_free_memory" in expr:
        return .41
    if "disk_io" in expr or "blockio" in expr:
        return 3.2e6 if label == "write" else 1.1e6
    if "network" in expr or "mktxp_interface" in expr:
        return (8e7 if "routerboard" in expr else 4e6) * (1 if label in ("receive", None) else .35)
    return 1


def range_rows(expr, start, end, step):
    key, labels = series_labels(expr)
    out = []
    for lab in labels:
        b = base_value(expr, lab)
        noisy = "status" not in expr and b > 0
        pts = []
        for t in range(int(start), int(end) + 1, int(step)):
            v = b * (wave(expr[-40:] + str(lab), t, .25) if noisy else 1)
            pts.append([t, str(min(v, .999) if 0 < b < 1 and "load" not in expr else v)])
        out.append({"metric": {key: lab} if key else {}, "values": pts})
    return out


def instant_extra(expr):
    """Facts and bars of the detail sheets."""
    one = lambda v: [{"metric": {}, "value": [0, str(v)]}]  # noqa: E731
    name = (re.search(r'(?:host_name|routerboard_name|datastore)="([^"]+)"', expr) or [None, ""])[1]
    if "topk(8" in expr:
        if "mountpoint" in expr:
            items = [("mountpoint", m, v) for m, v in (("/", .41), ("/var", .33), ("/boot/efi", .06))]
        elif "pbs_snapshot_vm_last_timestamp" in expr:
            items = [("vm_name", g, h * 3600) for g, h in sorted(BACKED_UP.items(), key=lambda x: -x[1])[:8]]
        elif "by (device)" in expr:
            pool = next((p for h, p in POOLS.items() if h == name), POOLS["NAS"])
            items = [("device", d, tb * TiB) for d, tb in pool[2].items()]
        else:
            items = [("name", p, b) for (sw, p), b in PORTS.items() if sw == name]
        return [{"metric": {k: n}, "value": [0, str(v)]} for k, n, v in items]
    if "system_cpu_logical_count" in expr:
        return one(16 if name.startswith("PVE") else 4)
    if "system_processes_count" in expr:
        return one(312)
    if "system_memory_usage_bytes" in expr:
        return one(HOSTS.get(name, (0, 0, 0, 8))[3] * GiB)
    if "monitor_cert_days_remaining" in expr:
        return one(61)
    if "monitor_status" in expr:
        return one(.998)
    if "mktxp_system_uptime" in expr:
        return one(23 * 86400)
    if "mktxp_system_cpu_count" in expr:
        return one(4)
    if "pbs_snapshot_count" in expr:
        return one(214)
    if "pbs_snapshot_vm_last_timestamp" in expr:
        return one(len(BACKED_UP))
    if "pbs_size" in expr:
        return one(4 * TiB)
    if "container_restarts_total" in expr:
        return one(2)
    if "container_memory_usage_limit_bytes" in expr:
        return one(2 * GiB)
    if "max_over_time" in expr:
        return one(3.1e8)
    if "device=~" in expr:
        return one(POOLS.get(name, POOLS["NAS"])[1] * (.36 if 'state="free"' in expr and "+" not in expr else 1))
    return []


def load_rack_panel():
    spec = importlib.util.spec_from_file_location("rack_panel", os.path.join(HERE, "..", "rack-panel.py"))
    rp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rp)
    rp.configure(rp.load_config(os.path.join(HERE, "config.json")))
    return rp


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        now = time.time()
        if url.path == "/api/map":
            return self.send(cairn_map())
        if url.path == "/api/v1/query":
            key = BY_EXPR.get(qs.get("query"))
            result = ([{"metric": m, "value": [now, str(v)]} for m, v in rows(now)[key]] if key
                      else instant_extra(qs.get("query", "")))
            return self.send({"status": "success", "data": {"resultType": "vector", "result": result}})
        if url.path == "/api/v1/query_range":
            result = range_rows(qs["query"], float(qs["start"]), float(qs["end"]), float(qs["step"]))
            return self.send({"status": "success", "data": {"resultType": "matrix", "result": result}})
        self.send_error(404)

    def send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    rp = load_rack_panel()
    BY_EXPR = {v: k for k, v in rp.QUERIES.items()}
    listen = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1:9099"
    host, port = listen.rsplit(":", 1)
    print(f"fake Prometheus + Cairn Map on http://{listen}", flush=True)
    ThreadingHTTPServer((host, int(port)), Handler).serve_forever()
