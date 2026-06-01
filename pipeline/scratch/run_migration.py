import subprocess, sys, os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sql_file = os.path.join(ROOT, "pipeline", "storage", "migrations", "004_edge_tracking.sql")
out_file = os.path.join(ROOT, "pipeline", "scratch", "migration_result.txt")

with open(sql_file) as f:
    sql = f.read()

r = subprocess.run(
    ["docker", "exec", "-i", "indian_premarket_scanner-postgres-1",
     "psql", "-U", "premarket", "-d", "premarket"],
    input=sql, capture_output=True, text=True
)

r2 = subprocess.run(
    ["docker", "exec", "indian_premarket_scanner-postgres-1",
     "psql", "-U", "premarket", "-d", "premarket", "-c", r"\dt"],
    capture_output=True, text=True
)

lines = [
    f"MIGRATION STDOUT: {r.stdout.strip()}",
    f"MIGRATION STDERR: {r.stderr.strip()}",
    f"EXIT CODE: {r.returncode}",
    "",
    "TABLES:",
    r2.stdout,
    r2.stderr,
]
result = "\n".join(lines)
print(result)
with open(out_file, "w") as f:
    f.write(result)
