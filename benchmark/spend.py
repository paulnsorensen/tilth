"""Run-wide spend ledger shared by every paid call in a benchmark run.

Agent cells, judge calls, and the evolve loop's reflection and proposer calls all
charge the same ledger, so one ``--max-usd`` ceiling bounds the whole run. Callers
check ``would_cross`` with their own estimate before a call and ``charge`` its
cost afterwards.
"""

import math
from dataclasses import dataclass, field


@dataclass
class SpendLedger:
    """Cumulative spend against a ceiling; ``max_usd=None`` means unbounded."""

    max_usd: float | None
    spent: float = 0.0
    charges: list[tuple[float, str]] = field(default_factory=list)

    def would_cross(self, estimate: float) -> bool:
        ceiling = math.inf if self.max_usd is None else self.max_usd
        return self.spent + estimate > ceiling

    def charge(self, cost: float, *, source: str) -> None:
        if cost < 0:
            raise ValueError(f"negative charge {cost} from {source}")
        self.spent += cost
        self.charges.append((cost, source))
