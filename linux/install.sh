#!/bin/sh
# linux/install.sh interval|daemon -- installs a systemd user unit (and
# removes the other mode's, so only one runs).
#   interval: `run --once` every 30 minutes (a2-photo-indexer-once.timer)
#   daemon:   `run` always on (a2-photo-indexer.service)
# linux/install.sh remove -- stops and removes both.
set -e
MODE=${1:?usage: $0 interval|daemon|remove}
REPO=$(cd "$(dirname "$0")/.." && pwd)
UNITS="$HOME/.config/systemd/user"
mkdir -p "$UNITS"

systemctl --user disable --now a2-photo-indexer.service a2-photo-indexer-once.timer 2>/dev/null || true
rm -f "$UNITS/a2-photo-indexer.service" "$UNITS/a2-photo-indexer-once.service" "$UNITS/a2-photo-indexer-once.timer"

case "$MODE" in
    daemon)
        sed "s|REPO/|$REPO/|" "$REPO/linux/a2-photo-indexer.service" > "$UNITS/a2-photo-indexer.service"
        systemctl --user daemon-reload
        systemctl --user enable --now a2-photo-indexer.service
        LOG="journalctl --user -u a2-photo-indexer -f" ;;
    interval)
        sed "s|REPO/|$REPO/|" "$REPO/linux/a2-photo-indexer-once.service" > "$UNITS/a2-photo-indexer-once.service"
        cp "$REPO/linux/a2-photo-indexer-once.timer" "$UNITS/"
        systemctl --user daemon-reload
        systemctl --user enable --now a2-photo-indexer-once.timer
        LOG="journalctl --user -u a2-photo-indexer-once -f" ;;
    remove)
        systemctl --user daemon-reload
        echo "removed"; exit 0 ;;
    *) echo "unknown mode: $MODE" >&2; exit 1 ;;
esac
loginctl enable-linger "$USER" 2>/dev/null || echo "note: 'loginctl enable-linger $USER' keeps it running with nobody logged in"
echo "installed ($MODE); log: $LOG"
