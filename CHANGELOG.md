# Changelog

## 1.2.0 (2026-11-17)

- Tiles: a host that exposes fans shows their speeds as one more value
  (`↻ 1450 · 1100`, in rpm), on the Nodes screen and wherever a host tile is
  drawn. `/api/state` gains `fans` per host (rpm by sensor name, or null).

## 1.1.1 (2026-11-17)

- Example: the fake Prometheus answers the fan query with two fans in rpm
  on the hosts that report a temperature (it answered with a temperature).

## 1.1.0 (2026-11-17)

- Host sheet: a "Fans" chart, one line per fan the host exposes through
  node_exporter (`node_hwmon_fan_rpm`, label `sensor`). Empty headers
  (0 rpm) are left out; a host without the metric shows no chart.

## 1.0.1 (2026-11-10)

- Host names are compared without case everywhere: a guest whose name
  Cairn Map spells differently from Prometheus (`web` and `WEB`) is still
  grouped under its node on the VM screen and coloured on the topology, and a
  node listed in `config.json` in another case still gets its big card and
  stays off the VM screen.

## 1.0.0 (2026-11-10)

First public release. Rack Panel first ran one home lab's rack screen;
this release turns everything that was specific to it into a setting.

- `config.json` (`-c`): Prometheus, nodes and other bare metal, and the
  sources as modules switched on one by one: `hostmetrics`, `uptime_kuma`,
  `mikrotik`, `docker`, `backups`, `topology`, `restart`. A module left out
  runs no query and its screens hide. Unknown keys are reported at start
  with the closest known one.
- English and French: English by default, `lang` in the config, and a
  language switch in Settings (kept by the screen, or by the browser).
- `--json` prints the state once; `/api/config` tells the page about the
  install (title, modules, WAN port, VLAN cards), never an address.
- Kiosk: the agent is `rack-panel-kiosk`, installed with `kiosk/install.sh`
  on Debian (cage + Chromium, foreground VT, compositor scaling, no
  translate bubble, no sleep).
- `example/`: a made-up lab served by a fake Prometheus, to try it without
  monitoring.
- Docker image `ghcr.io/jbellirigenty/rack-panel` (amd64, arm64).
