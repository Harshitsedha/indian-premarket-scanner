import subprocess, sys, time, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

import httpx

OUT = Path(__file__).parent / "server_check.txt"
lines = []
def log(msg=""): print(msg); lines.append(str(msg))
def save(): OUT.write_text("\n".join(lines))

# --- Start API server ---
PIPELINE = Path(__file__).resolve().parents[1]
api_proc = subprocess.Popen(
    [sys.executable, str(PIPELINE / "api" / "run.py")],
    cwd=str(PIPELINE), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
log("API server started (PID %d)" % api_proc.pid)

# --- Start Next.js dev server ---
DASHBOARD = PIPELINE.parent / "dashboard"
next_proc = subprocess.Popen(
    ["npm", "run", "dev"], cwd=str(DASHBOARD),
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=True,
)
log("Next.js server started (PID %d)" % next_proc.pid)

# --- Wait for both to come up ---
for name, url, timeout in [("API", "http://localhost:8001/api/health", 15),
                            ("Next.js", "http://localhost:3000/", 30)]:
    deadline = time.time() + timeout
    up = False
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=2)
            if r.status_code < 500:
                log(f"{name} UP: {r.status_code} {url}")
                up = True
                break
        except Exception:
            pass
        time.sleep(0.5)
    if not up:
        log(f"{name} did not start within {timeout}s")

log()
log("DONE - servers running. Kill manually when done testing.")
save()
api_proc.terminate()
next_proc.terminate()
