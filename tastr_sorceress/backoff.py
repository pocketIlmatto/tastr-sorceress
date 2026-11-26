"""Exponential backoff with jitter, so a flaky site is never hammered."""

from __future__ import annotations

import random


class Backoff:
    def __init__(self, base: float = 5.0, cap: float = 300.0, rng: random.Random | None = None):
        self.base = base
        self.cap = cap
        self.failures = 0
        self._rng = rng or random.Random()

    def next_delay(self) -> float:
        delay = min(self.cap, self.base * (2 ** min(self.failures, 20)))
        self.failures += 1
        # Between 75% and 100% of the delay, so many helpers don't retry in lockstep.
        return delay * (0.75 + 0.25 * self._rng.random())

    def reset(self) -> None:
        self.failures = 0
