"""python test_rack_panel.py — checks build_state against canned Prometheus rows."""
import importlib.util
import json
import os
import re

spec = importlib.util.spec_from_file_location("rp", os.path.join(os.path.dirname(__file__), "rack-panel.py"))
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)
rp.configure({**rp.DEFAULTS, "nodes": ["NODE1", "NODE2", "NODE3", "NAS", "VAULT", "DESK"],
              "physical": ["KIOSK"], "modules": {
                  "hostmetrics": {}, "uptime_kuma": {"public": [".example.org"], "internal": [".home"]},
                  "mikrotik": {"wan": "ROUTER wan2"}, "docker": {"transient_hosts": ["CI"]},
                  "backups": {"query": "last_backup_timestamp", "label": "guest"},
                  "topology": {"url": "http://map", "data_vlan": "vlan10", "admin_vlan": "vlan99"},
                  "restart": {"clients": ["192.0.2.89"], "protected_vmids": [100], "protected_names": ["HAOS"]}}})

NOW = 1_000_000.0
fs = lambda h, mnt, dev, typ, used, free: [  # noqa: E731
    ({"host_name": h, "mountpoint": mnt, "device": dev, "type": typ, "state": "used"}, used),
    ({"host_name": h, "mountpoint": mnt, "device": dev, "type": typ, "state": "free"}, free)]
ROWS = {
    "cpu": [({"host_name": "NODE1"}, 0.25)],
    "net": [({"host_name": "NODE1", "direction": "receive"}, 1000.0), ({"host_name": "NODE1", "direction": "transmit"}, 869.0)],
    "mem": [({"host_name": "NODE1", "state": "used"}, 3), ({"host_name": "NODE1", "state": "free"}, 1)],
    "arc": [({"host_name": "NODE1"}, 1)],  # freeable ZFS ARC is not "used"
    "seen": [({"host_name": "NODE1"}, 1), ({"host_name": "VAULT"}, 1)],
    "up": [({"host_name": "NODE1"}, 1)],
    "fs": fs("NODE1", "/", "/dev/sda1", "ext4", 90, 10)
          + fs("NAS", "/tank/a", "tank/a", "zfs", 30, 60) + fs("NAS", "/tank/b", "tank/b", "zfs", 10, 60),
    "temp": [({"host_name": "NODE1"}, 90)],
    "fans": [({"host_name": "NODE1", "sensor": "fan2"}, 900.4), ({"host_name": "NODE1", "sensor": "fan1"}, 1200)],
    "kuma": [({"monitor_name": "Grafana", "monitor_type": "http", "monitor_url": "https://grafana.lab.home"}, 0),
             ({"monitor_name": "Dockge", "monitor_type": "keyword", "monitor_url": "https://x.example.org/a"}, 1),
             ({"monitor_name": "Ping MONITOR", "monitor_type": "ping"}, 0)],
    "ct_cpu": [({"host_name": "ADMIN", "container_name": "ntfy"}, 0.02)],
    "ct_mem": [({"host_name": "ADMIN", "container_name": "ntfy"}, 5e7)],
    "ct_memp": [({"host_name": "ADMIN", "container_name": "ntfy"}, 0.01)],
    "ct_restarts": [({"host_name": "ADMIN", "container_name": "ntfy"}, 2)],
    "ct_seen": [({"host_name": "ADMIN", "container_name": "ntfy"}, 1), ({"host_name": "APP", "container_name": "app-db-1"}, 1)],
    "kuma_ms": [({"monitor_name": "Dockge"}, 42)],
    "wan_rx": [({}, 100.0)], "wan_tx": [],
    "sw_cpu": [({"routerboard_name": "SW1", "board_name": "CRS326"}, 3)],
    "sw_temp": [({"routerboard_name": "SW1"}, 80)],
    "port_bps": [({"routerboard_name": "ROUTER", "name": "wan2"}, 5e6), ({"routerboard_name": "SW1", "name": "ether21"}, 2e6)],
    "neighbors": [({"routerboard_name": "ROUTER", "identity": "SW1", "interface": "mgmt99"}, 1),  # VLAN interface, no cable
                  ({"routerboard_name": "ROUTER", "identity": "SW1", "interface": "sfp-sfpplus3,bond-sw1,bridge"}, 1),
                  ({"routerboard_name": "ROUTER", "identity": "WAP", "interface": "sfp-sfpplus5,bridge"}, 1),
                  ({"routerboard_name": "SW1", "identity": "ROUTER", "interface": "ether18,bridge"}, 1),
                  ({"routerboard_name": "ROUTER", "identity": "SW2", "interface": "sfp-sfpplus1,bond-sw2,bridge"}, 1),
                  ({"routerboard_name": "SW1", "identity": "SW2", "interface": "sfp-sfpplus2,bond-router,bridge"}, 1),
                  ({"routerboard_name": "SW1", "identity": "SW2", "interface": "ether17,bridge"}, 1),
                  ({"routerboard_name": "SW1", "identity": "ROUTER", "interface": "ether18,bridge"}, 1),
                  ({"routerboard_name": "SW2", "identity": "SW1", "interface": "mgmt99"}, 1)],
    "nic_bps": [({"host_name": "NAS", "device": "enp7s0f3np3"}, 8e8)],
    "pbs_used": [({"datastore": "main"}, 5)], "pbs_size": [({"datastore": "main"}, 10)],
    "pbs_last": [({"guest": "LOGGER"}, NOW - 30 * 3600), ({"guest": "MONITOR"}, NOW - 3600)],
}
by_expr = {expr: ROWS[key] for key, expr in rp.QUERIES.items()}
raw = rp.fetch(by_expr.__getitem__)
s = rp.build_state(raw, NOW)

