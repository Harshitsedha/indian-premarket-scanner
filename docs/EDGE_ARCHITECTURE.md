# Edge Page — Architecture (Phase 0 data spine)

The Edge page is built on a durable, dense store of intraday radar data. Phase 0 is the
storage spine only — nothing user-visible — but every later phase (event labeling, slice
explorer, edge-decay alerts, journal funnel) depends on it being correct, durable, and
reversible. This document records the **five locked architectural decisions** and why
they were made, as built.

## Data flow (as built)

```
radar_poller.py  ──(live snapshot, unchanged)──▶  Redis key radar:snapshot ──▶ /radar UI
       │
       └──(fire-and-forget XADD, one entry per 45s cycle)──▶  Redis Stream radar:frames
                                                                      │  (consumer group "persist")
                                                                      ▼
                                                            persist_worker.py
                                                              · XREADGROUP / XAUTOCLAIM
                                                              · batch INSERT … ON CONFLICT
                                                              · XACK only after COMMIT
                                                                      │
                                                                      ▼
                                                   TimescaleDB  radar_snapshots (hypertable)
                                                   + radar_events / event_labels / daily_regime (empty, Phase 1)
```

The poller and worker both run **outside Docker**, connecting to Redis and Postgres over
`localhost` + host-mapped ports (never the compose service names). See
[PORTABILITY.md](PORTABILITY.md) for the export/escape-hatch contract and
[EDGE_PHASE0_RUNBOOK.md](EDGE_PHASE0_RUNBOOK.md) for the Timescale-enablement procedure.

---

## Decision 1 — Separate persist worker (full fault isolation)

**Decision.** The radar poller does **not** write to Postgres. Each 45 s frame is published
to the Redis Stream `radar:frames` with a single fire-and-forget `XADD`; a dedicated,
independently-restartable process (`persist_worker.py`) consumes the stream and
batch-inserts into TimescaleDB.

**Rationale.** Live serving is sacred. The /radar UI reads only `radar:snapshot` from
Redis; persistence must never be able to slow, block, or break the poll loop. By making
the poller's only new responsibility one `XADD` wrapped in try/except, persistence
failures (DB down, slow disk, bad migration) are completely isolated from live serving —
the poller keeps publishing from Redis regardless. The worker can crash, be restarted, or
be deployed independently without touching the poller.

**As built / proven.** Proof 1 stopped Postgres for ~4 min: the poller kept publishing,
`radar:snapshot` kept refreshing, frames buffered in the stream, and the worker backed off
— zero impact on live serving, zero data loss, gap-free drain on recovery. The poller has
**zero Postgres dependency** in its persistence path.

---

## Decision 2 — Dense storage (no downsampling, ever)

**Decision.** Store every frame, every symbol, every 45 s — ~199 rows per cycle,
~100 k rows/day. `radar_snapshots` keeps all 16 per-symbol metrics the poller computes
(price, prev_close, open, high, low, volume, gap_pct, change_pct, change_from_open_pct,
rvol, range_used_pct, atr_mult, index_membership, has_news, catalyst_line,
headline_count), not just a thin subset.

**Rationale.** The whole point of the Edge page is post-hoc analysis — forward-return
labeling, slice exploration, edge-decay detection — none of which can be done on data that
was discarded or averaged away at write time. "Dense" applies to columns too: any metric
not trivially recomputable from price is stored, because re-deriving it later from raw
quotes may be impossible (baselines change, intraday context is lost). Storage is cheap;
lost signal is not.

**As built / proven.** Phase 5 confirmed 199 rows per frame landing every ~45 s, full
universe, no downsampling.

---

## Decision 3 — TimescaleDB with a plain-Postgres portability guarantee

**Decision.** TimescaleDB is the engine, but the schema stays **plain-Postgres-portable**.
All tables are defined in standard ANSI/Postgres SQL with standard types only. Every
Timescale-specific object (hypertable conversion, compression policy, continuous
aggregate) lives in a separate, clearly-marked, skippable migration step. No application
logic may depend on a Timescale-only object existing.

**Rationale.** Timescale gives us time-partitioning, compression, and continuous
aggregates for free, but we never want to be locked in. By keeping the table definitions
vanilla and quarantining the Timescale calls behind extension-guards, a future migration
to plain Postgres or another store is a **data export, not a rewrite** — `pg_dump` emits
ordinary rows. The continuous aggregate is an optimization layer only; the same hourly
rollup is a plain `GROUP BY date_trunc(...)` query against the raw table.

