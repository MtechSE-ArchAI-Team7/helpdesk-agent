from __future__ import annotations

from typing import Literal

Priority = Literal["highest", "high", "medium", "low", "lowest"]

PRIORITY_CRITERIA: dict[Priority, str] = {
    "highest": (
        "The incident is blocking business progress now: a major service or business process is unavailable "
        "or severely impaired, with no effective workaround."
    ),
    "high": (
        "The incident could block progress: a significant service or business function is seriously impaired, "
        "or the issue is likely to become blocking soon."
    ),
    "medium": (
        "The incident has the potential to affect progress: impact is meaningful but contained, "
        "or a workaround exists while the issue is addressed."
    ),
    "low": (
        "The problem is minor or easily worked around, with limited impact and no immediate risk of blocking progress."
    ),
    "lowest": (
        "The problem is trivial, with little or no impact on business progress; typically informational or cosmetic."
    ),
}
