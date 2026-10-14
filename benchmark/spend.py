"""Run-wide spend ledger shared by every paid call in a benchmark run.

Agent cells, judge calls, and the evolve loop's reflection and proposer calls all
charge the same ledger, so one ``--max-usd`` ceiling bounds the whole run. Callers
check ``would_cross`` with their own estimate before a call and ``charge`` its
cost afterwards. ``reserve`` holds back part of the ceiling: the evolve loop sets
it during its search so every check, the judge's included, leaves the finish
phase's budget unspent, then sets it to 0 for finish.
"""

import math
from dataclasses import dataclass, field

# Unreserved headroom below this floor counts as exhausted, so float leftovers never become a budget cap.
MIN_HEADROOM_USD = 0.01


@dataclass
class SpendLedger:
    """Cumulative spend against a ceiling; ``max_usd=None`` means unbounded."""

    max_usd: float | None
    spent: float = 0.0
    charges: list[tuple[float, str]] = field(default_factory=list)
    reserve: float = 0.0

    def would_cross(self, estimate: float) -> bool:
        ceiling = math.inf if self.max_usd is None else self.max_usd
        return self.spent + estimate + self.reserve > ceiling

    def headroom(self) -> float | None:
        """Unreserved spend left under the ceiling; ``None`` when unbounded."""
        return None if self.max_usd is None else self.max_usd - self.spent - self.reserve

    def exhausted(self) -> bool:
        """True when the unreserved headroom is below ``MIN_HEADROOM_USD``."""
        headroom = self.headroom()
        return headroom is not None and headroom < MIN_HEADROOM_USD

    def charge(self, cost: float, *, source: str) -> None:
        if cost < 0:
            raise ValueError(f"negative charge {cost} from {source}")
        self.spent += cost
        self.charges.append((cost, source))