node1, vault = s["hosts"]
assert (node1["name"], node1["up"], node1["mem"], node1["disk"]) == ("NODE1", True, 0.5, 0.9)
assert vault == {"name": "VAULT", "up": False, "cpu": None, "mem": None, "disk": None, "temp": None, "fans": None, "vm": False, "rx": None, "tx": None}
assert node1["fans"] == [1200, 900]                                      # by sensor name, empty headers left out
assert (node1["rx"], node1["tx"]) == (1000.0, 869.0)
assert node1["vm"] is False and "ADMIN" not in rp.PHYSICAL  # bare metal stays off the VM screen
# Names are compared without case: a lowercase host_name of a configured node is still bare metal.
low = rp.fetch({e: [({**m, "host_name": m["host_name"].lower()} if "host_name" in m else m, v) for m, v in ROWS[k]]
                for k, e in rp.QUERIES.items()}.__getitem__)
assert [(h["name"], h["vm"]) for h in rp.build_state(low, NOW)["hosts"]] == [("node1", False), ("vault", False)]
# Services = web probes only, with their zone (never the URL); a down ping probe still alerts.
assert [(x["name"], x["zone"]) for x in s["services"]] == [("Grafana", "Internal"), ("Dockge", "Public")], s["services"]
assert "Ping MONITOR: down" in s["alerts"] and "monitor_url" not in str(s)
assert rp.zone("http://192.0.2.69:3003/x") == "Direct"
# Containers: running ones with their numbers, one seen today but gone = stopped.
assert s["containers"] == [{"host": "ADMIN", "name": "ntfy", "up": True, "cpu": 0.02, "mem": 5e7, "memp": 0.01, "restarts": 2},
                           {"host": "APP", "name": "app-db-1", "up": False}], s["containers"]
assert {"name": "NAS tank", "used": 40, "size": 100} in s["storage"]  # pool = sum(used) + shared free
assert s["services"][0]["name"] == "Grafana"  # failures first
assert s["wan"] == {"rx": 100.0, "tx": None}
assert s["backups"]["stale"] == ["LOGGER"]
for needle in ("VAULT is not answering", "Grafana: down", "NODE1: disk at 90%", "NODE1 at 90 °C",
               "Switch SW1 at 80 °C", "LOGGER"):
    assert any(needle in a for a in s["alerts"]), needle
# Thresholds from the screen's settings, clamped.
lim = rp.limits_from({"disk": ["95"], "temp": ["999"], "backup_h": ["abc"]})
assert lim == {"disk": 95, "temp": 110, "sw_temp": 75, "backup_h": 26}
s2 = rp.build_state(raw, NOW, lim)
assert not any("disk" in a or "NODE1 at" in a for a in s2["alerts"]), s2["alerts"]
assert rp.build_state(raw, NOW, dict(lim, backup_h=48))["backups"]["stale"] == []

# Detail sheets: names come from the live state, PromQL only from the fixed templates.
k = rp.known(raw)
assert k["host"] == {"NODE1", "VAULT"} and k["pool"] == {"NAS tank"}
assert k["container"] == {"ADMIN ntfy", "APP app-db-1"}
assert "ROUTER wan2" in k["port"] and k["pbs"] == {"PBS main"} and "Grafana" in k["service"]
rp.raw_state = lambda: raw
assert rp.detail({"kind": ["host"], "name": ['NODE1"} or vector(1) #'], "range": ["1h"]})[0] == 404
assert rp.detail({"kind": ["evil"], "name": ["NODE1"]})[0] == 400
assert rp.detail({"kind": ["host"], "name": ["NODE1"], "range": ["5y"]})[0] == 400
seen = []
def q_range(expr, start, end, step):
    seen.append((expr, end - start, step))
    return [({"direction": "receive"}, [(start, 1.0), (end, float("nan"))])]
