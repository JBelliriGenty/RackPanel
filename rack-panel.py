#!/usr/bin/env python3
"""rack-panel: live state of a server rack, for a touch screen next to it.

Stdlib only. Runs a fixed set of PromQL queries (never a client-supplied one),
folds them into one JSON document (/api/state) and serves the carousel page (/).
Each source is a module switched on in config.json: see docs/configuration.md.
"""
import argparse
import difflib
import json
import math
import os
import re
import ssl
import string
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.1"
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "page.html")
CACHE_SECONDS = 10
TOPOLOGY_SECONDS = 300  # Cairn Map is re-read every 5 min
# Uptime Kuma probes that count as web services (the Services screen); the rest only feed the alerts.
WEB_PROBES = ("http", "keyword", "json-query")
_CT = "host_name, container_name"
CT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
# Host temperature = CPU, disks, board. Network cards (10 GbE, Wi-Fi) run hot by design
# (a 10 GbE NIC sits at ~76 °C) and would paint a healthy host orange; sensors above 110 °C are bogus.
HOST_TEMP = ('node_hwmon_temp_celsius < 110 unless on (host_name, chip)'
             ' node_hwmon_chip_names{chip_name=~"en.*|eth.*|iwlwifi.*|mt7.*"}')
ARC_FREEABLE = "node_zfs_arc_size - node_zfs_arc_c_min"
# Alert thresholds: defaults, overridable per request (?disk=85&temp=85...)
# from the screen's settings, clamped to sane ranges.
LIMITS = {"disk": (85, 50, 99), "temp": (85, 50, 110), "sw_temp": (75, 40, 100), "backup_h": (26, 2, 240)}

DEFAULTS = {
    "title": "Rack Panel",
    "lang": "en",
    "listen": "127.0.0.1:3006",
    "prometheus": "http://127.0.0.1:9090",
    "nodes": [],
    "physical": [],
    "modules": {"hostmetrics": {}},
}
# Module name -> its options with their defaults. A module is on when config.json lists it under "modules".
MODULES = {
    "hostmetrics": {},
    "uptime_kuma": {"public": [], "internal": [".home", ".lan", ".local", ".internal"]},
    "mikrotik": {"wan": ""},
    # Throwaway containers (CI jobs, unnamed `docker run`) are not reported as stopped once gone,
    # nor anything on transient_hosts (a CI runner: jobs and test databases come and go all day).
    "docker": {"ephemeral": "FORGEJO-ACTIONS-TASK-.*|GITEA-ACTIONS-TASK-.*|[a-z]+_[a-z]+|[0-9a-f]+",
               "transient_hosts": []},
    "backups": {"query": "max by (vm_name)(pbs_snapshot_vm_last_timestamp)", "label": "vm_name"},
    "topology": {"url": "", "user": "rack-panel", "groups": "", "data_vlan": "", "admin_vlan": "",
                 "vlans": [], "storage_links": []},
    "restart": {"clients": [], "pve_api": [], "pve_token_id": "", "token_file": "pve-token", "ca_file": "",
                "ssh_key_file": "ssh-key", "ssh_user": "root", "protected_vmids": [], "protected_names": [],
                "docker_hosts": {}},
}


def module_queries(o):
    """PromQL of every module, from its options (o = module -> options)."""
    q = {}
    if "hostmetrics" in o:
        q.update({
            "cpu": 'sum by (host_name)(rate(system_cpu_time_seconds_total{state!="idle"}[2m]))'
                   ' / sum by (host_name)(rate(system_cpu_time_seconds_total[2m]))',
            "mem": 'sum by (host_name, state)(system_memory_usage_bytes{state=~"used|free|cached|buffered"})',
            # ZFS ARC above its floor: counted as "used" by the OS but handed back on demand.
            "arc": "max by (host_name)(clamp_min(" + ARC_FREEABLE + ", 0))",
            "seen": 'count by (host_name)(max_over_time(system_memory_usage_bytes{state="used"}[1d]))',
            "up": 'count by (host_name)(system_memory_usage_bytes{state="used"})',
            "fs": 'sum by (host_name, mountpoint, device, type, state)(system_filesystem_usage_bytes)',
            "temp": "max by (host_name)(" + HOST_TEMP + ")",
            # Host traffic (bytes/s) by direction, without loopback, bridges and container/VM interfaces.
            "net": 'sum by (host_name, direction)(rate(system_network_io_bytes_total'
                   '{device!~"lo|veth.*|docker.*|br-.*|fw.*|tap.*|vmbr.*"}[2m]))',
        })
    if "uptime_kuma" in o:
        q.update({"kuma": "monitor_status", "kuma_ms": "monitor_response_time"})
    if "mikrotik" in o:
        router, _, iface = o["mikrotik"]["wan"].partition(" ")
        if iface:  # Internet = the router's WAN interface, as mktxp sees it
            sel = f"{{routerboard_name={_q(router)}, name={_q(iface)}}}"
            q["wan_rx"] = f"rate(mktxp_interface_rx_byte_total{sel}[2m]) * 8 / 1e6"
            q["wan_tx"] = f"rate(mktxp_interface_tx_byte_total{sel}[2m]) * 8 / 1e6"
        q.update({
            "sw_cpu": "mktxp_system_cpu_load",
            "sw_temp": "max by (routerboard_name)({__name__=~\"mktxp_system_(cpu|switch)_temperature\"})",
            # Every switch port, in + out (bit/s): busiest ports, live links on the topology, port names for /api/detail.
            "port_bps": '(rate(mktxp_interface_rx_byte_total{name!~"bridge.*|lo"}[2m])'
                        ' + on (routerboard_name, name) rate(mktxp_interface_tx_byte_total[2m])) * 8',
            "neighbors": "mktxp_neighbor",
        })
    if "docker" in o:  # OTel docker_stats receiver. Utilization and memory % come in percent.
        d = o["docker"]
        hosts = "|".join(d["transient_hosts"]) or "-"
        q.update({
            "ct_cpu": f"max by ({_CT})(container_cpu_utilization_ratio) / 100",
            "ct_mem": f"max by ({_CT})(container_memory_usage_total_bytes)",
            "ct_memp": f"max by ({_CT})(container_memory_percent_ratio) / 100",
            "ct_restarts": f"max by ({_CT})(round(increase(container_restarts_total[1d])))",
            "ct_seen": f'count by ({_CT})(max_over_time(container_memory_usage_total_bytes'
                       f'{{container_name!~"{d["ephemeral"]}", host_name!~"{hosts}"}}[1d]))',
        })
    if "backups" in o:  # Proxmox Backup Server (pbs-exporter) + the last good backup of each guest
        q.update({"pbs_used": "pbs_used", "pbs_size": "pbs_size", "pbs_last": o["backups"]["query"]})
    if "topology" in o and "hostmetrics" in o:
        # Per-interface traffic (bit/s, in + out) of the hosts with direct storage links.
        links = o["topology"]["storage_links"]
        q["nic_bps"] = ('sum by (host_name, device)(rate(system_network_io_bytes_total{host_name=~"%s"}[2m])) * 8'
                        % "|".join(sorted({h for h, _, _ in links}) or ["-"]))
    return q


