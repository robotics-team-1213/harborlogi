#!/usr/bin/env bash
# Nightly one-Fleet full first flow (D-927 E): newest Harbor and platform mains, report JSON names both SHAs
# and the first failing stage. Usage: nightly_full_flow.sh <work-dir> [harbor-ref] [platform-ref]
set -euo pipefail
WORK=${1:?work dir}
HARBOR_REF=${2:-main}
PLATFORM_REF=${3:-main}
HARBOR_URL=${HARBOR_URL:-https://github.com/kernel-robot-8th/kernel-robot-repo-2}
PLATFORM_URL=${PLATFORM_URL:-https://github.com/robotics-team-1213/rosy-platform}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RUN=$WORK/run-$STAMP
mkdir -p "$WORK/reports" "$RUN"
trap 'rm -rf "$RUN"' EXIT

fetch() {  # shallow, clean checkout of one ref
  git init -q "$2"
  git -C "$2" -c core.autocrlf=false fetch -q --depth 1 "$1" "$3"
  git -C "$2" -c core.autocrlf=false checkout -q --detach FETCH_HEAD
}
fetch "$HARBOR_URL" "$RUN/harbor" "$HARBOR_REF"
fetch "$PLATFORM_URL" "$RUN/platform" "$PLATFORM_REF"
SHA=$(git -C "$RUN/platform" rev-parse HEAD)

# One venv per platform dependency set, reused across nights.
if [ ! -x "$WORK/venv/bin/python" ]; then
  python3 -m venv --without-pip "$WORK/venv"
  /usr/bin/python3 -m pip -q --python "$WORK/venv/bin/python" install httpx uvicorn pyyaml pydantic fastapi cryptography websockets numpy
fi

REPORT=$WORK/reports/$STAMP.json
cd "$RUN/harbor/harborlogi"
status=0
nice -n 15 timeout 1800 "$WORK/venv/bin/python" -m tools.integration.real_fleet_http \
  --platform-root "$RUN/platform" --platform-sha "$SHA" --full-flow --report "$REPORT" > "$WORK/reports/$STAMP.log" 2>&1 || status=$?
echo "harbor $(git -C "$RUN/harbor" rev-parse HEAD) platform $SHA exit $status report $REPORT"
# The same flow through integrated control R (D-965); either failure fails the night.
via_r=0
nice -n 15 timeout 1800 "$WORK/venv/bin/python" -m tools.integration.real_fleet_http \
  --platform-root "$RUN/platform" --platform-sha "$SHA" --full-flow --via-r --report "$WORK/reports/$STAMP-via-r.json" \
  > "$WORK/reports/$STAMP-via-r.log" 2>&1 || via_r=$?
echo "via-r exit $via_r report $WORK/reports/$STAMP-via-r.json"
[ "$via_r" = 0 ] || status=$via_r
# Connection rules (docs/platform-contract.md 「연결 규약」) against the same platform.
rules=0
nice -n 15 timeout 900 "$WORK/venv/bin/python" -m tools.integration.real_fleet_http \
  --platform-root "$RUN/platform" --platform-sha "$SHA" --transport-only > "$WORK/reports/$STAMP-rules.log" 2>&1 || rules=$?
echo "connection rules exit $rules log $WORK/reports/$STAMP-rules.log"
[ "$rules" = 0 ] || status=$rules
# Keep two weeks of reports.
find "$WORK/reports" -type f -mtime +14 -delete
exit "$status"
