"""
Storage layer.

Two interchangeable implementations of the same `Store` interface:

  * PostgrestStore — talks to Supabase Postgres over its PostgREST HTTP API
    using only `requests` (no heavy SDK), with the service_role key.
  * MemoryStore    — an in-process store used by `--dry-run` and the unit
    tests, so the whole cycle can run with no database at all.

The engine and dashboard depend ONLY on this interface, never on Supabase
directly. Swapping to a different backend later is a one-file change.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

import requests


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Interface
# --------------------------------------------------------------------------- #
class Store:
    # config -----------------------------------------------------------------
    def latest_config(self) -> Optional[dict]: raise NotImplementedError
    def insert_config(self, payload: dict) -> dict: raise NotImplementedError
    def config_history(self, limit: int = 50) -> list[dict]: raise NotImplementedError

    # cycles -----------------------------------------------------------------
    def create_cycle(self, payload: dict) -> dict: raise NotImplementedError
    def update_cycle(self, cycle_id: int, patch: dict) -> None: raise NotImplementedError

    # snapshots / observations ----------------------------------------------
    def insert_leaderboard(self, rows: list[dict]) -> None: raise NotImplementedError
    def insert_observations(self, rows: list[dict]) -> None: raise NotImplementedError
    def latest_observations(self, limit: int = 500) -> list[dict]: raise NotImplementedError
    def latest_leaderboard(self) -> list[dict]: raise NotImplementedError

    # paper trades -----------------------------------------------------------
    def open_trades(self, strategy: Optional[str] = None) -> list[dict]: raise NotImplementedError
    def insert_trade(self, payload: dict) -> Optional[dict]: raise NotImplementedError
    def update_trade(self, trade_id: int, patch: dict) -> None: raise NotImplementedError
    def all_trades(self) -> list[dict]: raise NotImplementedError


# --------------------------------------------------------------------------- #
# Supabase / PostgREST
# --------------------------------------------------------------------------- #
class PostgrestStore(Store):
    def __init__(self, url: str, key: str, timeout: float = 30.0):
        if not url or not key:
            raise ValueError("PostgrestStore needs a Supabase URL and key.")
        self.base = url.rstrip("/") + "/rest/v1"
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        })

    # -- low-level ----------------------------------------------------------
    def _req(self, method: str, table: str, *, params=None, body=None, prefer=None):
        headers = {}
        if prefer:
            headers["Prefer"] = prefer
        r = self.session.request(
            method, f"{self.base}/{table}",
            params=params,
            data=json.dumps(body) if body is not None else None,
            headers=headers, timeout=self.timeout,
        )
        if not r.ok:
            raise RuntimeError(f"{method} {table} -> {r.status_code}: {r.text[:300]}")
        if r.status_code == 204 or not r.text:
            return None
        return r.json()

    def _insert(self, table: str, rows, *, ignore_conflict=False, return_rows=True):
        prefer = []
        if return_rows:
            prefer.append("return=representation")
        if ignore_conflict:
            prefer.append("resolution=ignore-duplicates")
        return self._req("POST", table, body=rows, prefer=",".join(prefer) or None)

    def _select(self, table: str, *, select="*", order=None, limit=None, **filters):
        params = {"select": select}
        for k, v in filters.items():
            params[k] = v  # caller passes PostgREST ops, e.g. status="eq.OPEN"
        if order:
            params["order"] = order
        if limit:
            params["limit"] = str(limit)
        return self._req("GET", table, params=params) or []

    def _update(self, table: str, patch: dict, **filters):
        self._req("PATCH", table, params=filters, body=patch, prefer="return=minimal")

    # -- config -------------------------------------------------------------
    def latest_config(self):
        rows = self._select("config_history", order="id.desc", limit=1)
        return rows[0] if rows else None

    def insert_config(self, payload):
        out = self._insert("config_history", payload)
        return out[0] if isinstance(out, list) and out else out

    def config_history(self, limit=50):
        return self._select("config_history", order="id.desc", limit=limit)

    # -- cycles -------------------------------------------------------------
    def create_cycle(self, payload):
        out = self._insert("cycles", payload)
        return out[0] if isinstance(out, list) and out else out

    def update_cycle(self, cycle_id, patch):
        self._update("cycles", patch, id=f"eq.{cycle_id}")

    # -- snapshots / observations -------------------------------------------
    def insert_leaderboard(self, rows):
        if rows:
            self._insert("leaderboard_snapshots", rows, return_rows=False)

    def insert_observations(self, rows):
        if rows:
            self._insert("observations", rows, return_rows=False)

    def latest_observations(self, limit=500):
        # newest cycle's observations (dashboard "recent signals")
        cyc = self._select("cycles", select="id", order="id.desc", limit=1)
        if not cyc:
            return []
        return self._select("observations", order="overlap.desc",
                             cycle_id=f"eq.{cyc[0]['id']}", limit=limit)

    def latest_leaderboard(self):
        cyc = self._select("cycles", select="id", order="id.desc", limit=1)
        if not cyc:
            return []
        return self._select("leaderboard_snapshots", order="rank.asc",
                             cycle_id=f"eq.{cyc[0]['id']}")

    # -- paper trades -------------------------------------------------------
    def open_trades(self, strategy=None):
        f = {"status": "eq.OPEN"}
        if strategy:
            f["strategy"] = f"eq.{strategy}"
        return self._select("paper_trades", **f)

    def insert_trade(self, payload):
        try:
            out = self._insert("paper_trades", payload)
            return out[0] if isinstance(out, list) and out else out
        except RuntimeError as e:
            # 409 = the partial-unique index blocked a duplicate OPEN trade. Skip.
            if "409" in str(e) or "duplicate" in str(e).lower():
                return None
            raise

    def update_trade(self, trade_id, patch):
        patch = {**patch, "updated_at": _now_iso()}
        self._update("paper_trades", patch, id=f"eq.{trade_id}")

    def all_trades(self):
        return self._select("paper_trades", order="id.asc")


# --------------------------------------------------------------------------- #
# In-memory (dry-run + tests)
# --------------------------------------------------------------------------- #
class MemoryStore(Store):
    def __init__(self):
        self._config: list[dict] = []
        self._cycles: list[dict] = []
        self._leaderboard: list[dict] = []
        self._observations: list[dict] = []
        self._trades: list[dict] = []
        self._seq = {"config": 0, "cycle": 0, "trade": 0}

    def _next(self, k):
        self._seq[k] += 1
        return self._seq[k]

    # config
    def latest_config(self):
        return dict(self._config[-1]) if self._config else None

    def insert_config(self, payload):
        row = {**payload, "id": self._next("config"), "created_at": _now_iso()}
        self._config.append(row)
        return dict(row)

    def config_history(self, limit=50):
        return [dict(r) for r in reversed(self._config[-limit:])]

    # cycles
    def create_cycle(self, payload):
        row = {**payload, "id": self._next("cycle"), "run_at": _now_iso()}
        self._cycles.append(row)
        return dict(row)

    def update_cycle(self, cycle_id, patch):
        for c in self._cycles:
            if c["id"] == cycle_id:
                c.update(patch)

    # snapshots / observations
    def insert_leaderboard(self, rows):
        self._leaderboard.extend(dict(r) for r in rows)

    def insert_observations(self, rows):
        self._observations.extend(dict(r) for r in rows)

    def latest_observations(self, limit=500):
        if not self._cycles:
            return []
        cid = self._cycles[-1]["id"]
        obs = [dict(o) for o in self._observations if o.get("cycle_id") == cid]
        obs.sort(key=lambda o: o.get("overlap", 0), reverse=True)
        return obs[:limit]

    def latest_leaderboard(self):
        if not self._cycles:
            return []
        cid = self._cycles[-1]["id"]
        rows = [dict(r) for r in self._leaderboard if r.get("cycle_id") == cid]
        rows.sort(key=lambda r: r.get("rank", 999))
        return rows

    # paper trades
    def open_trades(self, strategy=None):
        return [dict(t) for t in self._trades
                if t["status"] == "OPEN" and (strategy is None or t["strategy"] == strategy)]

    def insert_trade(self, payload):
        # enforce the same partial-unique invariant the DB does
        for t in self._trades:
            if (t["status"] == "OPEN" and t["strategy"] == payload["strategy"]
                    and t["condition_id"] == payload["condition_id"]
                    and t["outcome_index"] == payload["outcome_index"]):
                return None
        row = {**payload, "id": self._next("trade"),
               "created_at": _now_iso(), "updated_at": _now_iso()}
        self._trades.append(row)
        return dict(row)

    def update_trade(self, trade_id, patch):
        for t in self._trades:
            if t["id"] == trade_id:
                t.update(patch)
                t["updated_at"] = _now_iso()

    def all_trades(self):
        return [dict(t) for t in self._trades]
