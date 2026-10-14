"""ČNB daily fixing (CZK per unit); falls back to the last stored rate."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import requests


def parse_cnb(text: str) -> tuple[str, dict[str, float]]:
    lines = text.strip().splitlines()
    head = lines[0].split("#")[0].strip()  # "13 Oct 2026" (English page) or "13.10.2026" (Czech)
    fmt = "%d.%m.%Y" if "." in head else "%d %b %Y"
    date = dt.datetime.strptime(head, fmt).date().isoformat()
    rates = {}
    for ln in lines[2:]:
        _, _, amount, code, rate = ln.split("|")
        rates[code] = float(rate.replace(",", ".")) / float(amount)
    return date, rates


class Fx:
    def __init__(self, path: Path, url: str, fetch: bool = True):
        self.path = path
        stored = json.loads(path.read_text()) if path.exists() else {}
        self.date, self.rates, self.source, self.error = stored.get("date"), stored.get("rates", {}), "stored", None
        if fetch:
            try:
                r = requests.get(url, timeout=30)
                r.raise_for_status()
                self.date, self.rates = parse_cnb(r.text)
                self.source = "cnb"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"date": self.date, "rates": self.rates}, indent=1) + "\n")
            except Exception as e:  # noqa: BLE001 - fall back to the stored rate
                self.error = f"{type(e).__name__}: {e}"[:200]
        self.rates.setdefault("CZK", 1.0)

    def to_czk(self, amount: float, cur: str) -> float:
        if cur not in self.rates:
            raise KeyError(f"no ČNB rate for {cur}")
        return amount * self.rates[cur]

    def czk_to(self, amount_czk: float, cur: str) -> float:
        return amount_czk / self.rates[cur]

    def meta(self) -> dict:
        return {"source": self.source, "fx_date": self.date, "eur_czk": self.rates.get("EUR"),
                "usd_czk": self.rates.get("USD"), "error": self.error}