d = rp.build_detail("pool", 'NAS ta"nk', "24h", NOW, lambda e: seen.append((e,)) or [({}, 5.0)], q_range)
assert all(e[1:] in ((), (86400, 720)) for e in seen)                   # 120 points over the range
assert all('device=~"ta\\"nk|ta\\"nk/.*"' in e[0] for e in seen), seen[0]  # quoted, escaped name
assert "$" not in "".join(e[0] for e in seen)                            # every placeholder filled
assert d["charts"][0]["series"][0]["points"] == [[d["start"], 1.0]]      # NaN dropped (JSON-safe)
seen.clear()
rp.build_detail("container", 'ADMIN n"t', "1h", NOW, lambda e: seen.append((e,)) or [], q_range)
assert all('host_name="ADMIN", container_name="n\\"t"' in e[0] for e in seen), seen[0]
d = rp.build_detail("host", "NODE1", "1h", NOW, lambda e: [], lambda e, *a: [
    ({"sensor": s}, [(NOW, 900.0)]) for s in ("fan2", "fan1")] if "fan_rpm" in e else [])
assert [(c["id"], c["unit"], [s["label"] for s in c["series"]]) for c in d["charts"]] == [
    ("fans", "rpm", ["fan1", "fan2"])]                                   # one line per fan, by sensor
for kind in rp.DETAIL:                                                   # every template substitutes
    rp.build_detail(kind, "A b", "1h", NOW, lambda e: [], lambda *a: [])

# Topology from Cairn Map: placed by Cairn Net, else by its guests' port, Wi-Fi behind the AP.
assert s["links"][0] == {"name": "ROUTER wan2", "bps": 5e6}  # busiest port first
P = lambda name, kind, also=(): {"family": "physical", "name": name, "kind": kind, "also": list(also)}  # noqa: E731
cmap = {"nodes": {str(i): x for i, x in enumerate([
    P("ROUTER", "router"), P("SW1", "switch"), P("WAP", "ap"), P("SW2", "switch"),
    {"family": "network", "kind": "router", "name": "isp-box", "path": ["vlan2", "sw2", "ether1", "isp-box"]},
    {"family": "network", "kind": "server", "name": "58:47", "path": ["vlan99", "sw1", "ether21", "58-47"]},
    {"family": "network", "kind": "iot", "name": "dns", "path": ["vlan20", "sw1", "ether1", "dns"]},
    {"family": "network", "kind": "iot", "name": "esp-1", "path": ["vlan20", "sw1", "ether2", "esp-1"]},
    {"family": "network", "kind": "iot", "name": "esp-2", "path": ["vlan20", "router", "sfp-sfpplus5", "esp-2"]},
    {"family": "network", "kind": "ap", "name": "wap", "path": ["vlan20", "router", "sfp-sfpplus5", "wap"]},
    P("NODE1", "node", ["host:node1", "net:router:vlan99:sw1:ether21:58:47"]),
    P("NODE2", "node", ["net:router:vlan99:sw1:ether24:A4:AE"]), P("dns", "server"), P("KIOSK", "server", ["net:router:vlan10:router:sfp-sfpplus5:50:1A"]), P("VAULT", "nas"),
    {"family": "hypervisor", "kind": "lxc", "name": "ADMIN", "path": ["node2", "admin"]},
    {"family": "hypervisor", "kind": "vm", "name": "DNS", "path": ["node2", "dns"]},
    {"family": "network", "kind": "server", "name": "admin", "path": ["vlan10", "router", "sfp-sfpplus11", "admin"]},
    # NODE3: one guest really on its port, one migrated away but still listed under it (tie).
    P("NODE3", "node"),
    {"family": "hypervisor", "kind": "vm", "name": "APP", "path": ["node3", "app"]},
    {"family": "hypervisor", "kind": "vm", "name": "DESKVM", "path": ["node3", "deskvm"]},
    {"family": "network", "kind": "server", "name": "app", "path": ["vlan10", "router", "sfp-sfpplus12", "app"]},
    {"family": "network", "kind": "server", "name": "dns", "path": ["vlan10", "router", "sfp-sfpplus11", "dns"]},
    {"family": "network", "kind": "server", "name": "deskvm", "path": ["vlan10", "router", "sfp-sfpplus11", "deskvm"]},
])}}
t = rp.build_topology(cmap, raw)
assert t["router"] == "ROUTER"
assert {g["name"]: (g["parent"], g["port"]) for g in t["gear"]} == {
    "SW1": ("ROUTER", "bond-sw1"), "WAP": ("ROUTER", "sfp-sfpplus5"),
    "SW2": ("ROUTER", "bond-sw2")}