QUERY_KEYS = ["cpu", "mem", "arc", "seen", "up", "fs", "temp", "net", "kuma", "kuma_ms", "wan_rx", "wan_tx", "sw_cpu",
              "sw_temp", "port_bps", "neighbors", "nic_bps", "ct_cpu", "ct_mem", "ct_memp", "ct_restarts", "ct_seen",
              "pbs_used", "pbs_size", "pbs_last"]


def _q(s):
    return json.dumps(s, ensure_ascii=False)  # PromQL string literal (Go escapes)


# Detail sheets (/api/detail): fixed PromQL templates, the client only picks a
# kind, a known name and a range. $n/$i/$h/$p = quoted names, $w = rate window,
# $s = step, $r = whole range. Each range is cut in 120 steps.
RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400}
POINTS = 120
_FS_SKIP = 'type!~"tmpfs|devtmpfs|squashfs|overlay|nsfs|proc|sysfs"'
_NET_SKIP = 'device!~"lo|veth.*|docker.*|br-.*|fw.*|tap.*|vmbr.*"'
_RX = 'rate(mktxp_interface_rx_byte_total{routerboard_name=$n, name!~"bridge.*|lo"$f}[$w])'
_TX = _RX.replace("_rx_", "_tx_")
_DUPLEX = ('label_replace(sum(%s) * 8, "dir", "receive", "", "")'
           ' or label_replace(sum(%s) * 8, "dir", "transmit", "", "")') % (_RX, _TX)
