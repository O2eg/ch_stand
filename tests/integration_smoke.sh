#!/bin/sh
set -eu

PROFILE=${1:-single.yaml}
CLUSTER=${2:-ch_stand_single}
CH_STAND_BIN=${CH_STAND_BIN:-ch-stand}

PROJECT=$(mktemp -d)
CONFIG=$PROJECT/configs/$PROFILE

cleanup() {
  "$CH_STAND_BIN" -c "$CONFIG" down --clear-data --force >/dev/null 2>&1 || true
  if [ "${CH_STAND_KEEP_IMAGES:-0}" != "1" ]; then
    "$CH_STAND_BIN" -c "$CONFIG" cleanup run --image --credentials --force \
      >/dev/null 2>&1 || true
  fi
  rm -rf "$PROJECT"
}
trap cleanup EXIT INT TERM

"$CH_STAND_BIN" init --directory "$PROJECT"
cd "$PROJECT"
"$CH_STAND_BIN" -c "$CONFIG" validate
"$CH_STAND_BIN" -c "$CONFIG" up --timeout 360
"$CH_STAND_BIN" -c "$CONFIG" health
"$CH_STAND_BIN" -c "$CONFIG" sql "SELECT 1"
"$CH_STAND_BIN" -c "$CONFIG" cluster status

SSH_KEY=$PROJECT/.ch_stand/credentials/ssh/ch_stand_test
SSH_PORT=$("$CH_STAND_BIN" --json -c "$CONFIG" connection | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["ssh_port"])')
ssh -i "$SSH_KEY" -p "$SSH_PORT" \
  -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null root@127.0.0.1 \
  'perf version && clickhouse-client --version && strace --version'

if [ "$PROFILE" != "single.yaml" ]; then
  "$CH_STAND_BIN" -c "$CONFIG" keeper status
  "$CH_STAND_BIN" -c "$CONFIG" sql "
    CREATE DATABASE IF NOT EXISTS ch_stand_smoke ON CLUSTER $CLUSTER;
    CREATE TABLE IF NOT EXISTS ch_stand_smoke.events ON CLUSTER $CLUSTER
    (id UInt64, value String)
    ENGINE = ReplicatedMergeTree
    ORDER BY id;
    INSERT INTO ch_stand_smoke.events VALUES (1, 'replicated');
    SYSTEM SYNC REPLICA ch_stand_smoke.events;
  "
  COUNT=$("$CH_STAND_BIN" -c "$CONFIG" sql --node 2 \
    "SYSTEM SYNC REPLICA ch_stand_smoke.events; SELECT count() FROM ch_stand_smoke.events")
  test "$(printf '%s\n' "$COUNT" | tail -n 1)" = "1"
fi

"$CH_STAND_BIN" -c "$CONFIG" down --clear-data --force
