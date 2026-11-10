# Rack Panel

A live, touch-first status screen for a server rack: put an old tablet or
laptop next to the rack and it shows, without a keyboard or a login, what
every machine is doing right now.

- **Carousel** of screens, swiped or rotating on their own: overview and
  alerts, physical nodes, VMs and LXC containers, web services, Docker
  containers, storage and backups, network.
- **Rings** per element (one arc per measure: CPU, memory, disk; red past
  its alert level) or **bubbles** for busy screens, grouped by node or host.
- **Detail sheets**: touch anything for live charts over 1 h, 6 h, 24 h or
  7 days, with key facts and its busiest parts (mount points, ports,
  datasets, oldest backups).
- **Live map** (swipe up): six zones at a glance, or the topology: Internet,
  router, switches, machines and their guests, links running faster with
  the traffic, one card per VLAN and one for direct storage links.
- **Restart** a VM, an LXC container or a Docker container from its sheet,
  behind an admin code typed on the screen (optional).
- **Settings** pulled down from the top: brightness, night mode, sleep when
  nobody is in front of the camera, rotation, alert thresholds, language
  (English or French).

Rack Panel **reads, stores nothing and never sends a query it did not
write**: a fixed set of PromQL queries, folded into one JSON document. It
sits next to the [Cairn](https://melior.digital) tools (its topology comes
from [Cairn Map](https://github.com/JBelliriGenty/Cairn-Map)) without being
one of them. Python standard library only, one server file, one page.

## What it needs

Prometheus, holding what your rack already exports. Each source is a
**module** you switch on in `config.json`:

| Module | Reads | Gives |
|---|---|---|
| `hostmetrics` | OpenTelemetry `hostmetrics` receiver (`system_*`, label `host_name`); node_exporter's `node_hwmon_*` and `node_zfs_arc_*` if present | nodes, VMs, disks, temperatures, host traffic |
| `uptime_kuma` | Uptime Kuma's `/metrics` (`monitor_status`, `monitor_response_time`) | web services, probe alerts |
| `mikrotik` | [mktxp](https://github.com/akpw/mktxp) (`mktxp_*`) | switches, ports, Internet throughput |
| `docker` | OpenTelemetry `docker_stats` receiver (`container_*`) | containers, stopped ones, restarts |
| `backups` | [pbs-exporter](https://github.com/natrontech/pbs-exporter) (`pbs_*`) or any "last backup time" series | datastore fill, late backups |
| `topology` | [Cairn Map](https://github.com/JBelliriGenty/Cairn-Map)'s `/api/map` | the live map's topology |
| `restart` | the Proxmox API and SSH | restart buttons on the sheets |

A module left out costs nothing: its queries never run and its screens
hide.

## Try it

No monitoring needed: `example/` holds a made-up lab (three Proxmox nodes,
a NAS, a dozen guests, two MikroTik devices, probes and containers) served
by a fake Prometheus.

```sh
python3 example/fake_prometheus.py &          # 127.0.0.1:9099
python3 rack-panel.py -c example/config.json  # http://127.0.0.1:3006
python3 test_rack_panel.py                    # self-check, prints "ok"
```

`python3 rack-panel.py -c config.json --json` prints the state once and
exits: the quickest way to check a config against your Prometheus.

## Run it

Docker: an image is published for each release (Alpine, amd64 and arm64,
runs as `nobody`). Mount the folder holding your `config.json` (with
`"listen": "0.0.0.0:3006"`) at `/etc/rack-panel`:

```sh
docker run -d --name rack-panel -p 3006:3006 -v $PWD/config:/etc/rack-panel:ro ghcr.io/jbellirigenty/rack-panel:1
```

Or straight from a checkout: `python3 rack-panel.py -c config.json`, or
`docker build -t rack-panel .`

There is no login: the screen has no keyboard. Keep the port on your LAN,
open to the screen (and your desk) only.

## The screen: kiosk on Debian

`kiosk/install.sh` turns a Debian 12 or 13 machine (a tablet, an old
laptop) into the screen: [cage](https://github.com/cage-kiosk/cage), a
single-application Wayland compositor, runs Chromium full screen on the
local agent `rack-panel-kiosk`, which serves the page from your server and
keeps the screen's settings.

```sh
sudo sh kiosk/install.sh --server http://192.0.2.10:3006 --scale 2 --lang en
```

Run it again at will: only what changed is rewritten. It handles the traps
met on real hardware:

- cage hangs silently unless its session is on the **foreground VT**: the
  service switches to tty1 first (`chvt 1`);
- on a HiDPI screen, Chromium's `--force-device-scale-factor` fills a
  quarter of the screen under Wayland: the compositor scales instead
  (`--scale`, applied with `wlr-randr`);
- Chromium offers to **translate** the page: switched off by policy;
- suspend, hibernate and the lid switch are disabled; a desktop installed
  alongside has its login manager disabled (not removed).

The agent (root, `127.0.0.1:3007` only) drives the backlight (night mode,
wake on touch, sleep when the front camera sees nobody: frames stay in
memory, never written or sent), restarts the display, reboots or powers off
the machine, and holds the admin code for restarts. Its settings live in
`/var/lib/rack-panel-kiosk/`. Forgot the admin code:
`sudo rm /var/lib/rack-panel-kiosk/admin.json && sudo systemctl restart rack-panel-kiosk`.

Screenshot of the screen, from SSH:
`sudo -u kiosk env XDG_RUNTIME_DIR=/run/user/$(id -u kiosk) WAYLAND_DISPLAY=wayland-0 grim /tmp/screen.png`.

Any other browser also works: open `http://server:3006/` (settings that
belong to the kiosk then stay greyed out; the language is kept in the
browser).

## Config

Every key, module by module, with defaults and the restart setup (Proxmox
token, SSH forced command): **[docs/configuration.md](docs/configuration.md)**.

## License

MIT, see [LICENSE](LICENSE).
