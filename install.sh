#!/usr/bin/env bash
# Install g935-linux for the current user (and optionally system udev rules).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}/g935-linux"
BIN="${XDG_BIN_HOME:-$HOME/.local/bin}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"

usage() {
  cat <<EOF
Usage: $0 [--user] [--udev] [--uninstall-user]

  --user             Install package, wrappers, systemd unit, desktop entry
  --udev             Install udev rule + optional mic-mute hwdb (needs sudo)
  --uninstall-user   Remove user install (not udev)
  (no args)          Same as --user
EOF
}

install_user() {
  mkdir -p "$PREFIX" "$BIN" "$UNIT_DIR" "$APP_DIR" "$AUTOSTART_DIR"
  rm -rf "$PREFIX/g935"
  cp -a "$ROOT/g935" "$PREFIX/g935"
  find "$PREFIX/g935" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
  find "$PREFIX/g935" -name '*.pyc' -delete 2>/dev/null || true
  install -m 755 "$ROOT/g935-control.py" "$PREFIX/g935-control.py"
  install -m 755 "$ROOT/g935-dspd.py" "$PREFIX/g935-dspd.py"
  mkdir -p "$PREFIX/icons"
  install -m 644 "$ROOT/icons/g935-control.svg" "$PREFIX/icons/g935-control.svg"
  icon_home="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
  mkdir -p "$icon_home"
  install -m 644 "$ROOT/icons/g935-control.svg" "$icon_home/g935-control.svg"
  # thin wrappers so PATH finds them and PYTHONPATH is set.
  # exec -a sets argv[0] to the desktop id (Wayland taskbar matching).
  cat > "$BIN/g935-control" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="$PREFIX\${PYTHONPATH:+:\$PYTHONPATH}"
exec -a g935-control /usr/bin/python3 "$PREFIX/g935-control.py" "\$@"
EOF
  cat > "$BIN/g935-dspd" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="$PREFIX\${PYTHONPATH:+:\$PYTHONPATH}"
exec /usr/bin/python3 "$PREFIX/g935-dspd.py" "\$@"
EOF
  chmod 755 "$BIN/g935-control" "$BIN/g935-dspd"

  # systemd unit with correct ExecStart
  cat > "$UNIT_DIR/g935-dsp.service" <<EOF
[Unit]
Description=G935 DSP soundstage + mic daemon (HID++ power-on watcher)
After=graphical-session.target
PartOf=graphical-session.target

[Service]
ExecStart=$BIN/g935-dspd
Restart=always
RestartSec=5
SyslogIdentifier=g935-dspd

[Install]
WantedBy=graphical-session.target
EOF

  # desktop entry (absolute Exec so it still launches if PATH is thin)
  cat > "$APP_DIR/g935-control.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=G935 Control
Comment=Sidetone, EQ and lighting control for the Logitech G935
Exec=$BIN/g935-control
Icon=g935-control
Terminal=false
Categories=AudioVideo;Audio;Settings;
StartupNotify=false
StartupWMClass=g935-control
EOF

  # Persistent tray icon (the daemon is headless). Hidden start; click Show.
  # systemd-xdg-autostart-generator runs before ~/.local/bin is on PATH, so
  # Exec= must be absolute or it logs "does not exist" and skips the unit.
  cat > "$AUTOSTART_DIR/g935-control.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=G935 Control
Comment=G935 tray icon
Exec=$BIN/g935-control --tray
TryExec=$BIN/g935-control
Icon=g935-control
Terminal=false
Categories=AudioVideo;Audio;Settings;
StartupNotify=false
StartupWMClass=g935-control
Hidden=false
X-GNOME-Autostart-enabled=true
X-KDE-autostart-after=panel
X-KDE-autostart-phase=2
EOF

  systemctl --user daemon-reload 2>/dev/null || true
  gtk-update-icon-cache -f -t \
    "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" >/dev/null 2>&1 || true
  echo "Installed user components to:"
  echo "  package:  $PREFIX"
  echo "  bins:     $BIN/g935-control  $BIN/g935-dspd"
  echo "  unit:     $UNIT_DIR/g935-dsp.service"
  echo "  desktop:  $APP_DIR/g935-control.desktop"
  echo "  autostart:$AUTOSTART_DIR/g935-control.desktop"
  echo
  echo "Enable the daemon (G HUB mode mic + power-on DSP):"
  echo "  systemctl --user enable --now g935-dsp"
  echo
  if ! [[ ":$PATH:" == *":$BIN:"* ]]; then
    echo "Note: $BIN is not on your PATH — add it or log out/in."
  fi
}

install_udev() {
  echo "Installing udev rule (and optional hwdb) — needs sudo..."
  sudo cp "$ROOT/99-g935.rules" /etc/udev/rules.d/
  sudo cp "$ROOT/70-g935-micmute.hwdb" /etc/udev/hwdb.d/
  sudo udevadm control --reload
  sudo systemd-hwdb update
  sudo udevadm trigger
  echo "Done. Unplug/replug the receiver so ACLs refresh."
}

uninstall_user() {
  rm -f "$BIN/g935-control" "$BIN/g935-dspd"
  rm -f "$UNIT_DIR/g935-dsp.service"
  rm -f "$APP_DIR/g935-control.desktop"
  rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/g935-control.desktop"
  rm -f "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps/g935-control.svg"
  rm -rf "$PREFIX"
  systemctl --user daemon-reload 2>/dev/null || true
  echo "Removed user install."
}

if [[ $# -eq 0 ]]; then
  install_user
  exit 0
fi

did=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) install_user; did=1 ;;
    --udev) install_udev; did=1 ;;
    --uninstall-user) uninstall_user; did=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1"; usage; exit 1 ;;
  esac
  shift
done
[[ $did -eq 1 ]] || usage
