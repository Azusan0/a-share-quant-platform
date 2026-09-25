#!/usr/bin/env bash
set -u

LOCK=/run/a-share-shadow.lock
PYTHON=/usr/local/lib/hermes-agent/venv/bin/python
SCRIPT=/usr/local/lib/hermes-agent/a_share_alert_template/shadow_scanner.py
DYNAMIC=/usr/local/lib/hermes-agent/a_share_alert_template/dynamic_pool.py
SENTIMENT=/usr/local/lib/hermes-agent/a_share_alert_template/market_sentiment.py
INTRADAY=/usr/local/lib/hermes-agent/a_share_alert_template/intraday_pipeline.py
RECOMMEND=/usr/local/lib/hermes-agent/a_share_alert_template/recommendation_engine.py
FUNDAMENTAL=/usr/local/lib/hermes-agent/a_share_alert_template/fundamental_data.py
BOARD_STRENGTH=/usr/local/lib/hermes-agent/a_share_alert_template/board_strength.py
CONFIG=/root/.hermes/scripts/a_share_alert_runtime_config.json
OUTPUT=/root/.hermes/scripts/a_share_shadow_snapshot.json
RECOMMEND_OUTPUT=/root/.hermes/scripts/a_share_recommendation_snapshot.json
FUNDAMENTAL_OUTPUT=/root/.hermes/scripts/a_share_fundamental_snapshot.json
BOARD_STRENGTH_OUTPUT=/root/.hermes/scripts/a_share_board_strength_snapshot.json
PIPELINE_STATUS=/usr/local/lib/hermes-agent/a_share_alert_template/pipeline_status.py
MANIFEST=/root/.hermes/scripts/a_share_pipeline_manifest.json
LOG_DIR=/root/.hermes/scripts/pipeline_logs

run_stage() {
  local name="$1" critical="$2" timeout_s="$3"
  shift 3
  local started finished rc status error
  started=$(date +%s)
  if timeout --signal=TERM --kill-after=5s "$timeout_s" "$@" >"$LOG_DIR/$name.log" 2>&1; then
    rc=0
    status=ok
  else
    rc=$?
    status=failed
  fi
  finished=$(date +%s)
  error=""
  if [[ "$status" != "ok" ]]; then
    error=$(tail -n 8 "$LOG_DIR/$name.log" | tr '\n' ' ' | cut -c1-800)
    echo "stage $name failed rc=$rc: $error" >&2
  fi
  if [[ "$critical" == "critical" ]]; then
    "$PYTHON" "$PIPELINE_STATUS" --manifest "$MANIFEST" stage "$name" "$status" "$((finished-started))" --critical --error "$error"
  else
    "$PYTHON" "$PIPELINE_STATUS" --manifest "$MANIFEST" stage "$name" "$status" "$((finished-started))" --error "$error"
  fi
  [[ "$critical" == "critical" && "$status" != "ok" ]] && return "$rc"
  return 0
}

(
  flock -n 9 || exit 0
  mkdir -p "$LOG_DIR"
  RUN_ID="$(date +%Y%m%dT%H%M%S)-$$"
  "$PYTHON" "$PIPELINE_STATUS" --manifest "$MANIFEST" start "$RUN_ID"
  run_stage sentiment optional 20 "$PYTHON" "$SENTIMENT" --output /root/.hermes/scripts/a_share_market_sentiment.json || true
  run_stage dynamic_pool critical 80 "$PYTHON" "$DYNAMIC" --config "$CONFIG" --output /root/.hermes/scripts/a_share_dynamic_pool.json || {
    "$PYTHON" "$PIPELINE_STATUS" --manifest "$MANIFEST" finish
    exit 1
  }
  run_stage fundamental optional 180 "$PYTHON" "$FUNDAMENTAL" --pool /root/.hermes/scripts/a_share_dynamic_pool.json \
    --output "$FUNDAMENTAL_OUTPUT" --db /root/.hermes/scripts/a_share_market_snapshots.db || true
  run_stage intraday critical 45 "$PYTHON" "$INTRADAY" --pool /root/.hermes/scripts/a_share_dynamic_pool.json \
    --db /root/.hermes/scripts/a_share_market_snapshots.db || {
    "$PYTHON" "$PIPELINE_STATUS" --manifest "$MANIFEST" finish
    exit 1
  }
  run_stage shadow critical 100 "$PYTHON" "$SCRIPT" --config "$CONFIG" --output "$OUTPUT" || {
    "$PYTHON" "$PIPELINE_STATUS" --manifest "$MANIFEST" finish
    exit 1
  }
  run_stage board_strength optional 90 "$PYTHON" "$BOARD_STRENGTH" --pool /root/.hermes/scripts/a_share_dynamic_pool.json \
    --shadow "$OUTPUT" --output "$BOARD_STRENGTH_OUTPUT" --db /root/.hermes/scripts/a_share_market_snapshots.db || true
  run_stage recommendation critical 20 "$PYTHON" "$RECOMMEND" --shadow "$OUTPUT" \
    --pool /root/.hermes/scripts/a_share_dynamic_pool.json \
    --states /root/.hermes/scripts/a_share_market_snapshots.db \
    --fundamental "$FUNDAMENTAL_OUTPUT" \
    --board-strength "$BOARD_STRENGTH_OUTPUT" \
    --config "$CONFIG" \
    --output "$RECOMMEND_OUTPUT" || {
    "$PYTHON" "$PIPELINE_STATUS" --manifest "$MANIFEST" finish
    exit 1
  }
  "$PYTHON" "$PIPELINE_STATUS" --manifest "$MANIFEST" finish
) 9>"$LOCK"
exit 0
