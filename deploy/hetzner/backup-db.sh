#!/usr/bin/env bash
# Consistent online backup of the petition SQLite database (names + emails: personal data).
#
#   deploy/hetzner/backup-db.sh [backup-dir]       default: /var/backups/banthefake
#
# Uses SQLite's backup API inside the running app container (the image has no sqlite3 CLI), copies the
# snapshot out, compresses it, encrypts it with `age` when AGE_RECIPIENT is set, and keeps the newest
# KEEP (default 14) backups. Copy the result off the server (restic/rclone to a Storage Box or similar);
# a backup on the same disk does not survive losing the server.
#
# Cron (root), daily at 03:17:
#   17 3 * * * AGE_RECIPIENT=age1... /opt/banthefake/deploy/hetzner/backup-db.sh >>/var/log/btf-backup.log 2>&1
set -euo pipefail

cd "$(dirname "$0")"
dest=${1:-/var/backups/banthefake}
keep=${KEEP:-14}
stamp=$(date -u +%Y%m%dT%H%M%SZ)
tmp_in_container=/data/.backup-$stamp.db

install -d -m 700 "$dest"
umask 077

docker compose exec -T app python - "$tmp_in_container" <<'PY'
import sqlite3, sys
src = sqlite3.connect("file:/data/banthefake.db?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[1])
with dst:
    src.backup(dst)
ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
count = dst.execute("SELECT COUNT(*) FROM signatures").fetchone()[0]
dst.close(); src.close()
if ok != "ok":
    sys.exit(f"integrity_check failed: {ok}")
print(f"backup ok: {count} signatures")
PY

out="$dest/banthefake-$stamp.db"
docker compose cp "app:$tmp_in_container" "$out"
docker compose exec -T app rm -f "$tmp_in_container"
gzip -9 "$out"
out="$out.gz"

if [ -n "${AGE_RECIPIENT:-}" ]; then
    age -r "$AGE_RECIPIENT" -o "$out.age" "$out"
    rm -f "$out"
    out="$out.age"
fi
echo "wrote $out"

# Rotate: keep the newest $keep files.
ls -1t "$dest"/banthefake-*.db.gz* 2>/dev/null | tail -n +"$((keep + 1))" | xargs -r rm -f
