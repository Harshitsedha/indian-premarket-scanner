import { EdgeEventsTable } from "../components/EdgeEventsTable";

// Phase 2: the /edge route now serves the radar-event table (one row per event,
// horizons pivoted to columns, server-side filter/sort/paginate + CSV/Parquet export).
// It deliberately imports NONE of the old setups/outcomes edge-summary logic
// (EdgeStats / /api/edge/summary) — that is fully severed from this page.

export default function EdgePage() {
  return <EdgeEventsTable />;
}
