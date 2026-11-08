"""A calendar month in UTC: the invoice period and the unit prices change on (ADR 0017).

.NET: a small immutable value type, like a `record struct Month(int Year, int Month)`.
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime

_FORMAT = re.compile(r"(\d{4})-(\d{2})")


@dataclass(frozen=True, order=True)
class Month:
    year: int
    month: int

    def __post_init__(self) -> None:
        if not (1 <= self.year <= 9998 and 1 <= self.month <= 12):
            raise ValueError(f"not a month: {self.year}-{self.month}")

    @classmethod
    def parse(cls, text: str) -> "Month":
        match = _FORMAT.fullmatch(text)
        if not match or not 1 <= int(match[2]) <= 12:
            raise ValueError(f"{text!r} is not a month: use YYYY-MM, like 2026-11")
        return cls(int(match[1]), int(match[2]))

    @classmethod
    def of(cls, when: datetime) -> "Month":
        """The month an instant falls in, in UTC."""
        utc = when.astimezone(UTC)
        return cls(utc.year, utc.month)

    @property
    def first_day(self) -> date:
        return date(self.year, self.month, 1)

    @property
    def start(self) -> datetime:
        """The month's first instant, UTC. The month is [start, end)."""
        return datetime(self.year, self.month, 1, tzinfo=UTC)

    @property
    def end(self) -> datetime:
        """The next month's first instant, UTC: not part of this month."""
        return self.next().start

    def next(self) -> "Month":
        return Month(self.year + 1, 1) if self.month == 12 else Month(self.year, self.month + 1)

    def previous(self) -> "Month":
        return Month(self.year - 1, 12) if self.month == 1 else Month(self.year, self.month - 1)

    def __str__(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"
