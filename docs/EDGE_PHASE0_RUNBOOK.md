# Edge Phase 0 — TimescaleDB enablement runbook (VPS, destructive)

Run these on the VPS over SSH, in order. **Do not skip a verification.** Any STOP point
means: halt, report, do not proceed. The container is `premarket-postgres-1`, db/user
`premarket` (compose project `premarket` at `/opt/premarket`).

This swaps the Postgres image to `timescale/timescaledb:2.17.2-pg16` **reusing the
existing `postgres_data` volume** (no `down -v`). Everything before step 4 is
non-destructive.

---

### Step 0 — Pre-pull the image and verify it is Alpine-based (no downtime yet)

Pull while the old container is still serving, so the swap window is seconds, not a
registry download.

```bash
docker pull timescale/timescaledb:2.17.2-pg16

# Verify it is the Alpine/musl variant (matches the old postgres:16-alpine libc).
# A reused volume + a libc change can corrupt index collations, so this is a hard gate.
docker run --rm --entrypoint cat timescale/timescaledb:2.17.2-pg16 /etc/os-release | grep -i '^ID='
```

**Expected:** `ID=alpine`.
**STOP** if it is not `alpine` (e.g. `debian`) — report back before swapping. A
glibc-based image on this musl-built volume risks collation/index corruption.

---

### Step 1 — Back up the database (BEFORE any change)

```bash
mkdir -p /opt/premarket/backups
TS=$(date +%Y%m%d_%H%M%S)
docker exec premarket-postgres-1 pg_dump -U premarket -d premarket \
  | gzip > /opt/premarket/backups/premarket_preTimescale_${TS}.sql.gz
ls -lh /opt/premarket/backups/premarket_preTimescale_${TS}.sql.gz
```

**STOP** if the file is missing or ~0 bytes.

---

### Step 2 — Record pre-swap row counts (fill this table in)

```bash
docker exec premarket-postgres-1 psql -U premarket -d premarket -c "
SELECT relname AS table, n_live_tup AS approx_rows
FROM pg_stat_user_tables ORDER BY relname;"

# Exact counts for the tables that MUST survive untouched:
docker exec premarket-postgres-1 psql -U premarket -d premarket -At -c "
SELECT 'setups',           count(*) FROM setups
UNION ALL SELECT 'outcomes',        count(*) FROM outcomes
UNION ALL SELECT 'radar_universe',  count(*) FROM radar_universe
UNION ALL SELECT 'radar_baselines', count(*) FROM radar_baselines
UNION ALL SELECT 'orb_range_defs',  count(*) FROM orb_range_defs
UNION ALL SELECT 'orb_history',     count(*) FROM orb_history
UNION ALL SELECT 'daily_briefings', count(*) FROM daily_briefings
UNION ALL SELECT 'headlines',       count(*) FROM headlines;"
```

| table | BEFORE | AFTER (step 6) |
|-------|-------:|---------------:|
| setups | | |
| outcomes | | |
| radar_universe | | |
| radar_baselines | | |
| orb_range_defs | | |
| orb_history | | |
| daily_briefings | | |
| headlines | | |

---

### Step 3 — Swap the image, then set `shared_preload_libraries`

The repo `docker-compose.yml` already pins `timescale/timescaledb:2.17.2-pg16`. Recreate
**only** the postgres service, keeping the volume:

```bash
cd /opt/premarket
git fetch origin main && git reset --hard origin/main   # bring in the pinned image + migrations
docker compose up -d postgres                            # recreates container, REUSES postgres_data
docker ps --filter name=premarket-postgres-1            # confirm it is up on the new image
docker inspect premarket-postgres-1 --format '{{.Config.Image}}'
```

**Expected image:** `timescale/timescaledb:2.17.2-pg16`.

The timescale image only auto-configures `shared_preload_libraries` on a **fresh** data
dir. We reused the volume, so set it explicitly, then restart:

```bash
docker exec premarket-postgres-1 sh -c \
  "grep -q timescaledb /var/lib/postgresql/data/postgresql.conf \
   || echo \"shared_preload_libraries = 'timescaledb'\" >> /var/lib/postgresql/data/postgresql.conf"
docker restart premarket-postgres-1

# VERIFY before going further:
docker exec premarket-postgres-1 psql -U premarket -d premarket -c "SHOW shared_preload_libraries;"
```

**Expected:** output includes `timescaledb`.
**STOP** if it does not — `CREATE EXTENSION` in step 4 will fail otherwise.

---

### Step 4 — Create the extension and verify existing data intact

```bash
docker exec premarket-postgres-1 psql -U premarket -d premarket -c \
  "CREATE EXTENSION IF NOT EXISTS timescaledb;"

docker exec premarket-postgres-1 psql -U premarket -d premarket -c "\dx"   # timescaledb listed
```

Re-run the **exact-count** query from step 2 and confirm every number matches the BEFORE
column. **STOP** and restore from the step-1 backup if any count differs.

---

### Step 5 — Apply the migrations

`013_up.sql` (tables + hypertable + compression), then `013b_radar_cagg.sql` (continuous
aggregate — **not** in a transaction; do not add `-1`).

```bash
cd /opt/premarket
docker exec -i premarket-postgres-1 psql -U premarket -d premarket \
  < pipeline/storage/migrations/013_up.sql

docker exec -i premarket-postgres-1 psql -U premarket -d premarket \
  < pipeline/storage/migrations/013b_radar_cagg.sql
```

Expect `NOTICE: TimescaleDB: radar_snapshots hypertable ... configured.` from the first,
no error from the second.

---

### Step 6 — Verify the spine objects + re-confirm row counts

```bash
docker exec premarket-postgres-1 psql -U premarket -d premarket -c "
SELECT hypertable_name FROM timescaledb_information.hypertables;
SELECT hypertable_name, proc_name FROM timescaledb_information.jobs
  WHERE proc_name LIKE '%compress%';
SELECT view_name FROM timescaledb_information.continuous_aggregates;
SELECT table_name FROM information_schema.tables
  WHERE table_name IN ('radar_snapshots','radar_events','event_labels','daily_regime')
  ORDER BY table_name;"
```

Re-run the step-2 exact-count query → fill the AFTER column → confirm it equals BEFORE.

---

### Step 7 — Install + start the persist worker, then deploy

```bash
sudo cp /opt/premarket/deploy/persist-worker.service /etc/systemd/system/persist-worker.service
# add the sudoers line below (one time) so the deploy can restart it, then:
sudo systemctl daemon-reload
sudo systemctl enable --now persist-worker
systemctl status persist-worker --no-pager
journalctl -u persist-worker -n 30 --no-pager
```

A normal first deploy (push to main) then restarts it via the deploy workflow.

---

### Rollback

If anything in steps 3–6 goes wrong: restore the volume from the step-1 dump into a
fresh `postgres:16-alpine` container, or apply `013_down.sql` to remove only the new
spine objects (existing tables are untouched by it).