_C = "{host_name=$h, container_name=$c}"
_POOL = 'system_filesystem_usage_bytes{host_name=$h, type="zfs", device=~$p, state="%s"}'
DETAIL = {
    "host": {
        "charts": [
            ("cpu", "CPU", "pct", 'sum(rate(system_cpu_time_seconds_total{host_name=$n, state!="idle"}[$w]))'
             ' / sum(rate(system_cpu_time_seconds_total{host_name=$n}[$w]))', None),
            ("mem", "RAM", "pct", '(sum(system_memory_usage_bytes{host_name=$n, state="used"})'
             ' - (sum(clamp_min(node_zfs_arc_size{host_name=$n} - node_zfs_arc_c_min{host_name=$n}, 0)) or vector(0)))'
             ' / sum(system_memory_usage_bytes{host_name=$n, state=~"used|free|cached|buffered"})', None),
            ("load", "Load", "num", '{__name__=~"system_cpu_load_average_(1m|5m|15m)", host_name=$n}', "__name__"),
            ("net", "Network", "bps", 'sum by (direction)(rate(system_network_io_bytes_total{host_name=$n, '
             + _NET_SKIP + '}[$w])) * 8', "direction"),
            ("disk", "Disk I/O", "Bps", 'sum by (direction)(rate(system_disk_io_bytes_total{host_name=$n, '
             'device!~"dm-.*|loop.*|zd.*|nbd.*"}[$w]))', "direction"),
            ("temp", "Temperature", "c", "max(" + HOST_TEMP.replace("celsius <", "celsius{host_name=$n} <") + ")", None),
        ],
        "facts": [
            ("Cores", "count", "sum(system_cpu_logical_count{host_name=$n})"),
            ("Total RAM", "bytes", 'sum(system_memory_usage_bytes{host_name=$n, state=~"used|free|cached|buffered"})'),
            ("Processes", "count", "sum(system_processes_count{host_name=$n})"),
        ],
        "bars": ("Mount points", "pct", "mountpoint",
                 'topk(8, sum by (mountpoint)(system_filesystem_usage_bytes{host_name=$n, state="used", ' + _FS_SKIP + '})'
                 ' / sum by (mountpoint)(system_filesystem_usage_bytes{host_name=$n, ' + _FS_SKIP + '}))'),
    },
    "service": {
        "charts": [
            ("status", "Status", "status", "min(min_over_time(monitor_status{monitor_name=$n}[$s]))", None),
            ("ms", "Response time", "ms", "avg(avg_over_time(monitor_response_time{monitor_name=$n}[$s]))", None),
        ],
        "facts": [
            ("Uptime over the range", "pct", "avg(avg_over_time((monitor_status{monitor_name=$n} == bool 1)[$r:1m]))"),
            ("Certificate valid for", "days", "max(monitor_cert_days_remaining{monitor_name=$n}) > 0"),
        ],
    },
    "switch": {
        "charts": [
            ("cpu", "CPU", "pct", "mktxp_system_cpu_load{routerboard_name=$n} / 100", None),
            ("temp", "Temperatures", "c", '{__name__=~"mktxp_system_(cpu|switch|phy)_temperature", routerboard_name=$n}',
             "__name__"),
            ("mem", "Memory", "pct", "1 - mktxp_system_free_memory{routerboard_name=$n}"
             " / mktxp_system_total_memory{routerboard_name=$n}", None),
            ("net", "Switched traffic", "bps", _DUPLEX.replace("$f", ""), "dir"),
        ],
        "facts": [("Up for", "s", "mktxp_system_uptime{routerboard_name=$n}"),
                  ("Cores", "count", "mktxp_system_cpu_count{routerboard_name=$n}")],
        "bars": ("Ports now (in + out)", "bps", "name",
                 "topk(8, (%s + on (name) %s) * 8)" % (_RX.replace("$f", "").replace("$w", "2m"),
                                                         _TX.replace("$f", "").replace("$w", "2m"))),
    },
    "port": {
        "charts": [("net", "Throughput", "bps", _DUPLEX.replace("$f", ", name=$i"), "dir")],
        "facts": [("Inbound peak over the range", "bps",
                   "max_over_time((%s * 8)[$r:$s])" % _RX.replace("$f", ", name=$i"))],
    },
    "pool": {
        "charts": [
            ("fill", "Fill", "pct", "sum(%s) / (sum(%s) + max(%s))" % (_POOL % "used", _POOL % "used", _POOL % "free"),
             None),
            ("used", "Used", "bytes", "sum(%s)" % (_POOL % "used"), None),
        ],
        "facts": [("Size", "bytes", "sum(%s) + max(%s)" % (_POOL % "used", _POOL % "free")),
                  ("Free", "bytes", "max(%s)" % (_POOL % "free"))],
        "bars": ("Largest datasets", "bytes", "device", "topk(8, sum by (device)(%s))" % (_POOL % "used")),
    },
    "pbs": {
        "charts": [("fill", "Fill", "pct", "pbs_used{datastore=$n} / pbs_size{datastore=$n}", None),
                   ("used", "Used", "bytes", "pbs_used{datastore=$n}", None)],
        "facts": [("Size", "bytes", "pbs_size{datastore=$n}"),
                  ("Snapshots", "count", "sum(pbs_snapshot_count{datastore=$n})"),
                  ("Guests backed up", "count", "count(pbs_snapshot_vm_last_timestamp{datastore=$n})")],
        "bars": ("Oldest backups", "s", "vm_name",
                 "topk(8, time() - pbs_snapshot_vm_last_timestamp{datastore=$n})"),
    },
    "container": {
        "charts": [
            ("cpu", "CPU", "pct", "max(container_cpu_utilization_ratio%s) / 100" % _C, None),
            ("mem", "Memory", "bytes", "max(container_memory_usage_total_bytes%s)" % _C, None),
            ("net", "Network", "bps", 'label_replace(sum(rate(container_network_io_usage_rx_bytes_total%s[$w])) * 8, "dir", "receive", "", "")'
             ' or label_replace(sum(rate(container_network_io_usage_tx_bytes_total%s[$w])) * 8, "dir", "transmit", "", "")'
             % (_C, _C), "dir"),
            ("disk", "Disk I/O", "Bps", "sum by (operation)(rate(container_blockio_io_service_bytes_recursive_total%s[$w]))" % _C,
             "operation"),
        ],
        "facts": [("Restarts (since created)", "count", "max(container_restarts_total%s)" % _C),
                  ("Memory limit", "bytes", "max(container_memory_usage_limit_bytes%s)" % _C)],
    },
    "fleet": {
        "charts": [
            ("hosts", "Hosts online", "count", 'count(system_memory_usage_bytes{state="used"})', None),
            ("svc", "Services", "count", 'label_replace(count(monitor_status == 1), "k", "ok", "", "")'
             ' or label_replace(count(monitor_status), "k", "total", "", "")', "k"),
            ("cpu", "Average CPU", "pct", 'avg(sum by (host_name)(rate(system_cpu_time_seconds_total{state!="idle"}[$w]))'
             ' / sum by (host_name)(rate(system_cpu_time_seconds_total[$w])))', None),
        ],  # + Internet, from the mikrotik module's wan (configure)
        "facts": [],
    },
}
LEGEND = {"receive": "in", "transmit": "out", "read": "read", "write": "write", "ok": "OK",
          "total": "tracked", "system_cpu_load_average_1m": "1 min", "system_cpu_load_average_5m": "5 min",
          "system_cpu_load_average_15m": "15 min", "mktxp_system_cpu_temperature": "CPU",
          "mktxp_system_switch_temperature": "chip", "mktxp_system_phy_temperature": "PHY"}

FLEET = list(DETAIL["fleet"]["charts"])


