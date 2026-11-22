#!/usr/bin/env bash
# Deploy the price monitor to the biolete.ro host and run it from cron.
#
#   ./deploy.sh            copy the code + .env, set up the venv, install the cron job
#   ./deploy.sh --remove   remove the cron job (leaves the files in place)
#
# Re-run after changing the code or .env. Searches, intervals and on/off live in
# the database (edit them in the dashboard), so changing those needs no deploy.
# Server-only settings (MySQL credentials, cron schedule) live in .env.server and
# are appended to the remote .env; the local .env keeps using SQLite.
set -euo pipefail
cd "$(dirname "$0")"

HOST="biolete.ro"                                # SSH alias from ~/.ssh/config
REMOTE_DIR="apps/price-monitor"                  # relative to the remote home
PYTHON="/opt/alt/python311/bin/python3"          # the system python3 there is 3.6
LOG="logs/cron-price-monitor.log"
CRON_TAG="# price-monitor"

# How often cron wakes the monitor up. Each search's own interval (in the database)
# decides whether it actually runs, so this is the finest interval you can use.
CRON_SCHEDULE="$(grep -E '^CRON_SCHEDULE=' .env.server 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"'" || true)"
CRON_SCHEDULE="${CRON_SCHEDULE:-*/5 * * * *}"
[[ $(wc -w <<<"$CRON_SCHEDULE") -eq 5 ]] || { echo "CRON_SCHEDULE must have 5 fields, got: $CRON_SCHEDULE"; exit 1; }
CRON_LINE="$CRON_SCHEDULE cd ~/$REMOTE_DIR && timeout 280 .venv/bin/python -W ignore -m price_monitor --once >> ~/$LOG 2>&1 $CRON_TAG"

if [[ "${1:-}" == "--remove" ]]; then
  ssh "$HOST" "crontab -l 2>/dev/null | grep -vF '$CRON_TAG' | crontab -"
  echo "Cron job removed."
  exit 0
fi

[[ -f .env ]] || { echo "Missing .env (copy .env.example and fill it in)"; exit 1; }
[[ -f .env.server ]] || { echo "Missing .env.server (MySQL settings for the server)"; exit 1; }

echo "==> Copying files to $HOST:~/$REMOTE_DIR"
ssh "$HOST" "mkdir -p ~/$REMOTE_DIR/price_monitor/providers ~/logs"
# Remote .env = local .env + server-only settings
cat .env .env.server | ssh "$HOST" "umask 077 && cat > ~/$REMOTE_DIR/.env"
scp -q requirements.txt searches.example.json "$HOST:$REMOTE_DIR/"
scp -q price_monitor/*.py "$HOST:$REMOTE_DIR/price_monitor/"
scp -q price_monitor/providers/*.py "$HOST:$REMOTE_DIR/price_monitor/providers/"
# Drop modules that no longer exist locally (e.g. a removed provider)
ssh "$HOST" "cd ~/$REMOTE_DIR && for f in price_monitor/*.py price_monitor/providers/*.py; do echo \$f; done" \
  | while read -r f; do [[ -f "$f" ]] || ssh "$HOST" "rm -f ~/$REMOTE_DIR/$f" < /dev/null; done

echo "==> Setting up the virtualenv"
ssh "$HOST" "cd ~/$REMOTE_DIR && { [ -x .venv/bin/python ] || $PYTHON -m venv .venv; } \
  && .venv/bin/pip install -q --disable-pip-version-check -r requirements.txt"

echo "==> Installing cron job"
ssh "$HOST" "{ crontab -l 2>/dev/null | grep -vF '$CRON_TAG'; echo '$CRON_LINE'; } | crontab -"
ssh "$HOST" "crontab -l | grep -F '$CRON_TAG'"

echo "==> Done. Status: ssh $HOST 'cd $REMOTE_DIR && .venv/bin/python -m price_monitor --status'"
echo "    Logs:   ssh $HOST tail -f $LOG"
