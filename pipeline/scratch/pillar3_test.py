"""
One-shot script: run migration 004, execute end-to-end test, verify DB.
Writes all output to scratch/pillar3_result.txt so it survives background runs.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

import psycopg2
import psycopg2.extras
from datetime import date
from utils.config import settings

OUT = Path(__file__).parent / "pillar3_result.txt"
lines = []
def log(msg=""):
    print(msg)
    lines.append(str(msg))

def save():
    OUT.write_text("\n".join(lines))

# ── 1. Run migration ──────────────────────────────────────────────────────────
log("=" * 60)
log("STEP 1: Running migration 004")
log("=" * 60)

migration_sql = (Path(__file__).parents[2] / "pipeline" / "storage" / "migrations" / "004_edge_tracking.sql").read_text()

conn = psycopg2.connect(
    host=settings.postgres_host,
    port=settings.postgres_port,
    dbname=settings.postgres_db,
    user=settings.postgres_user,
    password=settings.postgres_password,
)
with conn:
    with conn.cursor() as cur:
        cur.execute(migration_sql)
log("Migration OK")

# ── 2. Verify tables ──────────────────────────────────────────────────────────
log()
log("STEP 2: Table verification (\\dt)")
with conn.cursor() as cur:
    cur.execute("""
        SELECT tablename FROM pg_tables
        WHERE schemaname = 'public'
        ORDER BY tablename
    """)
    tables = [r[0] for r in cur.fetchall()]
log("Tables: " + ", ".join(tables))

for tname in ("setups", "outcomes"):
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = '{tname}'
            ORDER BY ordinal_position
        """)
        cols = cur.fetchall()
    log(f"\n{tname}:")
    for col, dtype in cols:
        log(f"  {col:<25} {dtype}")

conn.close()

# ── 3. End-to-end test ────────────────────────────────────────────────────────
log()
log("=" * 60)
log("STEP 3: log_setups() end-to-end test")
log("=" * 60)

from processing.setup_logger import log_setups

dummy_ranked = [
    {
        "rank": 1, "symbol": "RELIANCE", "score": 0.6200,
        "sentiment": "bullish", "setup_type": "news_catalyst",
        "thesis": "2 headlines mention Reliance; Strong Q4 earnings beat",
        "signals": {"news_mention": 0.6667, "importance": 0.8, "gap_potential": 0.3,
                    "fii_alignment": 1.0, "gap_pct": 0.9, "gap_source": "proxy"},
    },
    {
        "rank": 2, "symbol": "INFY", "score": 0.4900,
        "sentiment": "bearish", "setup_type": "news_catalyst",
        "thesis": "1 headline mentions Infosys; guidance cut",
        "signals": {"news_mention": 0.3333, "importance": 0.8, "gap_potential": 0.3,
                    "fii_alignment": 0.0, "gap_pct": -1.2, "gap_source": "proxy"},
    },
]
dummy_claude = {
    "overall_bias": {"direction": "bearish", "confidence": 3, "reason": "FII outflows dominate"},
    "headlines": [],
}

ids = log_setups(dummy_ranked, dummy_claude, trading_date=date.today())
log(f"Inserted setup IDs: {ids}")

# ── 4. DB verification ────────────────────────────────────────────────────────
log()
log("STEP 4: DB verification query")
conn2 = psycopg2.connect(
    host=settings.postgres_host,
    port=settings.postgres_port,
    dbname=settings.postgres_db,
    user=settings.postgres_user,
    password=settings.postgres_password,
)
with conn2.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
    cur.execute("""
        SELECT id, trading_date, symbol, setup_type, hypothesis,
               bias_direction, bias_confidence, gap_pct, gap_source,
               score, thesis
        FROM setups ORDER BY id DESC LIMIT 5
    """)
    rows = cur.fetchall()
conn2.close()

log(f"setups rows (last 5):")
for r in rows:
    log(f"  id={r['id']}  {r['trading_date']}  {r['symbol']:<12} "
        f"{r['setup_type']:<15} {r['hypothesis']:<8} "
        f"bias={r['bias_direction']}({r['bias_confidence']}) "
        f"gap={r['gap_pct']} score={r['score']}")

log()
log("ALL DONE")
save()
