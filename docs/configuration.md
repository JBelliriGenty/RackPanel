# Configuration reference

Everything Rack Panel reads, key by key. The [README](../README.md) says what
the screen shows; this page is what you edit.

| File | Who writes it | |
|---|---|---|
| `config.json` (`-c`) | you | the server: Prometheus, machines, modules |
| secret files (`token_file`, `ssh_key_file`, `ca_file`) | you | restart module only, next to `config.json` |
| `/var/lib/rack-panel-kiosk/settings.json` | the screen's Settings | brightness, night, rotation, thresholds, language |
| `/var/lib/rack-panel-kiosk/admin.json` | the screen | the admin code, as a salted PBKDF2 hash |

Relative paths are read from the config file's folder. With Docker, that is
the folder mounted at `/etc/rack-panel`.

Check a config without starting the server:

```sh
python3 rack-panel.py -c config.json --json
```

It prints `/api/state` once. A key nothing reads (a typo such as `listn`),
at the top, under `modules` or inside a module, is reported at start on
standard error with the closest known key ("did you mean 'listen'?"), and
otherwise ignored.

## config.json

| Key | Type | Default | |
|---|---|---|---|
| `title` | string | `"Rack Panel"` | top left of the screen and the browser tab |
| `lang` | `"en"` \| `"fr"` | `"en"` | the page's language until someone picks one in Settings |
| `listen` | `"host:port"` | `"127.0.0.1:3006"` | `"0.0.0.0:3006"` in Docker |
| `prometheus` | URL | `"http://127.0.0.1:9090"` | |
| `nodes` | list of names | `[]` | the Nodes screen, in this order: hypervisors, NAS, any machine worth a big card |
| `physical` | list of names | `[]` | other bare metal: kept off the VM screen and never restarted, like `nodes` |
| `modules` | object | `{"hostmetrics": {}}` | module name → its options; a module not listed is off |

Names are the `host_name` label of the OpenTelemetry metrics, compared
without case. A name that never reported shows as "no data" on the Nodes
screen.

## Modules

### `hostmetrics`

No option. Reads the OpenTelemetry Collector's
[`hostmetrics` receiver](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/receiver/hostmetricsreceiver)
(cpu, memory, filesystem, network, load, processes scrapers) with the
`host_name` label. Optional, from node_exporter on the same `host_name`:
`node_hwmon_temp_celsius` (temperature; network chips and sensors above
110 °C are ignored), `node_hwmon_fan_rpm` (fan speeds on the host sheet,
one line per `sensor`) and `node_zfs_arc_*` (the part of the ZFS ARC the
system can take back is not counted as used memory). A host seen in the
last day but silent now is reported as not answering.

### `uptime_kuma`

Uptime Kuma's own `/metrics`, scraped by Prometheus. HTTP, keyword and
JSON-query monitors are the Services screen; every other monitor (ping,
port…) only raises alerts. Probe URLs never reach the page, only their zone:

| Key | Type | Default | |
|---|---|---|---|
| `public` | list of suffixes | `[]` | a probe whose host ends with one of these is **Public** |
| `internal` | list of suffixes | `[".home", ".lan", ".local", ".internal"]` | **Internal**; anything else (an IP) is **Direct** |

### `mikrotik`

[mktxp](https://github.com/akpw/mktxp) metrics: switch CPU and
temperatures, per-port traffic, neighbours (for the topology).

| Key | Type | Default | |
|---|---|---|---|
| `wan` | `"<router> <interface>"` | `""` | the Internet link, as mktxp names it (`routerboard_name` and `name`), e.g. `"core ether1"`. Empty: no Internet figures |

### `docker`

The OpenTelemetry
[`docker_stats` receiver](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/receiver/dockerstatsreceiver)
(`container_*`, labels `host_name` and `container_name`). A container seen in
the last day but gone now is shown as stopped, unless it matches:

| Key | Type | Default | |
|---|---|---|---|
| `ephemeral` | regex | CI task names and Docker's random names | containers that come and go (never "stopped") |
| `transient_hosts` | list of names | `[]` | hosts whose containers come and go (a CI runner) |

### `backups`

[pbs-exporter](https://github.com/natrontech/pbs-exporter) (`pbs_used`,
`pbs_size`, `pbs_snapshot_*`) for the datastore, plus one series giving each
guest's last good backup time (Unix seconds):

| Key | Type | Default | |
|---|---|---|---|
| `query` | PromQL | `"max by (vm_name)(pbs_snapshot_vm_last_timestamp)"` | one row per guest |
| `label` | string | `"vm_name"` | the label holding the guest's name |

A backup older than the screen's threshold (26 h by default, set in
Settings) is an alert.

