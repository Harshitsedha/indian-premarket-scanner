# Edge Page — Architecture (Phase 1: event emission + forward-return labeling)

Phase 1 turns the dense Phase 0 store into a stream of *labeled events*. It detects
threshold crossings on the same enriched radar rows the alert engine sees, emits a
first-crossing event per setup, and labels each event's forward return at T+5/15/30 and
EOD. Like Phase 0 it is back-end only — nothing user-visible yet — but the
`/edge` API, slice explorer, and edge-decay monitor (Phases 2+) all read what this phase
produces. This document records the **three locked decisions** for Phase 1 and why they
were made, as built, mirroring [EDGE_ARCHITECTURE.md](EDGE_ARCHITECTURE.md).

## Data flow (as built)

```
radar_poller.py
  · live snapshot (unchanged) ───────────────▶ Redis radar:snapshot ──▶ /radar UI
  · fire-and-forget XADD (one per 45s frame) ─▶ Redis Stream radar:frames ┐
  · fire-and-forget XADD (one per event) ─────▶ Redis Stream radar:events ┤
        │  first-crossing only; dedup via Redis setnx (own keyspace)      │
        ▼                                                                 │ (consumer
   process_events(): reuse _matches(), resolve+store direction            │  group
                                                                          ▼  "persist")
                                                            persist_worker.py
                                                              · ONE XREADGROUP over BOTH
                                                                streams; frames keep the
                                                                ordered backlog replay,
                                                                events use a light path
                                                              · INSERT … ON CONFLICT DO
                                                                NOTHING; XACK after COMMIT
                                                                      │
                                                                      ▼
                                              TimescaleDB  radar_snapshots  radar_events
                                                                                  │
                                          event_labeler.py (scheduler */5 + 15:40) │
                                            · nearest radar_snapshots.price ±50s   ▼
                                            · forward_return signed by direction   event_labels
                                            · MAE/MFE; EOD floor; NULL on gaps
```

The poller's only new responsibility is a second fire-and-forget `XADD` — it still has
**zero Postgres dependency** in any path the `/radar` UI depends on. See
[EDGE_PHASE0_RUNBOOK.md](EDGE_PHASE0_RUNBOOK.md) for the Timescale-enablement procedure;
Phase 1 adds no schema requiring it (migration 014 is plain ANSI/Postgres).

---

## Decision 1 — Events drain on a light path in the SAME worker

**Decision.** `radar:events` is a second Redis Stream consumed by the existing
`persist_worker.py` process, under the same consumer group, in a single `XREADGROUP` over
both streams. Events take a deliberately *lighter* path than frames: orphan recovery
(`XAUTOCLAIM`) still applies so nothing is lost, but events do **not** participate in the
ordered backlog replay (`check_backlog` / `last_id`) that frames use on restart.

**Rationale.** The frames path is the proven, load-bearing Phase 0 mechanism; the prime
directive is *do not regress it*. Spawning a second worker process would double the ops
surface (another systemd unit, another deploy restart, another failure to monitor) for a
stream that carries tens of entries a day versus ~199 rows every 45 s. Folding events into
the same process introduces **no new DB writer** — the worker that already owns every
Postgres write simply gains one more idempotent insert path. Events are order-independent
and each entry is a self-contained single-row insert, so they have no need for the ordered
replay frames require; keeping them off that code path means the frames replay logic is
extended, not forked, and stays byte-for-byte as Phase 0 left it. Idempotency comes from
`INSERT … ON CONFLICT DO NOTHING` (see Decision 2), so at-least-once redelivery is safe.

**As built / proven.** One `XREADGROUP` reads `{radar:frames: <cursor>, radar:events:
">"}`; the response is dispatched by stream name (never by position), the
backlog→live transition is keyed on the **frames** stream only, and `_claim_orphans` /
`_cleanup_stale_consumers` are parameterized over the stream so both get orphan recovery
with the frames call unchanged. The AC#5 proof (see "validated", below) stopped Postgres
mid-run: `radar:snapshot` kept serving from Redis, both streams buffered (11/11 entries,
nothing acked), and on recovery the worker drained **gap-free** — 30/30 snapshot rows
across 10/10 distinct frame timestamps and 10 unique events, with a duplicate event
dropped by `ON CONFLICT`.

---

## Decision 2 — First-crossing dedup, hardened by a DB unique index

