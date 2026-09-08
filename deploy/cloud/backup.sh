#!/bin/sh
# Nightly backup of the hosted oddsrail state: a consistent sqlite copy plus
# the ledgers, kept 14 days under /var/backups/oddsrail-cloud. Restore =
# stop the service, untar into /var/lib/oddsrail-cloud, start it.
set -eu
SRC=/var/lib/oddsrail-cloud
DST=/var/backups/oddsrail-cloud
STAMP=$(date -u +%Y-%m-%dT%H%M)
TMP=$(mktemp -d)
mkdir -p "$DST"
python3 - "$SRC/cloud.sqlite3" "$TMP/cloud.sqlite3" <<'PY'
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()
PY
cp -r "$SRC/ledgers" "$TMP/ledgers" 2>/dev/null || true
cp -r "$SRC/guests" "$TMP/guests" 2>/dev/null || true
tar czf "$DST/$STAMP.tgz" -C "$TMP" .
rm -rf "$TMP"
find "$DST" -name '*.tgz' -mtime +14 -delete
echo "backup written: $DST/$STAMP.tgz ($(du -h "$DST/$STAMP.tgz" | cut -f1))"
