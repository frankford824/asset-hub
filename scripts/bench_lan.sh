#!/usr/bin/env bash
# 局域网热路径压测：健康检查 / 检索 P95 / 单文件下载吞吐 / 可选打包 E2E
set -euo pipefail

BASE_URL="${ASSET_HUB_BASE_URL:-http://127.0.0.1}"
SEARCH_N="${SEARCH_N:-200}"
SKU="${BENCH_SKU:-HQT10000}"
OUT_DIR="${BENCH_OUT:-/tmp/asset-hub-bench}"
mkdir -p "${OUT_DIR}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing $1"; exit 1; }; }
need curl
need python3

echo "== health =="
curl -fsS "${BASE_URL}/health" | tee "${OUT_DIR}/health.json"
echo

echo "== status =="
curl -fsS "${BASE_URL}/api/v1/status" | tee "${OUT_DIR}/status.json"
echo

echo "== search latency (${SEARCH_N}x q=${SKU}) =="
python3 - <<PY
import json, statistics, time, urllib.parse, urllib.request
base = ${BASE_URL@Q}
sku = ${SKU@Q}
n = int(${SEARCH_N@Q})
url = f"{base}/api/v1/search?" + urllib.parse.urlencode({"q": sku, "kind": "finalized", "limit": 20})
samples = []
for i in range(n):
    t0 = time.perf_counter()
    with urllib.request.urlopen(url, timeout=30) as r:
        r.read()
    samples.append((time.perf_counter() - t0) * 1000)
samples.sort()
p50 = samples[len(samples)//2]
p95 = samples[int(len(samples)*0.95)-1]
p99 = samples[min(len(samples)-1, int(len(samples)*0.99))]
print(json.dumps({"n": n, "p50_ms": round(p50,2), "p95_ms": round(p95,2), "p99_ms": round(p99,2), "max_ms": round(samples[-1],2)}, ensure_ascii=False))
open("${OUT_DIR}/search.json","w").write(json.dumps({"p50_ms":p50,"p95_ms":p95,"p99_ms":p99}, indent=2))
PY

echo "== pick asset for download =="
ASSET_ID="$(python3 - <<PY
import json, urllib.parse, urllib.request
base = ${BASE_URL@Q}
sku = ${SKU@Q}
url = f"{base}/api/v1/search?" + urllib.parse.urlencode({"q": sku, "kind": "finalized", "limit": 1})
with urllib.request.urlopen(url, timeout=30) as r:
    data = json.load(r)
rows = data.get("results") or []
if not rows:
    raise SystemExit("no search hit for download bench")
print(rows[0]["asset_id"])
PY
)"
echo "asset_id=${ASSET_ID}"

echo "== single-file download throughput =="
DL="${OUT_DIR}/sample.bin"
T0=$(date +%s.%N)
curl -fsS -o "${DL}" "${BASE_URL}/api/v1/assets/${ASSET_ID}/download"
T1=$(date +%s.%N)
python3 - <<PY
import os, json
path = ${DL@Q}
t0 = float(${T0@Q}); t1 = float(${T1@Q})
size = os.path.getsize(path)
elapsed = max(t1 - t0, 1e-6)
mbps = size / elapsed / (1024*1024)
print(json.dumps({"bytes": size, "sec": round(elapsed,4), "MBps": round(mbps,2)}, ensure_ascii=False))
open(${OUT_DIR@Q}+"/download.json","w").write(json.dumps({"bytes":size,"sec":elapsed,"MBps":mbps}, indent=2))
PY

if [[ -n "${BENCH_EXCEL:-}" && -f "${BENCH_EXCEL}" ]]; then
  echo "== pack E2E (${BENCH_EXCEL}) =="
  JOB_JSON="$(curl -fsS -F "file=@${BENCH_EXCEL}" -F "super_dir_name=bench" "${BASE_URL}/api/v1/jobs")"
  echo "${JOB_JSON}" | tee "${OUT_DIR}/job_create.json"
  JOB_ID="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])' <<<"${JOB_JSON}")"
  for _ in $(seq 1 120); do
    ST="$(curl -fsS "${BASE_URL}/api/v1/jobs/${JOB_ID}")"
    STATUS="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])' <<<"${ST}")"
    echo "status=${STATUS}"
    if [[ "${STATUS}" == "done" || "${STATUS}" == "failed" ]]; then
      echo "${ST}" | tee "${OUT_DIR}/job_final.json"
      break
    fi
    sleep 0.5
  done
  if [[ "${STATUS}" == "done" ]]; then
    curl -fsS -o "${OUT_DIR}/result.zip" "${BASE_URL}/api/v1/jobs/${JOB_ID}/download"
    ls -lh "${OUT_DIR}/result.zip"
  fi
else
  echo "== pack E2E skipped (set BENCH_EXCEL=/path/to.xlsx) =="
fi

echo "bench artifacts in ${OUT_DIR}"