def configure(cfg):
    """Set the module-level settings from a merged config (load_config); the tests call it too."""
    global CFG, OPT, NODES, PHYSICAL, QUERIES, PROMETHEUS, STORAGE_LINKS, ADMIN_VLAN, DATA_VLAN, BACKUP_LABEL
    global ACTION_CLIENTS, PVE_API, PVE_TOKEN_ID, PROTECTED_VMIDS, PROTECTED, DOCKER_HOSTS
    CFG = cfg
    OPT = {name: {**MODULES[name], **(o or {})} for name, o in cfg["modules"].items() if name in MODULES}
    NODES = list(cfg["nodes"])
    # Bare metal (nodes + physical): shown on the Nodes and Storage screens, not on the VM screen, never restarted.
    PHYSICAL = {x.upper() for x in NODES + list(cfg["physical"])}
    PROMETHEUS = cfg["prometheus"].rstrip("/")
    QUERIES = module_queries(OPT)
    top, rst = OPT.get("topology", MODULES["topology"]), OPT.get("restart", MODULES["restart"])
    STORAGE_LINKS = [list(x) for x in top["storage_links"]]  # direct links nothing on the network sees: [host, iface, peer]
    ADMIN_VLAN, DATA_VLAN = top["admin_vlan"], top["data_vlan"]  # Cairn Net path segments ("vlan99")
    BACKUP_LABEL = OPT.get("backups", MODULES["backups"])["label"]
    # Restart (only the screen's own address may ask; its kiosk agent checks the admin code first).
    ACTION_CLIENTS = set(rst["clients"]) if "restart" in OPT else set()
    PVE_API, PVE_TOKEN_ID = list(rst["pve_api"]), rst["pve_token_id"]
    PROTECTED_VMIDS, PROTECTED = set(rst["protected_vmids"]), {x.upper() for x in rst["protected_names"]}
    DOCKER_HOSTS = {k.upper(): v for k, v in rst["docker_hosts"].items()}  # SSH target of each Docker host
    router, _, iface = OPT.get("mikrotik", {}).get("wan", "").partition(" ")
    DETAIL["fleet"]["charts"] = FLEET + ([("wan", "Internet", "bps", _DUPLEX.replace("$n", _q(router)).replace(
        "$f", ", name=" + _q(iface)), "dir")] if iface else [])


def public_config():
    """What the page needs to know about this install (/api/config): never an address or a secret."""
    top = OPT.get("topology", MODULES["topology"])
    vid = lambda v: v.removeprefix("vlan")  # noqa: E731
    return {"title": CFG["title"], "lang": CFG["lang"], "version": VERSION, "modules": sorted(OPT),
            "wan": OPT.get("mikrotik", {}).get("wan", ""), "data_vlan": vid(top["data_vlan"]),
            "admin_vlan": vid(top["admin_vlan"]), "vlans": [{"id": vid(str(v["id"])), "name": v.get("name", "")}
                                                             for v in top["vlans"]]}


def prom(expr):
    url = PROMETHEUS + "/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    with urllib.request.urlopen(url, timeout=8) as r:
        data = json.load(r)["data"]["result"]
    return [(row["metric"], float(row["value"][1])) for row in data]


def prom_range(expr, start, end, step):
    url = PROMETHEUS + "/api/v1/query_range?" + urllib.parse.urlencode(
        {"query": expr, "start": start, "end": end, "step": step})
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.load(r)["data"]["result"]
    return [(row["metric"], [(int(t), float(v)) for t, v in row["values"]]) for row in data]


def known(raw):
    """Names the client may ask a detail sheet for, per kind, from the cached state queries."""
    pools = {f"{m['host_name']} {m['device'].split('/')[0]}" for m, _ in raw["fs"] if m.get("type") == "zfs"}
    return {
        "host": {m["host_name"] for m, _ in raw["seen"] + raw["up"]},
        "service": {m["monitor_name"] for m, _ in raw["kuma"]},
        "switch": {m["routerboard_name"] for m, _ in raw["sw_cpu"]},
        "port": {f"{m['routerboard_name']} {m['name']}" for m, _ in raw["port_bps"]},
        "pool": pools,
        "pbs": {f"PBS {m['datastore']}" for m, _ in raw["pbs_size"]},
        "container": {f"{m['host_name']} {m['container_name']}" for m, _ in raw["ct_seen"] + raw["ct_mem"]},
        "fleet": {"all"},
    }


def params(kind, name):
    """Template values for a validated name; every name goes in quoted, never raw."""
    if kind == "port":
        sw, _, iface = name.partition(" ")
        return {"n": _q(sw), "i": _q(iface)}
    if kind == "pool":
        host, _, pool = name.partition(" ")
        return {"h": _q(host), "p": _q(re.escape(pool) + "|" + re.escape(pool) + "/.*")}
    if kind == "container":
        host, _, ct = name.partition(" ")
        return {"h": _q(host), "c": _q(ct)}
    if kind == "pbs":
        return {"n": _q(name.partition(" ")[2])}
    return {"n": _q(name)}


def zone(url):
    """Where a web probe points, without the address: a public domain, a LAN one, or anything else (an IP)."""
    host = urllib.parse.urlsplit(url).hostname or ""
    o = OPT.get("uptime_kuma", MODULES["uptime_kuma"])
    return ("Public" if any(host.endswith(s) for s in o["public"]) else
            "Internal" if any(host.endswith(s) for s in o["internal"]) else "Direct")


def _num(v):
    return float(f"{v:.4g}") if math.isfinite(v) else None


