"""
Spend tracking and hard limits.

WHY THIS EXISTS
---------------
The pipeline makes ~35 paid API calls per film and can run unattended. A retry
bug that loops is not a theoretical risk — it is the single most likely way this
project costs you real money unexpectedly. An aggressive retry on a failing shot
can burn $100 in an hour with nobody watching.

So spend is not merely observed, it is ENFORCED, in two steps:

  1. reserve()  BEFORE a paid call: the call's estimated cost is held against
     both ceilings. If it would not fit, BudgetExceeded is raised and the call
     is never made. Reservations are what stop several concurrent workers from
     each seeing "just under the limit" and all dispatching at once.
  2. spend()    AFTER a paid call: the real cost is ALWAYS recorded, because
     the money is already gone. Only then is a ceiling checked, so a breach
     halts the next call without making the ledger under-report this one.

An earlier version checked the ceiling inside spend() and refused to record a
call that crossed it. Since spend() runs after the API call, that meant money
was spent but never counted, the job total stayed under the cap, and workers
kept dispatching. A $3.50 ceiling let $12 through in testing.

The daily cap persists to disk so it survives restarts. A per-process counter
would reset every time you re-run, which defeats the purpose.
"""

import json
import threading
from datetime import UTC, datetime

from visci.config import OUTPUT_DIR, settings
from visci.providers.base import CostReport


def _today() -> str:
    """
    Today's date in UTC.

    Deliberately UTC rather than local time. The daily spend cap must reset at a
    predictable moment regardless of where the code runs — a laptop that changes
    timezone, or a server in a different region, would otherwise reset the
    budget at a surprising hour.
    """
    return datetime.now(UTC).date().isoformat()


class BudgetExceeded(RuntimeError):
    """Raised when a spend ceiling would be breached. Halts the run."""


# Every Ledger in this process shares one lock for the daily file. Two jobs
# running at once (the API allows it) would otherwise both read the same daily
# total, add their own cost, and the second write would erase the first.
_DAILY_LOCK = threading.Lock()