# Internet enters through the switch where the ISP box is seen; the spare SW1-SW2 link stands apart.
assert t["wan"] == {"name": "isp-box", "via": "SW2", "port": "ether1"}, t["wan"]
assert t["spare"] == [{"a": "ROUTER", "b": "SW1", "port": "SW1 ether18"},
                      {"a": "SW1", "b": "SW2", "port": "SW1 ether17"}], t["spare"]
m = {x["name"]: (x["parent"], x["port"], x["guests"]) for x in t["machines"]}
assert m == {"NODE1": ("SW1", "ether21", []), "NODE2": ("ROUTER", "sfp-sfpplus11", ["ADMIN", "DNS"]),
             "KIOSK": ("WAP", None, []), "VAULT": ("ROUTER", None, []),
             "NODE3": ("ROUTER", "sfp-sfpplus12", ["APP", "DESKVM"])}, m
# A node is drawn on its data path (guests' port), its admin VLAN port apart; admin only = fallback.
assert {x["name"]: x["admin"] for x in t["machines"]} == {
    "NODE1": None, "NODE2": ["SW1", "ether24"], "KIOSK": None, "VAULT": None, "NODE3": None}
assert t["bps"]["SW1 ether21"] == 2e6
# Other VLANs from Cairn Net, MAC named after its machine; direct storage links with their NIC rate.
assert [(d["sw"], d["port"], d["name"]) for d in t["vlans"]["20"]] == [
    ("SW1", "ether1", "DNS"), ("SW1", "ether2", "esp-1"), ("WAP", "Wi-Fi", "esp-2")], t["vlans"]["20"]  # known machines in capitals
assert t["vlans"]["99"] == [{"sw": "SW1", "port": "ether21", "name": "NODE1", "kind": "server"}], t["vlans"]
rp.STORAGE_LINKS = [["NAS", "enp7s0f3np3", "NODE1"], ["NAS", "enp94s0", "VAULT"]]
assert rp.build_topology(cmap, raw)["storage"] == [{"a": "NAS", "iface": "enp7s0f3np3", "b": "NODE1", "bps": 8e8},
                                                    {"a": "NAS", "iface": "enp94s0", "b": "VAULT", "bps": None}]
# what a machine is for, from Cairn Map's nodes (Cairn IaC description)
dm = {"nodes": {"a": {"name": "NAS", "attrs": {"description": "ZFS storage"}}, "b": {"name": "web", "attrs": {}}}}
assert rp.description(dm, "nas") == "ZFS storage" and rp.description(dm, "WEB") == "" and rp.description(None, "x") == ""
# Lot 6: restart. KIOSK only, names from the live state, never a node or HAOS, secrets absent = 503.
import tempfile  # noqa: E402
rp.SECRETS = tempfile.mkdtemp()
rp.DOCKER_HOSTS = {"ADMIN": "192.0.2.69"}
raw6 = dict(raw, seen=raw["seen"] + [({"host_name": "TEST-VM"}, 1), ({"host_name": "HAOS"}, 1)])
calls = []
VMS = [{"name": "test-vm", "vmid": 132, "node": "node3", "type": "qemu"},
       {"name": "haos", "vmid": 100, "node": "node2", "type": "qemu"}]
fake_pve = lambda m, path: calls.append((m, path)) or (VMS if m == "GET" else None)  # noqa: E731
fake_ssh = lambda addr, ct: calls.append(("ssh", addr, ct))  # noqa: E731
go = lambda body, client="192.0.2.89": rp.restart(body, client, raw6, fake_pve, fake_ssh)  # noqa: E731
assert go({"kind": "host", "name": "TEST-VM"}, "192.0.2.120")[0] == 403          # DESKVM: no
assert go({"kind": "host", "name": "TEST-VM"}) == (503, {"error": "no Proxmox token"})
assert go({"kind": "container", "name": "ADMIN ntfy"}) == (503, {"error": "no SSH key"})
for f in ("pve-token", "ssh-key"):
    open(os.path.join(rp.SECRETS, f), "w").write("x")