### `topology`

The live map's topology comes from
[Cairn Map](https://github.com/JBelliriGenty/Cairn-Map) `/api/map`, re-read
every 5 minutes: which guests run on which node (Cairn IaC or the Proxmox
module), which machine sits on which switch port (Cairn Net). Rack Panel
must be one of Cairn Map's `trusted_proxies`; it sends the user and groups
below.

| Key | Type | Default | |
|---|---|---|---|
| `url` | URL | `""` | Cairn Map. Empty: no topology (the Zones view still works) |
| `user` | string | `"rack-panel"` | sent as `Remote-User` |
| `groups` | string | `""` | sent as `Remote-Groups` when set (to meet Cairn Map's `require_groups`) |
| `data_vlan` | string | `""` | Cairn Net's name of the data VLAN (`"vlan10"`): the main card, not repeated |
| `admin_vlan` | string | `""` | the switches' management VLAN (`"vlan99"`): a node's port there is drawn apart, dotted, and that card is rooted on the switch with the most management links |
| `vlans` | list of `{"id", "name"}` | `[]` | card names and order for the other VLANs (`{"id": "20", "name": "IoT"}`); unlisted ones follow by number |
| `storage_links` | list of `[host, interface, peer]` | `[]` | cables no switch sees (a NAS wired straight to a node): a card of their own, their rate from the host's interface |

### `restart`

A **Restart** button on VM, LXC and container sheets, only on the screen
that runs the kiosk agent: two touches, then the admin code (4 to 8 digits,
set in Settings; 5 wrong codes lock it for 5 minutes). The agent checks the
code, then asks the server, which only takes it from `clients`. Nodes,
`physical` machines and anything protected are never restarted.

| Key | Type | Default | |
|---|---|---|---|
| `clients` | list of IPs | `[]` | the screen's address, as the server sees it |
| `pve_api` | list of URLs | `[]` | Proxmox nodes (`https://pve1:8006`), tried in turn |
| `pve_token_id` | string | `""` | `user@realm!token` |
| `token_file` | path | `"pve-token"` | the token's secret, alone in the file |
| `ca_file` | path | `""` | the CA of the Proxmox certificates; empty: the system's |
| `ssh_key_file` | path | `"ssh-key"` | private key for container restarts |
| `ssh_user` | string | `"root"` | |
| `protected_vmids` | list of numbers | `[]` | never restarted (Home Assistant OS, the firewall…) |
| `protected_names` | list of names | `[]` | same, by name |
| `docker_hosts` | object | `{}` | host name (`host_name`) → address to SSH to; only these take container restarts |

A missing token or key is not an error: the button answers "no Proxmox
token" or "no SSH key".

**Proxmox token**: a role with `VM.Audit` and `VM.PowerMgmt` only, on `/vms`:

```sh
pveum role add RackPanelRestart -privs "VM.Audit VM.PowerMgmt"
pveum user add rack-panel@pve
pveum acl modify /vms -user rack-panel@pve -role RackPanelRestart
pveum user token add rack-panel@pve restart --privsep 0   # secret -> pve-token
```

**Docker hosts**: the key may only run `rack-panel-restart` (a
`docker restart` of a valid name), and only from the server. Copy
`rack-panel-restart` to `/usr/local/sbin/` on each host, then add to
`/root/.ssh/authorized_keys`:

```
restrict,from="192.0.2.10",command="/usr/local/sbin/rack-panel-restart" ssh-ed25519 AAAA… rack-panel
```

## Kiosk agent

`rack-panel-kiosk` is set by `kiosk/install.sh` (`--server`, `--scale`,
`--lang`); its environment, in `/etc/systemd/system/rack-panel-kiosk.service`:

| Variable | Default | |
|---|---|---|
| `UPSTREAM` | `http://127.0.0.1:3006` | the Rack Panel server |
| `LISTEN` | `127.0.0.1:3007` | where Chromium opens the page |
| `STATE_DIR` | `/var/lib/rack-panel-kiosk` | settings, admin code, last page (served when the server is down) |
| `WIFI_IF` | the first wireless interface | shown in Settings |
| `CAMERA` | `/dev/video0` | presence; no camera = the screen never sleeps for lack of it |

The screen's settings (`settings.json`) are bounded and checked by the
agent: brightness, night window and wake time, sleep after N minutes with
nobody in front of the camera and the motion threshold, time per screen and
pause after a touch, screens shown, ring or bubble view per screen, the
four alert thresholds (`disk`, `temp`, `sw_temp`, `backup_h`, sent to the
server with each refresh) and the language.