def build_detail(kind, name, rng, now, query, query_range):
    spec, secs = DETAIL[kind], RANGES[rng]
    step = secs // POINTS
    sub = dict(params(kind, name), s=f"{step}s", w=f"{max(120, 2 * step)}s", r=f"{secs}s")
    fill = lambda expr: string.Template(expr).substitute(sub)  # noqa: E731
    end = int(now) // step * step  # aligned, so the short cache and Prometheus agree
    charts, order = [], {k: i for i, k in enumerate(LEGEND)}
    for cid, title, unit, expr, legend in spec["charts"]:
        rows = sorted(query_range(fill(expr), end - secs, end, step),
                      key=lambda r: (order.get(r[0].get(legend), 99), str(r[0].get(legend))))
        series = [{"label": LEGEND.get(m.get(legend), m.get(legend)) if legend else title,
                   "points": [[t, _num(v)] for t, v in pts if _num(v) is not None]} for m, pts in rows]
        series = [s for s in series if s["points"]]
        if series:
            charts.append({"id": cid, "title": title, "unit": unit, "series": series})
    facts = []
    for title, unit, expr in spec["facts"]:
        rows = query(fill(expr))
        if rows and _num(rows[0][1]) is not None:
            facts.append({"label": title, "unit": unit, "value": _num(rows[0][1])})
    bars = None
    if spec.get("bars"):
        title, unit, label, expr = spec["bars"]
        rows = sorted(((m.get(label, "?"), _num(v)) for m, v in query(fill(expr))), key=lambda x: -(x[1] or 0))
        bars = {"title": title, "unit": unit, "items": [{"label": k, "value": v} for k, v in rows if v is not None]}
    return {"kind": kind, "name": name, "range": rng, "step": step, "start": end - secs, "end": end,
            "charts": charts, "facts": facts, "bars": bars, "version": VERSION}


def fetch(query):
    """Every query of the switched-on modules; a module off = no rows."""
    return {k: query(QUERIES[k]) if k in QUERIES else [] for k in QUERY_KEYS}


def limits_from(qs):
    out = {}
    for key, (default, lo, hi) in LIMITS.items():
        try:
            out[key] = min(hi, max(lo, int(qs.get(key, [default])[0])))
        except ValueError:
            out[key] = default
    return out


def build_state(r, now, limits=None):
    lim = limits or {k: v[0] for k, v in LIMITS.items()}
    by = lambda key, label="host_name": {m.get(label): v for m, v in r[key]}  # noqa: E731

    mem = {}
    for m, v in r["mem"]:
        mem.setdefault(m["host_name"], {})[m["state"]] = v
    fs = {}
    for m, v in r["fs"]:
        fs.setdefault((m["host_name"], m["mountpoint"], m["device"], m["type"]), {})[m["state"]] = v

    cpu, up, temp, arc = by("cpu"), by("up"), by("temp"), by("arc")
    net = {(m["host_name"], m.get("direction")): v for m, v in r["net"]}
    hosts = []
    for name in sorted(set(by("seen")) | set(up)):
        mm = mem.get(name, {})
        root = fs.get(next((k for k in fs if k[0] == name and k[1] == "/"), None), {})
        hosts.append({
            "name": name,
            "up": name in up,
            "cpu": cpu.get(name),
            "mem": max(0, mm.get("used", 0) - arc.get(name, 0)) / (sum(mm.values()) or 1) if mm else None,
            "disk": root.get("used", 0) / (sum(root.values()) or 1) if root else None,
            "temp": temp.get(name),
            "vm": name.upper() not in PHYSICAL,
            "rx": _num(net[(name, "receive")]) if (name, "receive") in net else None,
            "tx": _num(net[(name, "transmit")]) if (name, "transmit") in net else None,
        })

    # ZFS datasets share their pool's free space: pool = sum(used) + free.
    pools = {}
    for (host, mount, device, typ), st in fs.items():
        if typ == "zfs":
            p = pools.setdefault((host, device.split("/")[0]), {"used": 0, "free": st.get("free", 0)})
            p["used"] += st.get("used", 0)
    storage = [{"name": f"{h} {pool}", "used": p["used"], "size": p["used"] + p["free"]}
               for (h, pool), p in sorted(pools.items())]
    for m, v in r["pbs_size"]:
        used = by("pbs_used", "datastore").get(m["datastore"], 0)
        storage.append({"name": f"PBS {m['datastore']}", "used": used, "size": v})

    ms = {m["monitor_name"]: v for m, v in r["kuma_ms"]}
    probes = sorted(({"name": m["monitor_name"], "status": int(v), "ms": ms.get(m["monitor_name"]),
                      "web": m.get("monitor_type") in WEB_PROBES, "zone": zone(m.get("monitor_url", ""))}
                     for m, v in r["kuma"]), key=lambda s: (s["status"] == 1, s["name"].lower()))
    services = [{k: v for k, v in p.items() if k != "web"} for p in probes if p["web"]]

    ct = {}
    for key in ("ct_cpu", "ct_mem", "ct_memp", "ct_restarts"):
        for m, v in r[key]:
            ct.setdefault((m["host_name"], m["container_name"]), {})[key[3:]] = _num(v)
    # Running = reports memory now; restarts over 1 day also cover gone CI containers, so they don't count.
    running = {(m["host_name"], m["container_name"]) for m, _ in r["ct_mem"]}
    gone = {(m["host_name"], m["container_name"]) for m, _ in r["ct_seen"]} - running
    containers = [{"host": h, "name": c, "up": (h, c) in running, **ct.get((h, c), {})} for h, c in sorted(running | gone)]

    stale = sorted(m.get(BACKUP_LABEL, "?") for m, v in r["pbs_last"] if now - v > lim["backup_h"] * 3600)
    sw_temp = by("sw_temp", "routerboard_name")
    switches = [{"name": m["routerboard_name"], "model": m.get("board_name"), "cpu": v,
                 "temp": sw_temp.get(m["routerboard_name"])}
                for m, v in sorted(r["sw_cpu"], key=lambda x: x[0]["routerboard_name"])]
    wan = {d: next((v for _, v in r["wan_" + d]), None) for d in ("rx", "tx")}
    links = [{"name": f"{m['routerboard_name']} {m['name']}", "bps": v}
             for m, v in sorted(r["port_bps"], key=lambda x: -x[1])[:6]]

    alerts = [f"{h['name']} is not answering" for h in hosts if not h["up"]]
    alerts += [f"{s['name']}: {'down' if s['status'] == 0 else 'pending'}"
               for s in probes if s["status"] in (0, 2)]
    disk = lim["disk"] / 100
    alerts += [f"{h['name']}: disk at {h['disk']:.0%}" for h in hosts if (h["disk"] or 0) >= disk]
    alerts += [f"{s['name']} {s['used'] / s['size']:.0%} full" for s in storage
               if s["size"] and s["used"] / s["size"] >= disk]
    alerts += [f"{h['name']} at {h['temp']:.0f} °C" for h in hosts if (h["temp"] or 0) >= lim["temp"]]
    alerts += [f"Switch {s['name']} at {s['temp']:.0f} °C" for s in switches if (s["temp"] or 0) >= lim["sw_temp"]]
    if stale:
        alerts.append(f"Backup older than {lim['backup_h']} h: " + ", ".join(stale))

    return {"time": now, "version": VERSION, "limits": lim, "nodes": NODES, "hosts": hosts, "services": services, "containers": containers, "storage": storage,
            "backups": {"count": len(r["pbs_last"]), "stale": stale}, "wan": wan,
            "switches": switches, "links": links, "alerts": alerts}


