-- Migration 008: dynamic strategy registry
-- Stores Claude-generated strategy classes; loaded at runtime by backtest/loader.py.
-- gap_and_go is seeded as the immutable baseline (validated=true, never deleted).

CREATE TABLE IF NOT EXISTS backtest_strategies (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name              TEXT        UNIQUE NOT NULL,         -- machine name used in jobs
    display_name      TEXT        NOT NULL,
    description       TEXT        NOT NULL,                -- user's original English description
    code              TEXT        NOT NULL,                -- Python class source (class definition only)
    validated         BOOLEAN     NOT NULL DEFAULT false,
    validation_report JSONB       NULL,                    -- Claude's structured validation output
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at        TIMESTAMPTZ NULL                     -- soft delete; gap_and_go never deleted
);

-- Seed gap_and_go as the immutable baseline strategy.
-- code stores only the class body; loader provides Action/Signal/BarContext in exec namespace.
INSERT INTO backtest_strategies (name, display_name, description, code, validated)
VALUES (
    'gap_and_go',
    'Gap and Go',
    'Buys breakout of opening range high on gap-up days. Entry triggers when price breaks the N-minute opening range high within the entry window, with stop at OR low and configurable R target.',
    $code$
class GapAndGo:
    """
    Gap-and-go momentum strategy for NSE equities.

    Entry logic:
      1. First bar of day: compute gap = (open - prev_close) / prev_close * 100
      2. If gap >= min_gap_pct: start tracking the opening range
      3. After opening_range_min bars complete: OR is locked (high/low captured)
      4. If the OR high is broken within entry_window_min bars of the session open
         (measured from the day's first bar, not from OR close): signal ENTER_LONG
         - stop  = OR low
         - target = fill + (fill - stop) * target_r   [computed by engine after fill]
         Once entry_window_min bars have elapsed with no breakout, the day is
         cancelled -- no entry even if the OR high breaks later.

    Exit logic (engine enforced):
      - Stop hit:   bar.low  <= stop_price
      - Target hit: bar.high >= target_price
      - EOD:        last bar of trading day if eod_exit=True
    """

    def __init__(
        self,
        min_gap_pct:        float = 1.0,
        opening_range_min:  int   = 15,
        entry_window_min:   int   = 60,
        stop_pct:           float = 1.0,
        target_r:           float = 2.0,
        eod_exit:           bool  = True,
    ) -> None:
        self.min_gap_pct       = min_gap_pct
        self.opening_range_min = opening_range_min
        self.entry_window_min  = entry_window_min
        self.stop_pct          = stop_pct
        self.target_r          = target_r
        self.eod_exit          = eod_exit

        self._tracking:     bool  = False
        self._entered:      bool  = False
        self._gap_pct:      float = 0.0
        self._is_first_bar: bool  = False

    def reset_day(self) -> None:
        self._tracking     = False
        self._entered      = False
        self._gap_pct      = 0.0
        self._is_first_bar = True

    def on_bar(self, ctx: BarContext) -> Signal | None:
        bar = ctx.current

        if self._is_first_bar:
            self._is_first_bar = False
            prev_close = ctx.prev_close()
            if prev_close and prev_close > 0:
                gap = (float(bar["open"]) - prev_close) / prev_close * 100
                if gap >= self.min_gap_pct:
                    self._tracking = True
                    self._gap_pct  = round(gap, 4)
            return None

        if not self._tracking or self._entered:
            return None

        if ctx.position is not None:
            return None

        or_high = ctx.opening_range_high(self.opening_range_min)
        or_low  = ctx.opening_range_low(self.opening_range_min)
        if or_high is None or or_low is None:
            return None

        today_bar_count = int((ctx.bars["date"] == ctx.current["date"]).sum())
        if today_bar_count > self.entry_window_min:
            self._tracking = False
            return None

        if float(bar["high"]) > or_high:
            self._entered = True
            return Signal(
                action     = Action.ENTER_LONG,
                stop_price = or_low,
                target_r   = self.target_r,
                gap_pct    = self._gap_pct,
                reason     = (
                    f"gap={self._gap_pct:.2f}% OR[{self.opening_range_min}] "
                    f"break {float(bar['high']):.2f}>{or_high:.2f}"
                ),
            )

        return None
$code$,
    true
)
ON CONFLICT (name) DO NOTHING;
