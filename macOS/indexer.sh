#!/bin/sh
# macOS/indexer.sh -- day-to-day control of the installed launchd worker
# (install it once with macOS/install.sh interval|daemon).
#
#   macOS/indexer.sh update    git pull; reinstall only if pyproject.toml changed; restart
#   macOS/indexer.sh restart   stop the running worker and start it again (new code, models reload)
#   macOS/indexer.sh stop      stop it; stays stopped until `start` (or the next login)
#   macOS/indexer.sh start     start it again after `stop`
#   macOS/indexer.sh status    running or not, and the last lines of the log
#   macOS/indexer.sh log       follow the log (Ctrl-C to leave)
set -e
REPO=$(cd "$(dirname "$0")/.." && pwd)
LABEL=com.a2cons.photo-indexer
DOMAIN="gui/$(id -u)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/a2-photo-indexer.log"
# The extras pip installs on a Mac (see README "Set up").
EXTRAS=${A2_INDEXER_EXTRAS:-ocr}

need_installed() {
    [ -f "$PLIST" ] || { echo "not installed -- run: macOS/install.sh daemon   (or: interval)" >&2; exit 1; }
}

loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }

restart() {
    need_installed
    if loaded; then
        # -k: kill the running process first, then start it -- a restart.
        launchctl kickstart -k "$DOMAIN/$LABEL"
    else
        launchctl bootstrap "$DOMAIN" "$PLIST"
    fi
    echo "restarted -- the models load again (a minute or two); '$0 log' to watch"
}

case "${1:-}" in
    update)
        cd "$REPO"
        before=$(git rev-parse HEAD)
        git pull --ff-only
        after=$(git rev-parse HEAD)
        if [ "$before" = "$after" ]; then
            echo "already up to date"
        else
            git log --oneline "$before..$after"
            if git diff --quiet "$before" "$after" -- pyproject.toml; then
                echo "dependencies unchanged -- no reinstall needed"
            else
                echo "pyproject.toml changed -- reinstalling"
                "$REPO/.venv/bin/pip" install -e "$REPO[$EXTRAS]"
            fi
        fi
        restart ;;
    restart)
        restart ;;
    stop)
        need_installed
        # bootout (not kill): a daemon's KeepAlive would just start it again.
        launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null && echo "stopped (until '$0 start' or the next login)" || echo "was not running" ;;
    start)
        need_installed
        if loaded; then
            launchctl kickstart "$DOMAIN/$LABEL"
        else
            launchctl bootstrap "$DOMAIN" "$PLIST"
        fi
        echo "started" ;;
    status)
        if [ ! -f "$PLIST" ]; then
            echo "not installed"
        elif loaded; then
            pid=$(launchctl print "$DOMAIN/$LABEL" | awk '/^\tpid = /{print $3}')
            mode=$(grep -q KeepAlive "$PLIST" && echo daemon || echo interval)
            if [ -n "$pid" ]; then
                echo "running ($mode), pid $pid, since $(ps -o lstart= -p "$pid")"
            else
                echo "installed ($mode), not running right now"
            fi
        else
            echo "installed but stopped -- '$0 start'"
        fi
        # Progress bars (model loading) are one huge \r-joined line: drop them.
        [ -f "$LOG" ] && { echo "--- last log lines:"; tail -c 200000 "$LOG" | tr '\r' '\n' | grep -v 'it/s\]' | grep -v '^[[:space:]]*$' | tail -n 8; } ;;
    log)
        tail -n 40 -f "$LOG" ;;
    *)
        sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
        exit 1 ;;
esac