def build_topology(cmap, raw):
    """Cairn Map /api/map + live port rates -> a small tree for the rack screen.

    Router (root) -> its MikroTik neighbours (switches, access point) -> physical machines on the
    port where Cairn Net sees them -> Proxmox guests. A machine Cairn Net does not place gets the
    port where most of its guests are seen; one behind the access point's port hangs off the AP.
    """
    nodes = cmap.get("nodes", {})
    phys = [x for x in nodes.values() if x.get("family") == "physical"]
    router = next((x["name"] for x in phys if x.get("kind") == "router"), None)
    gear = {x["name"]: x["kind"] for x in phys if x.get("kind") in ("switch", "ap")}
    up = {}  # device -> (parent, port on the parent)
    for m, _ in raw["neighbors"]:
        dev, parent = m.get("identity"), m.get("routerboard_name")
        ifs = m.get("interface", "").split(",")
        # mgmt99 is the switches' VLAN 99 interface: every switch sees every other through it, it is no cable.
        if parent == router and dev in gear and not ifs[0].startswith("mgmt"):
            port = next((i for i in ifs if i.startswith("bond-")), ifs[0])
            if dev not in up or port.startswith("bond-"):  # an aggregate wins over a stray single port
                up[dev] = (router, port)
    seen = {}  # network device name -> (switch, port), from Cairn Net's tree: vlan/switch/port/device
    for x in nodes.values():
        p = x.get("path", [])
        if x.get("family") == "network" and len(p) == 4:
            seen.setdefault(x["name"].upper(), (p[1].upper(), p[2]))
    guests = {}
    for x in nodes.values():
        if x.get("family") == "hypervisor" and x.get("kind") in ("lxc", "vm", "guest") and x.get("path"):
            guests.setdefault(x["path"][0].upper(), []).append(x["name"])
    ap_port = {v: k for k, v in up.items() if gear.get(k) == "ap"}
    # The ISP box (another router Cairn Net sees on a switch port): Internet enters through that switch.
    wan = next(({"name": x["name"], "via": x["path"][1].upper(), "port": x["path"][2]} for x in nodes.values()
                 if x.get("family") == "network" and x.get("kind") == "router" and len(x.get("path", [])) == 4
                 and x["path"][1].upper() in gear), None)
    # Copper links between switches outside the aggregates: the switches' management links (VLAN 99,
    # red cables), not data paths. Drawn apart, and as the backbone of the VLAN 99 card.
    spare = {}
    sw_all = {n for n, k in gear.items() if k == "switch"} | {router}
    for m, _ in raw["neighbors"]:
        a, b, ifs = m.get("routerboard_name"), m.get("identity"), m.get("interface", "")
        if a in sw_all and b in sw_all and "bond-" not in ifs and not ifs.startswith("mgmt"):
            spare.setdefault(tuple(sorted((a, b))), (a, ifs.split(",")[0]))
    machines, taken = [], set()
    # Most guests first: a node with a clear majority claims its port, so a tie (a stale guest
    # placement in the repo, e.g. a migrated VM) does not land on another node's port.
    for x in sorted(phys, key=lambda x: (-len(guests.get(x["name"].upper(), [])), x["name"])):
        vm = any(x["name"].upper() in (n.upper() for n in gs) for gs in guests.values())
        if x.get("kind") not in ("node", "nas", "server", "pc") or vm:  # a VM listed as hardware stays a guest
            continue
        g = sorted(guests.get(x["name"].upper(), []))
        net = [a.split(":") for a in x.get("also", []) if a.startswith("net:") and a.count(":") > 4]
        # A node's 1G port in the admin VLAN is not its data path (that is its 10G link to the router,
        # where its guests are seen): place it by its data port, show the admin port apart.
        adm = next(((n[3].upper(), n[4]) for n in net if n[2] == ADMIN_VLAN), None)
        data = [n for n in net if n[2] != ADMIN_VLAN]
        at = (data[0][3].upper(), data[0][4]) if data else None
        if not at and g:
            spots = [seen[n.upper()] for n in g if n.upper() in seen]
            at = max(sorted(set(spots)), key=lambda p: (spots.count(p), p not in taken)) if spots else None
            taken.add(at)
        at = at or adm
        parent, port = at if at else (router, None)
        if at in ap_port:  # Wi-Fi client: behind the access point, not its switch port
            parent, port = ap_port[at], None
        machines.append({"name": x["name"], "kind": x["kind"], "parent": parent, "port": port, "guests": g,
                         "admin": list(adm) if adm and adm != at else None})
    machines.sort(key=lambda m: m["name"])
    bps = {f"{m['routerboard_name']} {m['name']}": _num(v) for m, v in raw["port_bps"]}
    # Every other VLAN as Cairn Net sees it (switch, port, device); MACs named after the machine that owns them.
    owner = {a.split(":", 5)[5].replace(":", "-").lower(): x["name"] for x in phys
             for a in x.get("also", []) if a.startswith("net:") and a.count(":") > 5}
    known = {x["name"].upper() for x in phys} | {g.upper() for gs in guests.values() for g in gs}
    named = lambda n: n.upper() if n.upper() in known else n  # noqa: E731
    vlans = {}
    for x in nodes.values():
        p = x.get("path", [])
        if x.get("family") == "network" and len(p) == 4 and p[0] != DATA_VLAN:
            vlans.setdefault(p[0].replace("vlan", ""), []).append(
                {"sw": p[1].upper(), "port": p[2], "name": named(owner.get(p[3].lower(), p[3])), "kind": x.get("kind")})
    # Wi-Fi clients are seen on the access point's switch port: hang them behind the AP itself.
    for v in vlans.values():
        for d in v:
            if (d["sw"], d["port"]) in ap_port:
                d["sw"], d["port"] = ap_port[(d["sw"], d["port"])], "Wi-Fi"
        v[:] = [d for d in v if d["name"].upper() not in gear]
    for v in vlans.values():
        v.sort(key=lambda d: (d["sw"], d["port"], d["name"]))
    nic = {(m["host_name"], m["device"]): _num(v) for m, v in raw["nic_bps"]}
    storage = [{"a": a, "iface": i, "b": b, "bps": nic.get((a, i))} for a, i, b in STORAGE_LINKS]
    return {"router": router, "gear": [{"name": n, "kind": k, "parent": up.get(n, (router, None))[0],
                                        "port": up.get(n, (None, None))[1]} for n, k in sorted(gear.items())],
            "machines": machines, "bps": bps, "wan": wan, "vlans": vlans, "storage": storage,
            "spare": [{"a": a, "b": b, "port": f"{sw} {port}"} for (a, b), (sw, port) in sorted(spare.items())],
            "version": VERSION}