**Decision.** An event fires **once** per `(symbol, event_type, trade_date)` — the first
crossing only. The poller claims the slot with a Redis `setnx` in the events' own keyspace
(`radar:event_emitted:{date}:{type}:{symbol}`), parallel to but never colliding with the
alert dedup. This is backed at the database by a **unique index** on
`radar_events (symbol, event_type, IST-day)`, and the worker inserts with a bare
`ON CONFLICT DO NOTHING` (no named target).

**Rationale.** A latched break (`broke_up`) re-qualifies on every subsequent poll, so the
poller must dedup or it would emit the same event each cycle — `setnx` does this and
survives poller restarts. But Redis alone is not durable enough to be the *only* guarantee:
a mid-session Redis flush would let the poller emit a second event (a fresh `event_id`) for
an already-crossed symbol. The DB unique index closes that hole. A bare `ON CONFLICT DO
NOTHING` is chosen deliberately over a named arbiter because **two** constraints must both
resolve to a no-op: the `event_id` primary key (a stream *redelivery* of the same event)
and the `(symbol, event_type, IST-day)` index (a *different* `event_id` for the same
crossing). Naming one arbiter would let the other raise mid-batch, leave the entry unacked,
and wedge redelivery forever; naming neither makes every duplicate a clean no-op.

**As built / proven.** Migration `014_up.sql` creates the index; the worker's
`_EVENT_INSERT_SQL` uses bare `ON CONFLICT DO NOTHING`. The dedup proof inserted: the same
event twice (redelivery → 1 row), a different `event_id` same key (Redis-flush dup →
dropped), and the same key on the next IST day (→ allowed). All four scenarios produced
exactly the expected rows.

### IMMUTABLE index expression — why `'05:30:00'::interval`, not `'Asia/Kolkata'`

The day bucket in the unique index is
`((ts AT TIME ZONE INTERVAL '5 hours 30 minutes')::date)`, **not**
`((ts AT TIME ZONE 'Asia/Kolkata')::date)`.

A unique-index expression must be `IMMUTABLE`. The named-zone form
`timezone(text, timestamptz)` is only `STABLE` — Postgres refuses it in an index
("functions in index expression must be marked IMMUTABLE"). The interval form
`timezone(interval, timestamptz)` **is** `IMMUTABLE`, because a fixed offset has no
session- or DST-dependent behavior, so it is accepted. Postgres stores it as
`(ts AT TIME ZONE '05:30:00'::interval)::date`.

**Why this is safe permanently.** India observes **no daylight saving time** and IST is a
fixed **UTC+05:30** offset — it has not changed since 1945 and there is no live proposal to
change it. The interval form is therefore *exactly* equivalent to `'Asia/Kolkata'` for
every timestamp, today and indefinitely, with none of the indexability cost. The labeler
(`event_labeler._IST_DAY_EXPR`) uses the byte-identical expression, and in Python buckets
with `ts.astimezone(IST).date()` where `IST = timezone(timedelta(hours=5, minutes=30))` —
the same fixed offset. A regression test asserts the migration and the labeler constant
match, so a future edit to one cannot silently diverge from the other (which would let a
midnight-UTC event dedup under one day but get labeled under another).

---

## Decision 3 — `daily_regime` deferred; `regime_id` emitted NULL

**Decision.** Phase 1 does not populate `daily_regime`. Every event is written with
`regime_id = NULL`. The column and the empty table remain as the Phase 0 spine created
them.

**Rationale.** Regime classification needs market-breadth / VIX aggregation that does not
exist yet, and an event can be detected and forward-return-labeled without it — the regime
link is an *enrichment*, not a dependency. Writing a placeholder (e.g. `regime_id` =
trade_date) was explicitly rejected: it would masquerade as real data and invite queries
that join on a meaningless key. An explicit `NULL` is honest and trivially backfillable —
a later regime phase can compute `daily_regime` and `UPDATE radar_events.regime_id` by
trade day without touching anything Phase 1 wrote.

**As built / proven.** `radar_events.py` sets `"regime_id": None` on every emitted event;
the worker inserts it as SQL `NULL`. No code anywhere in Phase 1 reads `daily_regime`.

---

## Forward-return labeling (how the labels stay trustworthy)

The labeler is a scheduled batch reader of `radar_snapshots.price` → writer of
`event_labels`. It never touches live serving or the persist worker, so it carries none of
the isolation risk of Decision 1. The guarantees baked in:

