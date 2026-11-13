#!/usr/bin/env bash
# Applies db/migrations/*.sql in order to the database in $DATABASE_URL.
#
#   DATABASE_URL=postgresql://cowin:cowin@localhost:5432/cowin ./db/scripts/apply_migrations.sh
#   ./db/scripts/apply_migrations.sh --reset      # drop and recreate the public schema first
#
# Number of hash partitions (default 8) is read from the cowin.partitions setting:
#   PGOPTIONS='-c cowin.partitions=16' ./db/scripts/apply_migrations.sh --reset
set -euo pipefail

DATABASE_URL="${DATABASE_URL:-postgresql://cowin:cowin@localhost:5432/cowin}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIGRATIONS="$HERE/../migrations"

if [[ "${1:-}" == "--reset" ]]; then
  echo "Resetting schema 'public' in $DATABASE_URL"
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q \
       -c "SET client_min_messages = warning; DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;"
fi

for f in "$MIGRATIONS"/*.sql; do
  echo "Applying $(basename "$f")"
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -o /dev/null -f "$f"
done
echo "Done."
