import sys
from pathlib import Path

# Ensure pipeline/ root is importable when run directly
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn

if __name__ == "__main__":
    uvicorn.run("api.main:app", host="0.0.0.0", port=8001, reload=True)
