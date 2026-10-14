"""Subprocess worker for the fast-flights fork (Mohamed3on/flights).

Run with the fork's install dir first on PYTHONPATH. Reads one JSON request on
stdin, prints one JSON reply: {"payload": [...], "complete": bool} or {"error": str}.
Stdlib + fork only: must not import the upstream fast_flights package.
"""
import dataclasses
import inspect
import json
import sys


class LenientJson:
    """The fork json.loads()es each streamed line; on some IPs a line carries
    trailing data after the first JSON value ("Extra data"). Parse the first value only."""
    dumps = staticmethod(json.dumps)

    @staticmethod
    def loads(s):
        return json.JSONDecoder().raw_decode(s.strip())[0]


def main() -> None:
    req = json.load(sys.stdin)
    import fast_flights
    from fast_flights import fetcher
    from fast_flights.querying import TRIP_LOOKUP, Query

    if req.get("capabilities"):
        print(json.dumps({
            "create_query": list(inspect.signature(fast_flights.create_query).parameters),
            "flightquery": [f.name for f in dataclasses.fields(fast_flights.FlightQuery)],
            "trips": list(TRIP_LOOKUP),
        }))
        return

    class Raw(list):
        complete = True

    def page_payload(html):
        script = fetcher.LexborHTMLParser(html).css_first(r"script.ds\:1")
        if script is None:
            head = html[:20000].lower()
            raise RuntimeError("EU consent page" if "consent" in head else "no ds:1 data in page")
        return Raw([json.loads(script.text().split("data:", 1)[1].rsplit(",", 1)[0])])

    fetcher.json = LenientJson
    deferred_errors = []
    deferred = fetcher._fetch_deferred_results

    def safe_deferred(*a, **k):
        """The follow-up step crashes on some page layouts (seen on GitHub runners);
        treat that like a refusal so the fork falls back to the page's own results."""
        try:
            return deferred(*a, **k)
        except fetcher.FlightsNotFound:
            raise
        except Exception as e:  # noqa: BLE001
            deferred_errors.append(f"{type(e).__name__}: {e}"[:200])
            raise fetcher.FlightsNotFound(deferred_errors[-1]) from e

    fetcher._fetch_deferred_results = safe_deferred
    # Return Google's raw payload instead of the fork's parsed model.
    fetcher.parse_payload = lambda p: Raw([p])
    fetcher.parse = page_payload

    class TfsQuery(Query):
        def __init__(self, tfs, hl, curr, gl):  # noqa: D107 - bypass dataclass init
            self._tfs, self.language, self.currency, self._gl = tfs, hl, curr, gl

        def to_str(self):
            return self._tfs

        def params(self):
            p = {"tfs": self._tfs, "hl": self.language, "tfu": "EgQIABABIgA", "curr": self.currency}
            if self._gl:
                p["gl"] = self._gl
            return p

    try:
        res = fetcher.get_flights(TfsQuery(req["tfs"], req["hl"], req["curr"], req.get("gl", "")))
        print(json.dumps({"payload": res[0], "complete": res.complete, "note": "; ".join(deferred_errors)}))
    except Exception as e:  # noqa: BLE001 - reported to the parent, with where it failed
        import traceback
        where = " <- ".join(f"{f.name}:{f.lineno}" for f in reversed(traceback.extract_tb(e.__traceback__)[-3:]))
        print(json.dumps({"error": f"{type(e).__name__}: {e} @ {where}"[:500]}))


if __name__ == "__main__":
    main()
