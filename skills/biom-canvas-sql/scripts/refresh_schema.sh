#!/usr/bin/env bash
# refresh_schema.sh — regenerate the structural inputs for schema_map.md
#
# The schema map in references/schema_map.md is a point-in-time snapshot and
# drifts as the warehouse changes. Run this to re-dump the live structure, then
# hand the two CSVs back to Claude (or re-run the generator) to rebuild
# schema_map.md. Nothing here is destructive — both queries are read-only
# INFORMATION_SCHEMA scans (near-zero cost).
#
# Usage:  bash refresh_schema.sh [dataset]     # default dataset: biom_canvas
set -euo pipefail

PROJECT="biom-reporting-s26"
DATASET="${1:-biom_canvas}"
OUT="${HOME}"

echo "Dumping structure for ${PROJECT}.${DATASET} ..."

bq query --nouse_legacy_sql --format=csv --max_rows=100000 --project_id="${PROJECT}" \
"SELECT table_name, ordinal_position, column_name, data_type, is_nullable
 FROM \`${PROJECT}.${DATASET}.INFORMATION_SCHEMA.COLUMNS\`
 ORDER BY table_name, ordinal_position" > "${OUT}/${DATASET}_columns.csv"

bq query --nouse_legacy_sql --format=csv --max_rows=100000 --project_id="${PROJECT}" \
"SELECT table_name, table_type, ddl
 FROM \`${PROJECT}.${DATASET}.INFORMATION_SCHEMA.TABLES\`
 ORDER BY table_name" > "${OUT}/${DATASET}_ddl.csv"

echo "Wrote:"
echo "  ${OUT}/${DATASET}_columns.csv"
echo "  ${OUT}/${DATASET}_ddl.csv"
echo "Upload both to Claude and ask it to rebuild references/schema_map.md."
echo "To map other layers too, re-run with a dataset arg, e.g.: bash refresh_schema.sh biom_core"
