"""Ordering strategies: which waiting item a long-lived queue pulls next.

A queue with room pulls one waiting item. Which one is policy, and it can
depend on facts Waystation never sees — a ticket graph, a deadline, a cost —
so it is injected, as integration is (ADR-0005). The default is arrival
order, which is what a queue does with no strategy given (ADR-0017,
ADR-0048).

One protocol serves every queue, generic over what the queue holds: the run
queue offers its waiting ``QueuedRun`` handles.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

__all__ = ["ArrivalOrder", "OrderingStrategy"]


class OrderingStrategy[ItemT](Protocol):
    """The rule a queue asks which waiting item to pull next."""

    def pick(self, waiting: Sequence[ItemT]) -> ItemT:
        """Choose the item to pull now.

        Asked at each pull that has a choice to make — more waiting than
        there is room for — over everything waiting then, so a ranking that
        changes as other items land is read as it is at the pull. Nothing
        guards against starvation: an item ranked last for ever waits for
        ever, and ageing, if a caller wants it, is the strategy's to do.

        Args:
            waiting: Every item waiting now, oldest first; never empty.

        Returns:
            One of ``waiting``, itself — not an equal copy.

        Raises:
            Exception: Anything a strategy raises is a failure the queue
                reports for each item it was ordering, never a raise out of
                the queue (ADR-0016).
        """
        ...


class ArrivalOrder:
    """Pull the oldest waiting item: first come, first served.

    What a queue does with no strategy given.
    """

    def pick[ItemT](self, waiting: Sequence[ItemT]) -> ItemT:
        """The oldest waiting item.

        Args:
            waiting: Every item waiting now, oldest first; never empty.

        Returns:
            The first of ``waiting``.
        """
        return waiting[0]
