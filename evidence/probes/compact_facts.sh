#!/bin/bash
# Compact the facts table of a DISPOSABLE database and report its size.
#   evidence/probes/compact_facts.sh <database>
# The first batch of a new week rewrites every row of sales (a delete and an
# ordered insert, app/data/ingest.py _shift_offsets); the old half is left as
# free space, so the heap and its indexes double. VACUUM FULL returns them to
# their size. It holds ACCESS EXCLUSIVE on sales for its whole run: every
# reader waits, so its duration is the outage it causes. Refuses the working
# database. caffeinate keeps the machine awake for the timing.
set -euo pipefail
db=${1:?database}
case "$db" in
  pharma_analytics) echo "refusing the working database" >&2; exit 2 ;;
esac
size() {
  psql -X -At -d "$db" -c "SELECT pg_size_pretty(pg_relation_size('sales')) || ' heap, ' || pg_size_pretty(pg_indexes_size('sales')) || ' indexes'"
}
echo "before: $(size)"
start=$(date +%s)
caffeinate -dims psql -X -q -v ON_ERROR_STOP=1 -d "$db" -c 'VACUUM (FULL, ANALYZE) sales'
echo "vacuum_full_seconds: $(( $(date +%s) - start ))"
echo "after: $(size)"
