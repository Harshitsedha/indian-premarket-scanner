"""
Start the API server on port 8001, test all 5 endpoints, then stop.
Writes results to scratch/api_test_result.txt.
"""
import subprocess, sys, time, os
from pathlib import Path

# Force UTF-8 output so Unicode in JSON responses doesn't crash print()
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Ensure pipeline/ is on path
PIPELINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE))

from dotenv import load_dotenv
load_dotenv(PIPELINE.parent / ".env")

import httpx

OUT = Path(__file__).parent / "api_test_result.txt"
lines = []

def log(msg=""):
    print(msg)
    lines.append(str(msg))

def save():
    OUT.write_text("\n".join(lines))

# Start server
log("Starting API server on port 8001...")
proc = subprocess.Popen(
    [sys.executable, str(PIPELINE / "api" / "run.py")],
    cwd=str(PIPELINE),
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)

# Wait for startup with retry
base = "http://localhost:8001"
for attempt in range(20):
    time.sleep(0.5)
    try:
        r = httpx.get(f"{base}/api/health", timeout=2)
        if r.status_code == 200:
            log(f"Server up after {(attempt+1)*0.5:.1f}s")
            break
    except Exception:
        pass
else:
    log("ERROR: server did not start within 10s")
    proc.terminate()
    save()
    sys.exit(1)

# Test all 5 endpoints
PATHS = [
    "/api/health",
    "/api/briefing/today",
    "/api/briefing/history?days=7",
    "/api/setups/2026-06-01",
    "/api/edge/summary?days=90",
]

log()
log("=" * 70)
log("ENDPOINT TESTS")
log("=" * 70)

import json
for path in PATHS:
    try:
        r = httpx.get(base + path, timeout=10)
        body = r.text
        try:
            parsed = r.json()
            pretty = json.dumps(parsed, indent=2)[:600]
        except Exception:
            pretty = body[:600]
        log()
        log(f"{'─'*60}")
        log(f"{r.status_code}  {path}")
        log(pretty)
    except Exception as e:
        log(f"FAIL {path}: {e}")

log()
log("ALL DONE")
proc.terminate()
save()
