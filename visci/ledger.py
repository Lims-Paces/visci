"""
Spend tracking and hard limits.

WHY THIS EXISTS
---------------
The pipeline makes ~35 paid API calls per film and can run unattended. A retry
bug that loops is not a theoretical risk — it is the single most likely way this
project costs you real money unexpectedly. An aggressive retry on a failing shot
can burn $100 in an hour with nobody watching.

So spend is not merely observed, it is ENFORCED. Every provider call goes
through `spend()`, which raises and halts the run before exceeding a ceiling.
The pipeline stops with a clear error rather than quietly draining a card.

The daily cap persists to disk so it survives restarts. A per-process counter
would reset every time you re-run, which defeats the purpose.
"""

import json
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


class Ledger:
    """
    Tracks money spent on one job, and across all jobs today.

    Usage is deliberately awkward to bypass — every provider call must report
    its cost through here, so there is no path where spend goes unrecorded.
    """

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.job_total_usd = 0.0
        self.entries: list[CostReport] = []
        self._daily_file = OUTPUT_DIR / ".daily_spend.json"

    # ------------------------------------------------------------------ api --

    def spend(self, report: CostReport) -> None:
        """
        Record a cost, after checking it does not breach either ceiling.

        Called by every provider immediately after a paid API call. Raises
        BudgetExceeded rather than returning a failure, because continuing past
        a spend limit is never the right behaviour.
        """
        if report.usd <= 0:
            # Mock mode, or a genuinely free call. Nothing to record.
            return

        prospective_job = self.job_total_usd + report.usd
        if prospective_job > settings.max_cost_per_job_usd:
            raise BudgetExceeded(
                f"Job {self.job_id} would reach ${prospective_job:.2f}, "
                f"over the ${settings.max_cost_per_job_usd:.2f} per-job ceiling. "
                f"Halted before spending. Raise max_cost_per_job_usd if intended."
            )

        daily = self._read_daily()
        prospective_daily = daily + report.usd
        if prospective_daily > settings.max_cost_per_day_usd:
            raise BudgetExceeded(
                f"Today's spend would reach ${prospective_daily:.2f}, "
                f"over the ${settings.max_cost_per_day_usd:.2f} daily ceiling. "
                f"Halted before spending. Resets at midnight."
            )

        self.job_total_usd = prospective_job
        self.entries.append(report)
        self._write_daily(prospective_daily)

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
