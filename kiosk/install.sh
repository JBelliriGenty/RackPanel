#!/bin/sh
# rack-panel kiosk on Debian (12 or 13): cage (single-app Wayland compositor) runs Chromium full screen on
# the local agent rack-panel-kiosk (127.0.0.1:3007), which serves the rack-panel page from the server, keeps
# the screen settings and drives the backlight. Run as root, again at will: only what changed is rewritten
# and restarted ("changed: <file>" lines).
#
#   sh install.sh --server http://rack-panel.lan:3006 [--scale 2] [--lang en]
#
# --scale: HiDPI factor set by the compositor (wlr-randr). Chromium's --force-device-scale-factor only fills
# a quarter of the screen under Wayland. --lang: Chromium's UI language (the page has its own setting).
set -eu

SERVER="" SCALE=1 LANG_UI=en
while [ $# -gt 0 ]; do
  case "$1" in
    --server) SERVER=$2; shift 2 ;;
    --scale) SCALE=$2; shift 2 ;;
    --lang) LANG_UI=$2; shift 2 ;;
    *) echo "usage: $0 --server URL [--scale N] [--lang xx]" >&2; exit 2 ;;
  esac
done
[ -n "$SERVER" ] || { echo "--server is required (the rack-panel server, e.g. http://192.0.2.10:3006)" >&2; exit 2; }
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 2; }
HERE=$(cd "$(dirname "$0")" && pwd)
AGENT=0 KIOSK=0 UNITS=0

# put PATH MODE < content: write only when different.
put() {
  tmp=$(mktemp)
  cat >"$tmp"
  if [ -f "$1" ] && cmp -s "$tmp" "$1"; then rm -f "$tmp"; return 1; fi
  mkdir -p "$(dirname "$1")"
  install -m "$2" "$tmp" "$1"
  rm -f "$tmp"
  echo "changed: $1"
}

# kbd: chvt; grim: screenshots for troubleshooting; ffmpeg: front camera frames for presence (memory only).
PKGS="cage chromium wlr-randr kbd grim ffmpeg iw python3"
MISSING=""
for p in $PKGS; do dpkg -s "$p" >/dev/null 2>&1 || MISSING="$MISSING $p"; done
if [ -n "$MISSING" ]; then
  apt-get update -q
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends $MISSING
  echo "changed: packages$MISSING"
fi

# A user with no password and no shell: it only runs cage.
if ! id kiosk >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin kiosk
  echo "changed: user kiosk"
fi
for g in video input render; do
  if getent group "$g" >/dev/null && ! id -nG kiosk | grep -qw "$g"; then usermod -aG "$g" kiosk; echo "changed: kiosk in $g"; fi
done

# logind session (screen and touch access) for the kiosk service.
put /etc/pam.d/cage 0644 <<'EOF' && KIOSK=1 || true
# Written by rack-panel kiosk/install.sh.
auth     required pam_unix.so nullok
account  required pam_unix.so
session  required pam_unix.so
session  required pam_systemd.so
EOF

# Private window: no "restore pages" bubble after a power cut. Pinch zoom and swipe-back off (swipes
# drive the carousel).
put /usr/local/bin/kiosk-session 0755 <<EOF && KIOSK=1 || true
#!/bin/sh
# Written by rack-panel kiosk/install.sh.
out=\$(wlr-randr | head -1 | cut -d' ' -f1)
wlr-randr --output "\$out" --scale $SCALE
exec /usr/bin/chromium --kiosk --incognito --noerrdialogs --disable-infobars --no-first-run \\
  --password-store=basic --ozone-platform=wayland --lang=$LANG_UI --disable-pinch \\
  --overscroll-history-navigation=0 --check-for-update-interval=31536000 \\
  http://127.0.0.1:3007/
EOF

# Without it, Chromium offers to translate the page.
put /etc/chromium/policies/managed/kiosk.json 0644 <<'EOF' && KIOSK=1 || true
{"TranslateEnabled": false}
EOF

put /usr/local/lib/rack-panel-kiosk/rack-panel-kiosk.py 0755 <"$HERE/rack-panel-kiosk.py" && AGENT=1 || true

# The agent runs as root (backlight, systemctl), on loopback only; settings in /var/lib/rack-panel-kiosk.
put /etc/systemd/system/rack-panel-kiosk.service 0644 <<EOF && { AGENT=1; UNITS=1; } || true
# Written by rack-panel kiosk/install.sh.
[Unit]
Description=rack-panel kiosk agent: page, screen settings, local actions
After=network-online.target
Wants=network-online.target

[Service]
Environment=UPSTREAM=$SERVER
Environment=LISTEN=127.0.0.1:3007
Environment=PYTHONDONTWRITEBYTECODE=1
StateDirectory=rack-panel-kiosk
ExecStart=/usr/bin/python3 /usr/local/lib/rack-panel-kiosk/rack-panel-kiosk.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

# cage only gets the screen when its session is on the foreground VT (else it hangs, silently): chvt 1 first.
put /etc/systemd/system/kiosk.service 0644 <<'EOF' && { KIOSK=1; UNITS=1; } || true
# Written by rack-panel kiosk/install.sh.
[Unit]
Description=Kiosk: rack-panel in Chromium (cage)
After=systemd-user-sessions.service network-online.target rack-panel-kiosk.service
Wants=rack-panel-kiosk.service
Wants=network-online.target
Conflicts=getty@tty1.service

[Service]
User=kiosk
ExecStartPre=+/usr/bin/chvt 1
# Chromium ignores SIGTERM under cage: don't wait 90 s.
TimeoutStopSec=10
PAMName=cage
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes
TTYVTDisallocate=yes
UtmpIdentifier=tty1
UtmpMode=user
StandardInput=tty-fail
StandardOutput=journal
StandardError=journal
ExecStart=/usr/bin/cage -d -- /usr/local/bin/kiosk-session
Restart=always
RestartSec=5

[Install]
WantedBy=graphical.target
EOF

# Never sleep; closing a keyboard cover must not suspend anything (logind reads it at next boot).
for t in sleep suspend hibernate hybrid-sleep; do
  [ "$(systemctl is-enabled "$t.target" 2>/dev/null)" = masked ] || { systemctl mask -q "$t.target"; echo "changed: $t.target masked"; }
done
put /etc/systemd/logind.conf.d/kiosk.conf 0644 <<'EOF' || true
# Written by rack-panel kiosk/install.sh.
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
IdleAction=ignore
EOF

# A desktop installed alongside would take the screen (and ~1 GB of RAM): its login manager is disabled, not removed.
for dm in gdm3 lightdm sddm; do
  if systemctl is-enabled -q "$dm" 2>/dev/null; then systemctl disable -q --now "$dm"; echo "changed: $dm disabled"; fi
done
if [ "$(systemctl get-default)" != graphical.target ]; then systemctl set-default -q graphical.target; echo "changed: default target"; fi

if [ $UNITS = 1 ]; then systemctl daemon-reload; fi
systemctl enable -q rack-panel-kiosk kiosk
if [ $AGENT = 1 ]; then systemctl restart rack-panel-kiosk; else systemctl start rack-panel-kiosk; fi
if [ $KIOSK = 1 ]; then systemctl restart kiosk; else systemctl start kiosk; fi
echo "ok: kiosk on http://127.0.0.1:3007/ (server $SERVER)"
