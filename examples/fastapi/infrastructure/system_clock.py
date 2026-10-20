from datetime import datetime, timezone

from domain.clock import IClock


class SystemClock(IClock):
    def now(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
