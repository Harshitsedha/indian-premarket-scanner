# Portability — Edge Data Spine (TimescaleDB ↔ plain Postgres)

The Edge page stores its data in TimescaleDB, but the schema is deliberately kept
**plain-Postgres-portable**. Migrating off Timescale (to vanilla Postgres or another
store) is a **data export, not a rewrite**. This document is the escape hatch.

## The rule

- All **tables** are defined in plain ANSI/Postgres SQL using standard types only
  (`timestamptz`, `text`, `double precision`, `bigint`, `integer`, `boolean`, `uuid`,
  `jsonb`, `date`). No Timescale-proprietary column types or features appear in any
  `CREATE TABLE`.
- Every **Timescale-specific object** lives in a clearly-marked, skippable step:
  - hypertable conversion of `radar_snapshots` (1-day chunks),
  - the compression policy (compress chunks > 7 days; `segmentby => symbol`,
    `orderby => ts`),
  - the hourly continuous aggregate `radar_snapshots_hourly` and its refresh policy.
- **No application logic may depend on any of those objects.** The continuous aggregate
  is an optimization layer only — every later phase must be able to compute the same
  rollup directly from the raw `radar_snapshots` rows. Nothing reads a Timescale-only
  catalog at request time.
- **No retention / drop policy exists on any table.** Data is kept indefinitely.
  Compression is the only space optimization; it never deletes rows.

## Where the Timescale coupling lives

| File | Plain Postgres? | Timescale-only |
|------|-----------------|----------------|
| `013_up.sql` | The four `CREATE TABLE`s run unchanged on vanilla PG. | A single guarded `DO` block (skipped when the `timescaledb` extension is absent) does the hypertable conversion + compression. |
| `013b_radar_cagg.sql` | — | Entire file is the continuous aggregate. **Must run outside a transaction.** On vanilla PG you simply do not apply it. |
| `013_down.sql` | Drops the tables on any PG. | Guarded block removes the cagg + compression policies first. |

Running `013_up.sql` on a Postgres **without** the timescaledb extension produces the
exact same tables and indexes — the `DO` block just `RAISE NOTICE`s and skips. Skip
`013b` entirely on vanilla Postgres.

## How to export / migrate off Timescale

`radar_snapshots` rows are ordinary Postgres rows; the only Timescale-coupled objects
are the hypertable conversion, the compression policy, and the continuous aggregate —
all droppable with **zero data loss**.

1. **Decompress first.** Compressed chunks cannot be read by a plain `pg_dump` as
   ordinary rows. Decompress every chunk, then remove the compression policy:
   ```sql
   SELECT remove_compression_policy('radar_snapshots', if_exists => TRUE);
   SELECT decompress_chunk(c, if_compressed => TRUE)
   FROM show_chunks('radar_snapshots') c;
   ```
2. **Drop the Timescale-only objects** (optional but makes the dump fully plain):
   ```sql
   DROP MATERIALIZED VIEW IF EXISTS radar_snapshots_hourly;   -- also drops its policy
   ```
   The hypertable itself dumps and restores as a normal table; you do not need to undo
   `create_hypertable` to export the data.
3. **Dump.** A standard `pg_dump` now emits `radar_snapshots` (and the other three
   tables) as plain `CREATE TABLE` + `COPY` data — restorable into any Postgres.
   ```bash
   pg_dump -U premarket -d premarket \
     -t radar_snapshots -t radar_events -t event_labels -t daily_regime \
     > edge_spine_export.sql
   ```
4. **Restore** into vanilla Postgres. Apply `013_up.sql` there first if you want the
   schema + indexes (the `DO` block self-skips), then `COPY` the data in; or just
   restore the dump directly.

## Recomputing the continuous aggregate from raw rows

If `radar_snapshots_hourly` does not exist (vanilla PG, or you dropped it), the same
rollup is a plain query — no Timescale required:

```sql
SELECT
    date_trunc('hour', ts) AS bucket,
    symbol,
    avg(rvol)       AS avg_rvol,
    avg(gap_pct)    AS avg_gap_pct,
    avg(change_pct) AS avg_change_pct,
    count(*)        AS sample_count
FROM radar_snapshots
GROUP BY bucket, symbol;
```

(`time_bucket('1 hour', ts)` in the cagg is equivalent to `date_trunc('hour', ts)`.)