_topo = {"at": 0.0, "map": None}


def cairn_map():
    """Cairn Map's tree, re-read every 5 min; this server must be one of its trusted proxies."""
    if "topology" not in OPT or not OPT["topology"]["url"]:
        raise OSError("topology module off")
    if time.time() - _topo["at"] > TOPOLOGY_SECONDS:
        o = OPT["topology"]
        req = urllib.request.Request(o["url"].rstrip("/") + "/api/map", headers={
            "Remote-User": o["user"], **({"Remote-Groups": o["groups"]} if o["groups"] else {})})
        with urllib.request.urlopen(req, timeout=15) as r:
            _topo["map"] = json.load(r)
        _topo["at"] = time.time()
    return _topo["map"]


_lock = threading.Lock()
_cache = {"at": 0.0, "raw": None}
_detail = {}  # (kind, name, range) -> (at, body)


def raw_state():
    with _lock:
        if time.time() - _cache["at"] > CACHE_SECONDS:
            _cache["raw"] = fetch(prom)
            _cache["at"] = time.time()
        return _cache["raw"]


def description(cmap, name):
    """What a machine is for (Cairn IaC description, carried by Cairn Map), "" when unknown."""
    for x in (cmap or {}).get("nodes", {}).values():
        if x.get("name", "").upper() == name.upper() and (x.get("attrs") or {}).get("description"):
            return x["attrs"]["description"]
    return ""


def detail(qs):
    """(code, body) for /api/detail?kind=&name=&range=; names checked against the live state."""
    kind, name, rng = (qs.get(k, [""])[0] for k in ("kind", "name", "range"))
    rng = rng or "1h"
    if kind not in DETAIL or rng not in RANGES:
        return 400, {"error": "unknown kind or range"}
    if name not in known(raw_state())[kind]:
        return 404, {"error": "unknown name"}
    key, now = (kind, name, rng), time.time()
    hit = _detail.get(key)
    if hit and now - hit[0] < max(CACHE_SECONDS, RANGES[rng] // POINTS // 2):
        return 200, hit[1]
    body = build_detail(kind, name, rng, now, prom, prom_range)
    if kind == "host":
        try:
            body["description"] = description(cairn_map(), name)
        except (OSError, ValueError):  # Cairn Map down: the sheet still opens, without it
            body["description"] = ""
    if len(_detail) > 200:  # ponytail: drop-all eviction, names are a bounded set anyway
        _detail.clear()
    _detail[key] = (now, body)
    return 200, body


def secret(key):
    """A restart secret named by the restart module (token_file, ssh_key_file), read from the config folder."""
    name = OPT.get("restart", MODULES["restart"])[key]
    try:
        with open(os.path.join(SECRETS, name), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def pve(method, path):
    """Proxmox API call with the restart token, each node tried in turn (a node down is not an error)."""
    ca = OPT["restart"]["ca_file"]
    ctx = ssl.create_default_context(cafile=os.path.join(SECRETS, ca) if ca else None)
    auth = {"Authorization": f"PVEAPIToken={PVE_TOKEN_ID}={secret('token_file')}"}
    err = None
    for base in PVE_API:
        req = urllib.request.Request(base + "/api2/json" + path, method=method, headers=auth)
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
                return json.load(r)["data"]
        except urllib.error.HTTPError:
            raise
        except OSError as exc:
            err = exc
    raise err


def ssh_restart(addr, ct):
    """docker restart through the forced command on the host (rack-panel-restart, see README)."""
    # ponytail: host keys not pinned; the key can only restart a container, a MITM learns a container name.
    key = os.path.join(SECRETS, OPT["restart"]["ssh_key_file"])
    p = subprocess.run(["ssh", "-i", key, "-o", "BatchMode=yes",
                        "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
                        "-o", "ConnectTimeout=5", f"{OPT['restart']['ssh_user']}@{addr}", ct], capture_output=True, text=True, timeout=60)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout).strip()[-200:] or f"code {p.returncode}")


