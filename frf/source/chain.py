"""Several indexes walked as one, in order.

WHY THIS EXISTS. GitHub's search will not accept `topic:parser OR topic:formatter` -- it answers
422 -- so a query that wants several topics has to be several queries. But `sourcing.walk` takes
ONE index, and rightly: it records coverage against a single denominator, and an index that
silently changed what it was searching for mid-walk would make that record meaningless.

So the several queries are joined HERE, where the joining is visible, rather than inside `GitHub`
where it would look like one search that happened to return everything.

WHAT IT PROMISES, which is exactly what every other index in this package promises:

    page(n, size=)   the nth page. EMPTY MEANS EXHAUSTED -- and here that means every link has
                     been exhausted, not just the current one. A link that runs dry advances to
                     the next; only the last one running dry ends the walk.
    total()          the sum of what the links report, or None if any of them will not say.
                     Summing over a None would invent a denominator, so None travels.
    name             what appears in the coverage log, naming every link.
"""
from __future__ import annotations

from ..core.scale import Candidate


class Chain:
    """Indexes walked end to end, as though they were one.

    Deliberately NOT interleaved. The links are given in preference order -- the first is the one
    most likely to hold usable material -- and a batch with a small budget should spend it there
    rather than sampling each link evenly. Interleaving would make a budget of five draw one
    candidate from five different searches, which is the worst of both.
    """

    def __init__(self, links: list, *, name: str = "") -> None:
        if not links:
            raise ValueError("a chain needs at least one index to walk")
        self._links = list(links)
        self.name = name or "chain(%s)" % ",".join(
            getattr(link, "name", "?") for link in self._links)
        # Which link a page number begins in, recorded rather than computed -- the same reason
        # `GitHub.page` records positions: a link that runs out early makes the arithmetic wrong
        # in a way that looks like it is working.
        self._positions: dict = {0: (0, 0)}

    def total(self) -> int | None:
        """The sum over links, or None when any link will not say.

        None is honest and supported; a total that quietly skipped the links which do not publish
        one would understate the supply and make every yield computed against it too high.
        """
        running = 0
        for link in self._links:
            one = link.total()
            if one is None:
                return None
            running += one
        return running

    def page(self, number: int, *, size: int = 20) -> list:
        """One page, from whichever link that page number falls in."""
        index, offset = self._position_of(number)
        while index < len(self._links):
            rows = self._links[index].page(offset, size=size)
            if rows:
                self._positions[number + 1] = (index, offset + 1)
                return list(rows)
            # This link is spent. The NEXT one starts at its own page zero -- returning [] here
            # would tell `walk()` the whole chain had run out because its first search did.
            index, offset = index + 1, 0
        self._positions[number + 1] = (index, 0)
        return []

    def _position_of(self, number: int) -> tuple:
        if number in self._positions:
            return self._positions[number]
        if not self._positions:
            return (0, 0)
        furthest = max(self._positions)
        return self._positions[furthest] if number >= furthest else (0, 0)