- **Direction frozen at emission, never recomputed.** `process_events` resolves direction
  once (ORB `broke_up`→`up` / `broke_down`→`down`; gap sign; `range_expansion`→`None`) and
  stores it in `trigger.direction`. The labeler reads it and applies ×+1 (`up`), ×−1
  (`down`), or ×+1-as-raw (`None`). A correct directional call is therefore always a
  positive `forward_return`; a `None`-direction event is labeled as a raw signed move and
  never guessed.
- **Nearest-frame, ±50s, never stretch-matched.** Frames land every ~45 s, so a genuine
  match is ≤~23 s off; the labeler picks the *nearest* frame within ±50 s (ordered by
  `abs(epoch)`), not the first in a window. If no frame falls inside tolerance (a polling
  gap or halt) the label is written with **NULL** metrics — never matched to a far frame.
  Each label records `matched_ts` + `offset_seconds` so loose matches can be filtered later.
- **EOD floor at 15:15 IST.** The `eod` horizon uses the day's last frame only if its ts is
  ≥ 15:15 IST; an earlier last print (illiquid / halted mid-day) writes `eod` NULL rather
  than passing a mid-day stop off as the close.
- **Maturity-gated, idempotent backfill.** A horizon is computed only once its window has
  matured (`event_ts + N + tolerance`, or 15:30 IST close for `eod`); `ON CONFLICT
  (event_id, horizon) DO UPDATE` makes any cadence safe. Scheduled every 5 min across
  market hours (so T+5 lands within ~6 min) plus a dedicated 15:40 EOD pass.

---

## Operational lessons baked into the design

These reflect real findings during Phase 1 build-out and are load-bearing, not optional:

- **`text[]` ≠ `uuid[]` in `ANY()`.** The labeler's `WHERE event_id = ANY(%s)` lookup
  failed at runtime with `operator does not exist: uuid = text` — psycopg2 sends a Python
  string list as `text[]`, and there is no implicit `text = uuid` operator for the `ANY`
  comparison (unlike an `INSERT`, which has an assignment cast). The fix is an explicit
  `ANY(%s::uuid[])`. Assignment casts on insert lull you into thinking text↔uuid "just
  works" — it does not for comparison operators.
- **E2E catches what fake-cursor unit tests cannot.** The `::uuid[]` bug was invisible to
  the unit tests because they stub the DB cursor — the SQL is never sent to a real planner,
  so a type-resolution error can't surface. It was caught only by the end-to-end run against
  a real Postgres. Fake-cursor tests verify *our* logic (direction signing, tolerance,
  floor, day bucketing); a thin E2E that actually executes the SQL is required to verify the
  *contract with Postgres* (type coercion, operator resolution, the IMMUTABLE-index
  acceptance itself). Keep both; neither substitutes for the other.
- **Validate the IMMUTABLE index on a real server, not from memory.** The
  STABLE-vs-IMMUTABLE distinction between the named-zone and interval `AT TIME ZONE` forms
  is not obvious and is easy to get wrong; the migration was applied to a throwaway DB to
  confirm the index is actually created before relying on it.

---

## Open items

- **Live-market `/radar` isolation proof — PENDING.** The AC#5 proof was run with synthetic
  frames/events against a throwaway DB (the Phase 0 "Proof 1" property extended to the
  events path): Postgres stopped mid-run, `radar:snapshot` kept serving, streams buffered,
  gap-free drain on recovery. A confirmation under a *live* poll loop during market hours is
  still outstanding (it cannot be reproduced off-hours without a live Upstox feed). The
  poller change is only one fire-and-forget `xadd_event` with zero DB dependency, so the
  loop is isolated by construction — but to confirm on the VPS during market hours:

  ```bash
  # with radar-poller + persist-worker running, market open:
  docker stop premarket-postgres-1
  # /radar must keep refreshing; events accumulate, not lost:
  watch -n2 'docker exec premarket-redis-1 redis-cli XLEN radar:events'
  # bring it back and confirm gap-free drain into radar_events:
  docker start premarket-postgres-1
  docker exec premarket-postgres-1 psql -U premarket -d premarket -c \
    "SELECT count(*), count(DISTINCT event_id) FROM radar_events WHERE ts::date = current_date;"
  journalctl -u persist-worker -n 30 --no-pager   # expect reclaim/drain, no errors
  ```

## Phase boundaries

Phase 1 (this document): events emitted to `radar_events`, forward returns in
`event_labels`, `regime_id` NULL. The `/edge` API, slice explorer, edge-decay monitor,
`daily_regime` population, and journal funnel are Phases 2–6.