def restart(body, client, raw, call_pve=pve, call_ssh=ssh_restart):
    """(code, body) for POST /api/restart {kind: host|container, name}; the name must be in the live state."""
    if client not in ACTION_CLIENTS:
        return 403, {"error": "actions are for the rack screen only"}
    kind, name = body.get("kind"), body.get("name")
    if kind not in ("host", "container") or not isinstance(name, str):
        return 400, {"error": "bad kind or name"}
    if name not in known(raw)[kind]:
        return 404, {"error": "unknown name"}
    if kind == "host":
        if name.upper() in PHYSICAL:
            return 403, {"error": "a node is restarted from a desk, not from the screen"}
        if name.upper() in PROTECTED:
            return 403, {"error": f"{name} is protected"}
        if not secret("token_file"):
            return 503, {"error": "no Proxmox token"}
        guest = next((g for g in call_pve("GET", "/cluster/resources?type=vm")
                      if str(g.get("name", "")).upper() == name.upper()), None)
        if not guest:
            return 404, {"error": f"{name} not found in Proxmox"}
        if guest["vmid"] in PROTECTED_VMIDS:
            return 403, {"error": f"{name} is protected"}
        call_pve("POST", f"/nodes/{guest['node']}/{guest['type']}/{guest['vmid']}/status/reboot")
        msg = f"{name} is restarting ({'VM' if guest['type'] == 'qemu' else 'LXC'} {guest['vmid']} on {guest['node']})"
    else:
        host, _, ct = name.partition(" ")
        if host not in DOCKER_HOSTS or not CT_NAME.fullmatch(ct):
            return 403, {"error": f"{host} takes no restarts"}
        if not secret("ssh_key_file"):
            return 503, {"error": "no SSH key"}
        call_ssh(DOCKER_HOSTS[host], ct)
        msg = f"{ct} restarted on {host}"
    print(f"restart {kind} {name!r} asked by {client}: {msg}", file=sys.stderr, flush=True)
    return 200, {"ok": True, "message": msg}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path != "/api/restart":
            return self.reply(404, b"not found\n", "text/plain")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 4096)) or b"{}")
            code, out = restart(body if isinstance(body, dict) else {}, self.client_address[0], raw_state())
        except ValueError:
            code, out = 400, {"error": "bad json"}
        except urllib.error.HTTPError as exc:
            code, out = 502, {"error": f"Proxmox refused ({exc.code} {exc.reason})"}
        except Exception as exc:  # node, Prometheus or host unreachable
            code, out = 502, {"error": str(exc)[:200]}
        self.reply(code, json.dumps(out).encode(), "application/json")

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        qs = urllib.parse.parse_qs(url.query)
        if url.path in ("/api/state", "/api/detail", "/api/topology", "/api/config"):
            try:
                if url.path == "/api/config":
                    code, body = 200, public_config()
                elif url.path == "/api/detail":
                    code, body = detail(qs)
                elif url.path == "/api/topology":
                    code, body = 200, build_topology(cairn_map(), raw_state())
                else:
                    code, body = 200, build_state(raw_state(), time.time(), limits_from(qs))
            except Exception as exc:  # Prometheus down: tell the page, keep serving
                code, body = 502, {"error": str(exc)}
            return self.reply(code, json.dumps(body).encode(), "application/json")
        if url.path in ("/", "/index.html"):
            with open(PAGE, "rb") as f:
                return self.reply(200, f.read(), "text/html; charset=utf-8")
        if url.path == "/healthz":
            return self.reply(200, b"ok\n", "text/plain")
        self.reply(404, b"not found\n", "text/plain")

    def reply(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # only errors reach the journal
        if not (len(args) > 1 and str(args[1]).startswith(("2", "3"))):
            super().log_message(fmt, *args)


def unknown_keys(cfg):
    """Config keys nothing reads (a typo would else go unnoticed), with the closest known key."""
    def check(d, known, where):
        for k in d:
            if k not in known:
                close = difflib.get_close_matches(k, known, 1)
                yield f"unknown key {where}{k!r}" + (f", did you mean {close[0]!r}?" if close else "")
    out = list(check(cfg, DEFAULTS, ""))
    mods = cfg.get("modules") or {}
    out += list(check(mods, MODULES, "in modules: "))
    for name, o in mods.items():
        if name in MODULES and isinstance(o, dict):
            out += check(o, MODULES[name], f"in module {name!r}: ")
    return out


def load_config(path):
    """config.json over DEFAULTS; restart secrets are read from the config's folder."""
    global SECRETS
    raw = {}
    if path:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    for w in unknown_keys(raw):
        print(f"rack-panel: {w}", file=sys.stderr)
    SECRETS = os.path.dirname(os.path.abspath(path)) if path else os.getcwd()
    return {**DEFAULTS, **raw}


SECRETS = os.getcwd()
configure(DEFAULTS)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-c", "--config", help="JSON config (see docs/configuration.md)")
    ap.add_argument("--json", action="store_true", help="print /api/state once and exit")
    a = ap.parse_args()
    configure(load_config(a.config))
    if a.json:
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(build_state(fetch(prom), time.time()), ensure_ascii=False, indent=2))
        sys.exit()
    host, port = CFG["listen"].rsplit(":", 1)
    ThreadingHTTPServer((host, int(port)), Handler).serve_forever()