assert go({"kind": "host", "name": "NODE1"})[0] == 403                                     # a node
assert go({"kind": "host", "name": "HAOS"})[0] == 403                                       # by name
rp.PROTECTED = set()
assert go({"kind": "host", "name": "HAOS"})[0] == 403 and ("POST", "/nodes/node2/qemu/100/status/reboot") not in calls  # by vmid
assert go({"kind": "host", "name": "NOPE"})[0] == 404 and go({"kind": "evil", "name": "x"})[0] == 400
assert go({"kind": "host", "name": ["TEST-VM"]})[0] == 400
code, out = go({"kind": "host", "name": "TEST-VM"})
assert code == 200 and calls[-1] == ("POST", "/nodes/node3/qemu/132/status/reboot"), calls
assert go({"kind": "container", "name": "ADMIN ntfy"})[0] == 200 and calls[-1] == ("ssh", "192.0.2.69", "ntfy")
assert go({"kind": "container", "name": "APP app-db-1"})[0] == 403                        # not a declared Docker host
assert not rp.CT_NAME.fullmatch("-oProxyCommand=x") and not rp.CT_NAME.fullmatch("a b")
# A module left out of the config: its queries never run, its screens stay empty.
cfg = {**rp.DEFAULTS, "modules": {"hostmetrics": {}}}
rp.configure(cfg)
assert "kuma" not in rp.QUERIES and "wan_rx" not in rp.QUERIES and rp.fetch(lambda e: [({}, 1)])["kuma"] == []
assert [c[0] for c in rp.DETAIL["fleet"]["charts"]] == ["hosts", "svc", "cpu"]  # no Internet chart without a WAN
assert rp.public_config()["modules"] == ["hostmetrics"] and rp.ACTION_CLIENTS == set()
assert rp.unknown_keys({"listn": 1, "modules": {"dockr": {}, "docker": {"transient_host": []}}}) == [
    "unknown key 'listn', did you mean 'listen'?", "unknown key in modules: 'dockr', did you mean 'docker'?",
    "unknown key in module 'docker': 'transient_host', did you mean 'transient_hosts'?"]
# Every key the code reads is in the configuration reference; the version is the latest CHANGELOG entry.
here = os.path.dirname(os.path.abspath(__file__))
doc = open(os.path.join(here, "docs", "configuration.md"), encoding="utf-8").read()
missing = [k for k in [*rp.DEFAULTS, *rp.MODULES, *(o for m in rp.MODULES.values() for o in m)] if f"`{k}`" not in doc]
assert not missing, missing
log = open(os.path.join(here, "CHANGELOG.md"), encoding="utf-8").read()
assert rp.VERSION == re.search(r"^## (\S+)", log, re.M).group(1), rp.VERSION
# The example: a known config, and the made-up lab gives a full state and topology.
example = os.path.join(here, "example", "config.json")
assert rp.unknown_keys(json.load(open(example, encoding="utf-8"))) == []
rp.configure(rp.load_config(example))
spec = importlib.util.spec_from_file_location("fake", os.path.join(here, "example", "fake_prometheus.py"))
fake = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fake)
lab = rp.fetch({v: [(m, v2) for m, v2 in fake.rows(NOW)[k]] for k, v in rp.QUERIES.items()}.__getitem__)
st = rp.build_state(lab, NOW)
assert len(st["hosts"]) == 18 and st["wan"]["rx"] and len(st["services"]) == 10 and len(st["containers"]) == 16
assert "LEGACY is not answering" in st["alerts"] and st["backups"]["stale"] == ["MEDIA"], st["alerts"]
topo = rp.build_topology(fake.cairn_map(), lab)
assert {m["name"]: m["parent"] for m in topo["machines"]}["KIOSK"] == "AP1" and set(topo["vlans"]) == {"20", "99"}
assert len(topo["storage"]) == 2 and topo["spare"], topo["spare"]
for kind, name in (("host", "PVE1"), ("switch", "CORE"), ("pool", "NAS tank"), ("pbs", "PBS main"), ("container", "PROXY traefik")):
    d = rp.build_detail(kind, name, "1h", NOW, lambda e: [(r["metric"], float(r["value"][1])) for r in fake.instant_extra(e)],
                        lambda e, s, en, step: [(r["metric"], [(t, float(v)) for t, v in r["values"]]) for r in fake.range_rows(e, s, en, step)])
    assert d["charts"], (kind, d)
    if kind == "host":                                                   # the example shows two fans, in rpm
        fans = [s for c in d["charts"] if c["id"] == "fans" for s in c["series"]]
        assert [s["label"] for s in fans] == ["fan1", "fan2"] and all(p[1] > 500 for s in fans for p in s["points"])
print("ok")