**As built / proven.** `013_up.sql` creates the four tables in plain SQL; the
hypertable + compression sit in a guarded `DO` block that self-skips when the extension is
absent; the continuous aggregate is split into `013b_radar_cagg.sql` (it cannot be created
inside a transaction). Migration up/down was proven clean on a throwaway DB. The full
export/recompute contract is documented in [PORTABILITY.md](PORTABILITY.md).

---

## Decision 4 — Keep everything (compression yes, deletion no)

**Decision.** Data is kept **indefinitely**. Compression is enabled (chunks older than
7 days, `segmentby => symbol`, `orderby => ts`); **no retention or drop policy exists on
any table.**

**Rationale.** Edge research is longitudinal — detecting that a setup's edge is decaying
requires months or years of history, so deletion would destroy the very signal the page
exists to surface. Compression bounds storage cost without losing a single row;
columnar compression segmented by symbol is highly effective on this dense, repetitive
time-series. Deletion is the one optimization we explicitly forbid.

**As built / proven.** A `policy_compression` job compresses chunks >7 days; no retention
job exists in `timescaledb_information.jobs`. Compressed chunks remain fully exportable
after `decompress_chunk` (see PORTABILITY.md).

---

## Decision 5 — 1-day chunk interval on the snapshots hypertable

**Decision.** `radar_snapshots` is a hypertable partitioned on `ts` with
`chunk_time_interval => INTERVAL '1 day'`.

**Rationale.** At ~100 k rows/day, a 1-day chunk is a few MB — large enough to avoid chunk
proliferation and planning overhead, small enough that the compression boundary (>7 days)
maps cleanly to whole chunks and that time-bounded queries (a day, a session, a slice)
prune to a handful of chunks. It also aligns naturally with a trading day, the unit most
Edge queries are scoped to.

**As built / proven.** `timescaledb_information.dimensions` reports
`time_interval = 1 day` for `radar_snapshots`.

---

## Durability & idempotency model (how Decision 1 stays safe)

The separation in Decision 1 is only safe if the stream transport never loses or
duplicates data. The guarantees:

- **At-least-once delivery + idempotent writes.** The worker reads via a consumer group
  and `XACK`s an entry **only after** the DB `COMMIT`. A crash before ack redelivers the
  frame; the unique key `(ts, symbol)` + `INSERT … ON CONFLICT DO NOTHING` makes the
  redelivery a no-op. Result: no loss, no duplicates (Proof 1 + Proof 2).
- **Crash recovery.** `XAUTOCLAIM` reclaims pending entries left by a dead consumer
  (e.g. a previous PID after a restart); stale per-PID consumers are reaped on startup.
- **Backpressure, not loss.** On DB unavailability the worker backs off (capped
  exponential) and reads/acks nothing — frames accumulate in the stream, which is capped
  at `MAXLEN ~ 10000` (≈20 trading sessions of buffer, ~500 MB worst case) and drained in
  order when the DB returns.
- **One entry per cycle.** The whole ~199-symbol frame is a single JSON stream entry, not
  199 entries — keeping the stream small and the insert a single batch.

## Operational lessons baked into the design

These reflect real incidents on this system and are load-bearing, not optional:

- **`localhost` only.** Poller and worker run outside Docker; all connections use
  `localhost`/`127.0.0.1` + host-mapped ports, never the compose service names
  `redis`/`postgres`. (This hostname class of bug has bitten before.)
- **No stale code after deploy.** The worker is registered in the GitHub Actions deploy
  and restarted on every deploy (hard-fail if the `systemctl restart` is denied), with a
  matching `/etc/sudoers.d` grant. No module-level mutable state survives a restart.
- **Blocking-read timeout is normal, not an error.** redis-py 8.0 defaults
  `socket_timeout` to 5 s; a blocking `XREADGROUP BLOCK 5000` on an empty stream must use
  `socket_timeout > block` and treat a genuine timeout as a no-data tick, not a failure
  (otherwise an idle stream thrashes). Backoff is reserved for real `ConnectionError`s.
- **Reversible enablement.** The Timescale image swap reuses the existing volume after a
  `pg_dump` backup; the migration is fully reversible (`013_down.sql`), deadlock-free, and
  drops only the new spine objects.

## Phase boundaries

Phase 0 (this document) is the spine only: `radar_snapshots` wired and persisting;
`radar_events`, `event_labels`, `daily_regime` created empty with `event_id` as the
identity that will later thread radar → setup → outcome → journal trade. Event emission,
forward-return labeling, the `/edge` API, slice explorer, edge-decay monitor, and journal
funnel are Phases 1–6.