class Ledger:
    """
    Tracks money spent on one job, and across all jobs today.

    Usage is deliberately awkward to bypass — every provider call must report
    its cost through here, so there is no path where spend goes unrecorded.
    """

    def __init__(self, job_id: str, already_spent_usd: float = 0.0) -> None:
        self.job_id = job_id
        # A resumed job starts from what it has ALREADY spent. Starting at zero
        # would hand every resume a fresh per-job budget, so repeated resumes
        # could spend the ceiling many times over.
        self.job_total_usd = already_spent_usd
        self.entries: list[CostReport] = []
        self._daily_file = OUTPUT_DIR / ".daily_spend.json"

        # Estimated cost of calls currently in flight. Counted against the
        # ceilings so concurrent workers cannot collectively overshoot.
        self._reserved_usd = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ api --

    def reserve(self, estimate_usd: float) -> None:
        """
        Hold an estimated cost against both ceilings BEFORE a paid call.

        Raises BudgetExceeded if the call would not fit, so it is never made.
        Pair every successful reserve() with a release() once the call is done
        (in a `finally`), whether it succeeded or failed.
        """
        if estimate_usd <= 0:
            return
        with self._lock, _DAILY_LOCK:
            prospective_job = self.job_total_usd + self._reserved_usd + estimate_usd
            if prospective_job > settings.max_cost_per_job_usd:
                raise BudgetExceeded(
                    f"Job {self.job_id} would reach ~${prospective_job:.2f}, over the "
                    f"${settings.max_cost_per_job_usd:.2f} per-job ceiling. "
                    f"Halted before spending. Raise max_cost_per_job_usd if intended."
                )
            prospective_daily = self._read_daily() + self._reserved_usd + estimate_usd
            if prospective_daily > settings.max_cost_per_day_usd:
                raise BudgetExceeded(
                    f"Today's spend would reach ~${prospective_daily:.2f}, over the "
                    f"${settings.max_cost_per_day_usd:.2f} daily ceiling. "
                    f"Halted before spending. Resets at midnight UTC."
                )
            self._reserved_usd += estimate_usd

    def release(self, estimate_usd: float) -> None:
        """Return a reservation once its call has finished (or failed)."""
        if estimate_usd <= 0:
            return
        with self._lock:
            self._reserved_usd = max(0.0, self._reserved_usd - estimate_usd)

    def spend(self, report: CostReport) -> None:
        """
        Record a cost that has ALREADY been incurred, then enforce the ceilings.

        Called immediately after a paid API call returns, so the money is gone.
        It is therefore always recorded first — refusing to record it would make
        the ledger under-report real spend. Then, if a ceiling is now crossed,
        BudgetExceeded is raised so the run stops before the NEXT call.
        """
        if report.usd <= 0:
            # Mock mode, or a genuinely free call. Nothing to record.
            return

        with self._lock, _DAILY_LOCK:
            self.job_total_usd += report.usd
            self.entries.append(report)
            daily = self._read_daily() + report.usd
            self._write_daily(daily)
            job_total = self.job_total_usd

        if job_total > settings.max_cost_per_job_usd:
            raise BudgetExceeded(
                f"Job {self.job_id} reached ${job_total:.2f}, over the "
                f"${settings.max_cost_per_job_usd:.2f} per-job ceiling. Run halted."
            )
        if daily > settings.max_cost_per_day_usd:
            raise BudgetExceeded(
                f"Today's spend reached ${daily:.2f}, over the "
                f"${settings.max_cost_per_day_usd:.2f} daily ceiling. Run halted."
            )

    def summary(self) -> str:
        """Human-readable breakdown, printed at the end of a run."""
        if not self.entries:
            return "No spend (mock mode)."
        lines = [f"  {e.detail:<40} ${e.usd:>8.4f}" for e in self.entries]
        lines.append(f"  {'TOTAL':<40} ${self.job_total_usd:>8.4f}")
        return "\n".join(lines)

    # ------------------------------------------------------- daily persistence --

    def _read_daily(self) -> float:
        """
        Today's spend so far, across every job.

        Stored on disk rather than in memory so the cap survives restarts —
        otherwise re-running after a crash would reset it to zero, which is
        exactly when you least want the safety net gone.
        """
        if not self._daily_file.exists():
            return 0.0
        try:
            data = json.loads(self._daily_file.read_text())
        except (json.JSONDecodeError, OSError):
            # A corrupt ledger must not be treated as "spent nothing" — that
            # would silently disable the cap. Assume the worst instead.
            return settings.max_cost_per_day_usd
        if data.get("date") != _today():
            return 0.0  # New day, fresh budget.
        return float(data.get("usd", 0.0))

    def _write_daily(self, total: float) -> None:
        self._daily_file.parent.mkdir(parents=True, exist_ok=True)
        self._daily_file.write_text(
            json.dumps({"date": _today(), "usd": round(total, 6)})
        )


# ---------------------------------------------------------------------------
# Published prices, verified August 2026.
#
# These are ESTIMATES used to compute what a call cost, since most of these APIs
# do not return a billed amount. Treat them as approximate and re-check against
# the provider's pricing page periodically — this is exactly the kind of
# constant that silently goes stale.
# ---------------------------------------------------------------------------

VEO_FAST_720P_USD_PER_SECOND = 0.10
VEO_LITE_720P_USD_PER_SECOND = 0.05

# Gemini 3 Pro Image (Nano Banana Pro), 1K-2K output: 1120 tokens = $0.134.
#
# This was originally set to $0.039 — the Gemini 2.5 FLASH Image price — which
# under-reported image spend by 3.4x and made the ledger disagree with Google's
# billing. Images are ~19 per film (12 reference sheets + 15 keyframes, minus
# retries), so the error was material: ~$2.50 per film reported as ~$0.75.
#
# Lesson: price constants must name the exact model tier they belong to.
NANO_BANANA_PRO_USD_PER_IMAGE = 0.134
NANO_BANANA_PRO_4K_USD_PER_IMAGE = 0.24

# Claude Opus 5: $5 per million input tokens, $25 per million output.
CLAUDE_INPUT_USD_PER_TOKEN = 5.0 / 1_000_000
CLAUDE_OUTPUT_USD_PER_TOKEN = 25.0 / 1_000_000

# Gemini Flash TTS is free of charge on the preview free tier. Recorded as zero,
# but kept as a named constant so there is an obvious place to set a real price
# when the preview ends.
GEMINI_TTS_USD_PER_CHARACTER = 0.0
