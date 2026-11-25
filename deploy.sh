#!/usr/bin/env bash
# Deploy the price monitor to the biolete.ro host and run it from cron.
#
#   ./deploy.sh            copy the code + .env, set up the venv, install the cron job
#   ./deploy.sh --remove   remove the cron job (leaves the files in place)
#   ./deploy.sh --resend   deploy, then clear the stored listings and email everything that
#                          matches now (python -m price_monitor --resend-all on the server)
#   ./deploy.sh --export   save the server's searches to searches.json here (a backup, gitignored:
#                          the repo is public; python -m price_monitor --import searches.json restores them)
#
# The cron job also exports the searches daily on the server, to backups/searches-DATE.json (the
# last 14 kept), for a bad edit or a broken upgrade.
#
# Re-run after changing the code or .env. Searches, intervals and on/off live in
# the database (edit them in the dashboard), so changing those needs no deploy.
# Server-only settings (MySQL credentials, cron schedule) live in .env.production and
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
CRON_SCHEDULE="$(grep -E '^CRON_SCHEDULE=' .env.production 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"'" || true)"
CRON_SCHEDULE="${CRON_SCHEDULE:-*/5 * * * *}"
[[ $(wc -w <<<"$CRON_SCHEDULE") -eq 5 ]] || { echo "CRON_SCHEDULE must have 5 fields, got: $CRON_SCHEDULE"; exit 1; }
# Daily at 04:17: the searches to backups/searches-YYYY-MM-DD.json, the last 14 kept (% is special in crontab)
BACKUP_LINE='17 4 * * * cd ~/'"$REMOTE_DIR"' && mkdir -p backups && .venv/bin/python -W ignore -m price_monitor --export backups/searches-$(date +\%F).json >> ~/'"$LOG"' 2>&1 && ls -1t backups/searches-*.json | tail -n +15 | xargs -r rm -- '"$CRON_TAG"' backup'
CRON_LINE="$CRON_SCHEDULE cd ~/$REMOTE_DIR && timeout 280 .venv/bin/python -W ignore -m price_monitor --once >> ~/$LOG 2>&1 $CRON_TAG"

if [[ "${1:-}" == "--export" ]]; then
  ssh "$HOST" "cd ~/$REMOTE_DIR && .venv/bin/python -W ignore -m price_monitor --export searches.export.json"
  scp -q "$HOST:$REMOTE_DIR/searches.export.json" searches.json
  ssh "$HOST" "rm -f ~/$REMOTE_DIR/searches.export.json"
  echo "Saved the server's searches to searches.json (gitignored; keep it, e.g. in your Mac's backups)."
  exit 0
fi

if [[ "${1:-}" == "--remove" ]]; then
  ssh "$HOST" "crontab -l 2>/dev/null | grep -vF '$CRON_TAG' | crontab -"
  echo "Cron job removed."
  exit 0
fi

[[ -f .env ]] || { echo "Missing .env (copy .env.example and fill it in)"; exit 1; }
[[ -f .env.production ]] || { echo "Missing .env.production (MySQL settings for the server)"; exit 1; }

echo "==> Copying files to $HOST:~/$REMOTE_DIR"
ssh "$HOST" "mkdir -p ~/$REMOTE_DIR/price_monitor/providers ~/logs"
# Remote .env = local .env without the keys .env.production sets, then .env.production
# (e.g. PLATFORMS=facebook locally, olx,vinted on the server)
SERVER_KEYS="$(grep -oE '^[A-Z_][A-Z0-9_]*=' .env.production | tr -d = | paste -sd'|' -)"
{ grep -vE "^(${SERVER_KEYS:-__none__})=" .env; echo; cat .env.production; } | ssh "$HOST" "umask 077 && cat > ~/$REMOTE_DIR/.env"
scp -q requirements.txt searches.example.json "$HOST:$REMOTE_DIR/"
scp -q price_monitor/*.py "$HOST:$REMOTE_DIR/price_monitor/"
scp -q price_monitor/providers/*.py "$HOST:$REMOTE_DIR/price_monitor/providers/"
# Drop modules that no longer exist locally (e.g. a removed provider)
ssh "$HOST" "cd ~/$REMOTE_DIR && for f in price_monitor/*.py price_monitor/providers/*.py; do echo \$f; done" \
  | while read -r f; do [[ -f "$f" ]] || ssh "$HOST" "rm -f ~/$REMOTE_DIR/$f" < /dev/null; done

echo "==> Setting up the virtualenv"
ssh "$HOST" "cd ~/$REMOTE_DIR && { [ -x .venv/bin/python ] || $PYTHON -m venv .venv; } \
  && .venv/bin/pip install -q --disable-pip-version-check -r requirements.txt"

echo "==> Installing cron jobs (the monitor, and the daily searches backup)"
ssh "$HOST" "{ crontab -l 2>/dev/null | grep -vF '$CRON_TAG'; echo '$CRON_LINE'; echo '$BACKUP_LINE'; } | crontab -"
ssh "$HOST" "crontab -l | grep -F '$CRON_TAG'"

if [[ "${1:-}" == "--resend" ]]; then
  echo "==> Clearing the stored listings and sending everything again"
  ssh "$HOST" "cd ~/$REMOTE_DIR && timeout 600 .venv/bin/python -W ignore -m price_monitor --resend-all 2>&1 | tee -a ~/$LOG"
fi

echo "==> Done. Status: ssh $HOST 'cd $REMOTE_DIR && .venv/bin/python -m price_monitor --status'"
echo "    Logs:   ssh $HOST tail -f $LOG"
