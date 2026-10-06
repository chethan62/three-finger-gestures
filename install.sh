#!/usr/bin/env bash
# three-finger-gestures -- install / uninstall with verification.
#
#   curl -fsSL https://raw.githubusercontent.com/chethan62/three-finger-gestures/main/install.sh | bash
#
#   ./install.sh              install or reinstall, then verify
#   ./install.sh --verify     verification only (changes nothing)
#   ./install.sh --uninstall  stop and remove the systemd unit
#   ./install.sh --purge      as --uninstall, and delete this source directory
#
# Idempotent: re-running install rewrites the unit and restarts the service.
# The unit's ExecStart is derived from THIS script's location, so the unit can
# never point at a stale copy.
set -euo pipefail

# Piped to bash there is no BASH_SOURCE (and `set -u` would make reading it an
# error), and no sources beside us either. Fall back to the cwd; fetch_sources()
# below notices the daemon is missing and fixes both.
SRC="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-}")" 2>/dev/null && pwd)"
DAEMON="$SRC/three-finger-gestures.py"
TEST="$SRC/test-three-finger-gestures.py"
SVC=three-finger-gestures
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/$SVC.service"
PY=/usr/bin/python3
DEST="${TFG_DEST:-$HOME/.local/share/$SVC}"
REPO=https://github.com/chethan62/$SVC.git

ok()  { printf '  ok    %s\n' "$*"; }
bad() { printf '  FAIL  %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 2; }

# One-command install: with no sources beside this script, fetch them and re-exec
# from the checkout so everything downstream behaves identically.
fetch_sources() {
  command -v git >/dev/null || die "git not found (needed to fetch the sources)"
  if [[ -e $DEST && ! -d $DEST/.git ]]; then
    die "$DEST exists and is not a git checkout -- move it aside, or set TFG_DEST"
  fi
  if [[ -d $DEST/.git ]]; then
    ok "updating $DEST"
    git -C "$DEST" pull --ff-only >/dev/null
  else
    ok "fetching sources into $DEST"
    git clone --depth 1 "$REPO" "$DEST" >/dev/null
  fi
  exec "$DEST/install.sh" "${1:-install}"
}

preflight() {
  [[ -x $PY ]]                 || die "python3 not found at $PY"
  command -v qdbus6 >/dev/null || die "qdbus6 not found (provides KDE's DBus CLI)"
  [[ -f $DAEMON && -f $TEST ]] || die "missing source files in $SRC"
  systemctl --user show-environment >/dev/null 2>&1 || die "no systemd user session"
  id -nG | tr ' ' '\n' | grep -qx input || die "user '$USER' is not in the 'input' group"
  grep -qi touchpad /sys/class/input/event*/device/name 2>/dev/null \
    || die "no touchpad found under /sys/class/input"
  command -v ydotool >/dev/null || die "ydotool not found (moves the Overview selection)"
  ok "preconditions (python3, qdbus6, ydotool, user session, input group, touchpad)"
}

write_unit() {
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT" <<EOF
[Unit]
Description=Three-finger swipe gestures (Windows 11 style)
PartOf=graphical-session.target

[Service]
ExecStart=$DAEMON
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical-session.target
EOF
  ok "unit written: $UNIT"
}

verify() {
  local rc=0 pid=
  systemctl --user is-active --quiet "$SVC" \
    && ok "service active" || { bad "service not active"; rc=1; }
  [[ "$(systemctl --user is-enabled "$SVC" 2>/dev/null)" == enabled ]] \
    && ok "enabled at login" || { bad "not enabled at login"; rc=1; }
  systemctl --user is-active --quiet ydotool \
    && ok "ydotoold running (Overview selection keys)" \
    || { bad "ydotoold not running -- Overview selection cannot move"; rc=1; }
  pid=$(systemctl --user show -p MainPID --value "$SVC")
  if [[ -n $pid ]] && ls -l "/proc/$pid/fd" 2>/dev/null | grep -q '/dev/input/event'; then
    ok "attached to a /dev/input/event node (pid $pid)"
  else
    bad "process holds no /dev/input/event fd (pid ${pid:-none})"; rc=1
  fi
  if journalctl --user -u "$SVC" --since '-30s' --no-pager 2>/dev/null | grep -q WARNING; then
    bad "startup warnings -- a mapped action name is not registered:"
    journalctl --user -u "$SVC" --since '-30s' --no-pager | grep WARNING | sed 's/^/        /'
    rc=1
  else
    ok "all mapped action names resolve"
  fi
  if "$PY" -B "$TEST" >/dev/null 2>&1; then
    ok "self-check passes"
  else
    bad "self-check failed:"; "$PY" -B "$TEST" 2>&1 | sed 's/^/        /'; rc=1
  fi
  return $rc
}

do_install() {
  preflight
  write_unit
  systemctl --user daemon-reload
  # ydotoold supplies the Overview arrow keys; without it those swipes no-op
  systemctl --user enable --now ydotool >/dev/null 2>&1 || true
  systemctl --user enable "$SVC" >/dev/null 2>&1 || true
  systemctl --user restart "$SVC"
  sleep 2
  echo "verifying:"
  if verify; then echo "INSTALL OK"; else echo "INSTALL FAILED"; exit 1; fi
}

do_uninstall() {
  systemctl --user disable --now "$SVC" >/dev/null 2>&1 || true
  rm -f "$UNIT"
  systemctl --user daemon-reload
  echo "uninstalled $SVC (unit removed, sources kept)"
  if [[ "${1:-}" == --purge ]]; then
    rm -rf "$SRC"
    echo "purged source directory $SRC"
  fi
}

# --uninstall/--purge need no sources, so only the other actions trigger a fetch.
if [[ ! -f $DAEMON ]]; then
  case "${1:-install}" in
    --uninstall|--purge) ;;
    *) fetch_sources "${1:-install}" ;;
  esac
fi

case "${1:-install}" in
  install)     do_install ;;
  --verify)    echo "verifying:"; verify ;;
  --uninstall) do_uninstall ;;
  --purge)
    # Checked before anything is touched: piped to bash $SRC is the cwd, and
    # rm -rf'ing that would be a nasty surprise. Refuse rather than half-uninstall.
    [[ -f $DAEMON ]] || die "refusing to purge $SRC -- no daemon there; run --purge from the installed source dir"
    do_uninstall --purge ;;
  *)           die "usage: $0 [install|--verify|--uninstall|--purge]" ;;
esac
