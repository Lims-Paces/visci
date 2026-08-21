"""
Adaptive concurrency control.

THE PROBLEM
-----------
Generating shots one at a time makes a 15-shot film take 30+ minutes. Running
them in parallel cuts that to under 10. But naive parallelism makes rate limits
WORSE, not better:

  - 5 workers are 5x more likely to trip a rate limit
  - when they do, all 5 back off together and retry together — the classic
    thundering herd, which produces bursts of failure rather than throughput
  - a quota ceiling is discovered independently by every worker, each burning
    its own backoff for a guaranteed failure

THE APPROACH
------------
1. START LOW, adapt upward. Begin at 3 concurrent, not 5. On a rate limit,
   halve it. After sustained success, step back up. The pipeline finds the
   provider's real ceiling instead of guessing.

2. JITTER every backoff so workers do not retry in lockstep.

3. QUOTA STOPS EVERYTHING. A rate limit means "slow down"; a quota means "you
   are done for today". The latter must abort the whole run at once rather than
   letting 5 workers each discover it separately.

4. SPEND CEILING CHECKED BEFORE EACH DISPATCH. Concurrency means a runaway bug
   burns money 5x faster, so the ledger gets a veto on every shot before it
   starts.
"""

import random
import threading


class ConcurrencyController:
    """
    Tracks how many shots may run at once, adjusting to what the provider allows.

    Thread-safe: every worker reports outcomes here, so all mutation is guarded
    by a lock.
    """

    def __init__(
        self,
        start: int = 3,
        minimum: int = 1,
        maximum: int = 8,
        successes_before_increase: int = 2,
    ) -> None:
        # Starting at 3 rather than 5 is deliberate. It is slightly slower on a
        # generous quota and considerably safer on a tight one — and it climbs
        # on its own if the provider is happy.
        self.limit = start
        self.minimum = minimum
        self.maximum = maximum
        self.successes_before_increase = successes_before_increase

        self._lock = threading.Lock()
        self._consecutive_successes = 0

        # Permits currently held by running workers. Compared against `limit` to
        # decide whether another may start. A plain Semaphore cannot do this —
        # its capacity is fixed at construction, so a limit change would have no
        # effect. A condition variable lets the limit move at runtime.
        self._in_flight = 0
        self._space = threading.Condition(self._lock)

        # Set when a quota ceiling is hit. Every worker checks this and stops,
        # so the run aborts once rather than 5 times.
        self.aborted = False
        self.abort_reason: str | None = None

    def acquire(self) -> bool:
        """
        Wait for a slot. Returns False if the run was aborted while waiting.

        This is what actually enforces the limit, and because it re-reads
        `self.limit` on every wake-up, a mid-run change takes effect
        immediately — unlike a fixed-capacity semaphore.
        """
        with self._space:
            while not self.aborted and self._in_flight >= self.limit:
                self._space.wait(timeout=1.0)
            if self.aborted:
                return False
            self._in_flight += 1
            return True

    def release(self) -> None:
        with self._space:
            self._in_flight -= 1
            self._space.notify()

    def on_success(self) -> None:
        """
        A shot completed. After a short run of successes, allow one more.

        Recovery must be reasonably quick. An early rate limit can halve the
        limit twice, and if climbing back needed many successes the pipeline
        would spend the rest of a 15-shot film crippled at 1 — one transient
        hiccup permanently costing throughput.

        Still gradual rather than instant: jumping straight back to maximum
        after a single success would just re-trip the limit.
        """
        with self._space:
            self._consecutive_successes += 1
            if self._consecutive_successes >= self.successes_before_increase:
                self._consecutive_successes = 0
                if self.limit < self.maximum:
                    self.limit += 1
                    # Wake a waiting worker — a slot just opened.
                    self._space.notify()

    def on_rate_limit(self) -> int:
        """
        A rate limit was hit. Halve concurrency and report the new limit.

        Halving rather than decrementing is the standard response — it backs off
        fast enough to actually clear the limit, whereas stepping down by one
        keeps re-tripping it.
        """
        with self._space:
            self._consecutive_successes = 0
            self.limit = max(self.minimum, self.limit // 2)
            return self.limit

    def abort(self, reason: str) -> None:
        """
        Stop the entire run. Used for quota ceilings and spend limits.

        Distinct from a rate limit: these do not clear by waiting, so every
        in-flight worker should stop rather than discover the same wall alone.
        """
        with self._space:
            if not self.aborted:
                self.aborted = True
                self.abort_reason = reason
            # Wake every waiting worker so they observe the abort and exit
            # rather than blocking until timeout.
            self._space.notify_all()

    def should_stop(self) -> bool:
        with self._space:
            return self.aborted


def jittered_delay(base: float, spread: float = 0.3) -> float:
    """
    Add randomness to a backoff delay.

    Without jitter, workers that failed together retry together, re-creating the
    exact burst that caused the limit. Spreading retries over a window lets them
    trickle back in.
    """
    return base * (1.0 + random.uniform(-spread, spread))
