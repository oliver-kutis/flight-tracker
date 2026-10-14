"""Paid-source quota counters persisted in data/quota.json."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path


class Quota:
    def __init__(self, path: Path, caps: dict[str, dict[str, int]], now=None):
        """caps: {source: {"daily_cap": n, "monthly_cap": n, "hourly_cap": n}} (any subset)."""
        self.path, self.caps = path, caps
        self._now = now or (lambda: dt.datetime.now(dt.UTC))
        self.data = json.loads(path.read_text()) if path.exists() else {}

    def _buckets(self) -> dict[str, str]:
        n = self._now()
        return {"hourly_cap": n.strftime("%Y-%m-%dT%H"), "daily_cap": n.strftime("%Y-%m-%d"),
                "monthly_cap": n.strftime("%Y-%m")}

    def used(self, source: str) -> dict[str, int]:
        d = self.data.get(source, {})
        return {cap: d.get(cap, {}).get(key, 0) for cap, key in self._buckets().items()}

    def allowed(self, source: str) -> bool:
        used = self.used(source)
        return all(used[c] < lim for c, lim in self.caps.get(source, {}).items())

    def consume(self, source: str, n: int = 1) -> None:
        d = self.data.setdefault(source, {})
        for cap, key in self._buckets().items():
            d[cap] = {key: d.get(cap, {}).get(key, 0) + n}  # keep only the current bucket
        self.save()

    def report(self) -> dict:
        return {s: {"used": self.used(s), "caps": c} for s, c in self.caps.items()}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1, sort_keys=True) + "\n")
