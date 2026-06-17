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
import os
import re
from datetime import datetime, timezone
from typing import Optional

import requests


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _missing_column(msg: str) -> Optional[str]:
    """PostgREST PGRST204 names a column that doesn't exist; pull it out so the
    caller can drop that field and retry (tolerates forward-only schema drift)."""
    m = re.search(r"Could not find the '([^']+)' column", msg or "")
    return m.group(1) if m else None


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

    # per-trader snapshot for the dashboard — optional; default no-op
    def set_traders(self, rows: list[dict]) -> None: return None
    def latest_traders(self) -> list[dict]: return []

    # time-series + accumulated trackers (dashboard charts) — optional; default no-op
    def append_history(self, rec: dict) -> None: return None
    def history(self, limit: int = 1000) -> list[dict]: return []
    def get_consensus_watch(self) -> dict: return {}
    def set_consensus_watch(self, d: dict) -> None: return None
    def get_trader_series(self) -> dict: return {}
    def set_trader_series(self, d: dict) -> None: return None
    def set_agreement(self, d: dict) -> None: return None
    def get_agreement(self) -> dict: return {}
    # health heartbeat (sustained-failure detection) — optional; default no-op
    def get_health(self) -> dict: return {}
    def set_health(self, d: dict) -> None: return None
    # last-good Polymarket-US catalog (so US tagging survives a transient fetch
    # failure instead of blanking the view) — optional; default no-op
    def get_us_catalog(self) -> dict: return {}
    def set_us_catalog(self, d: dict) -> None: return None
    # user-tuned wallet/account policy (dashboard Settings) — optional; default no-op
    def get_wallet_config(self) -> dict: return {}
    def set_wallet_config(self, d: dict) -> None: return None
    # cohort membership clock {wallet: last_qualified_iso} for the stability/grace rule
    def get_cohort_state(self) -> dict: return {}
    def set_cohort_state(self, d: dict) -> None: return None


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
        # Tolerate schema drift: if a column doesn't exist yet (PGRST204), drop it
        # and retry, so a newly-added config field never 400-crashes the poller
        # (those fields then fall back to the dataclass default on read).
        row = dict(payload)
        for _ in range(8):
            try:
                out = self._insert("config_history", row)
                return out[0] if isinstance(out, list) and out else out
            except RuntimeError as e:
                col = _missing_column(str(e))
                if col and col in row:
                    row.pop(col)
                    continue
                raise
        out = self._insert("config_history", row)
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
        # Tolerate forward schema drift the same way insert_config does: if a new
        # column (end_date/peak_price) isn't migrated yet, drop it and retry so the
        # trade still records (that feature just stays dormant until the ALTER runs).
        row = dict(payload)
        for _ in range(8):
            try:
                out = self._insert("paper_trades", row)
                return out[0] if isinstance(out, list) and out else out
            except RuntimeError as e:
                s = str(e)
                # 409 = the partial-unique index blocked a duplicate OPEN trade. Skip.
                if "409" in s or "duplicate" in s.lower():
                    return None
                col = _missing_column(s)
                if col and col in row:
                    row.pop(col)
                    continue
                raise
        return None

    def update_trade(self, trade_id, patch):
        patch = {**patch, "updated_at": _now_iso()}
        for _ in range(8):
            try:
                self._update("paper_trades", patch, id=f"eq.{trade_id}")
                return
            except RuntimeError as e:
                col = _missing_column(str(e))
                if col and col in patch:
                    patch.pop(col)
                    continue
                raise

    def all_trades(self):
        # PostgREST caps a single response (Supabase default 1000 rows). The
        # high-churn control benchmark alone blows past 1000, so a single GET was
        # silently returning only the OLDEST 1000 trades (order=id.asc) — which
        # truncated every realized stat and hid newer consensus closes from the
        # dashboard. Page through so the metrics/tables see EVERY trade.
        out, offset, page = [], 0, 1000
        while True:
            rows = self._select("paper_trades", order="id.asc", limit=page, offset=str(offset))
            out.extend(rows)
            if len(rows) < page or len(out) >= 100000:   # stop at the last partial page (hard safety bound)
                break
            offset += page
        return out

    def last_cycle(self):
        rows = self._select("cycles", order="id.desc", limit=1)
        return rows[0] if rows else None

    # -- accumulated trackers (kv_store JSON blobs) -------------------------
    def _kv_get(self, key, default):
        rows = self._select("kv_store", select="value", key=f"eq.{key}", limit=1)
        return rows[0]["value"] if rows else default

    def _kv_set(self, key, value):
        # PostgREST upsert on the primary key (`key`)
        self._req("POST", "kv_store",
                  body={"key": key, "value": value, "updated_at": _now_iso()},
                  prefer="resolution=merge-duplicates,return=minimal")

    def set_traders(self, rows):
        self._kv_set("latest_traders", [dict(r) for r in rows])

    def latest_traders(self):
        return self._kv_get("latest_traders", [])

    def append_history(self, rec):
        h = self._kv_get("history", [])
        h.append(rec)
        if len(h) > 1500:
            h = h[-1500:]
        self._kv_set("history", h)

    def history(self, limit=1000):
        return self._kv_get("history", [])[-limit:]

    def get_consensus_watch(self):
        return self._kv_get("consensus_watch", {})

    def set_consensus_watch(self, d):
        self._kv_set("consensus_watch", d)

    def get_trader_series(self):
        return self._kv_get("trader_series", {})

    def set_trader_series(self, d):
        self._kv_set("trader_series", d)

    def set_agreement(self, d):
        self._kv_set("agreement", d)

    def get_agreement(self):
        return self._kv_get("agreement", {})

    def get_health(self):
        return self._kv_get("health", {})

    def set_health(self, d):
        self._kv_set("health", d)

    def get_us_catalog(self):
        return self._kv_get("us_catalog", {})

    def set_us_catalog(self, d):
        self._kv_set("us_catalog", d)

    def get_wallet_config(self):
        return self._kv_get("wallet_config", {})

    def set_wallet_config(self, d):
        self._kv_set("wallet_config", d)

    def get_cohort_state(self):
        return self._kv_get("cohort_state", {})

    def set_cohort_state(self, d):
        self._kv_set("cohort_state", d)


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
        self._history: list[dict] = []
        self._watch: dict = {}
        self._tseries: dict = {}
        self._agreement: dict = {}
        self._cohort_state: dict = {}
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

    def last_cycle(self):
        return dict(self._cycles[-1]) if self._cycles else None

    def set_traders(self, rows):
        self._traders_snap = [dict(r) for r in rows]

    def latest_traders(self):
        return [dict(r) for r in getattr(self, "_traders_snap", [])]

    def append_history(self, rec):
        self._history.append(rec)

    def history(self, limit=1000):
        return [dict(r) for r in self._history[-limit:]]

    def get_consensus_watch(self):
        return dict(self._watch)

    def set_consensus_watch(self, d):
        self._watch = d

    def get_trader_series(self):
        return dict(self._tseries)

    def set_trader_series(self, d):
        self._tseries = d

    def set_agreement(self, d):
        self._agreement = d

    def get_agreement(self):
        return dict(self._agreement)

    def get_cohort_state(self):
        return dict(self._cohort_state)

    def set_cohort_state(self, d):
        self._cohort_state = dict(d)


# --------------------------------------------------------------------------- #
# File-backed (the $0 GitHub-Pages deployment — no database at all)
# --------------------------------------------------------------------------- #
class FileStore(Store):
    """
    Persists everything to JSON files under `data_dir`. The GitHub Actions
    poller commits these files back to the repo each cycle; a static dashboard
    reads a precomputed payload. No hosted database, no secrets.

      data/state.json          mutable source of truth (trades, config, counters)
      data/observations.jsonl  append-only log of EVERY observation (honesty rule #2)
      data/leaderboard.jsonl   append-only leaderboard snapshots
    """

    def __init__(self, data_dir: str = "data"):
        self.dir = data_dir
        os.makedirs(self.dir, exist_ok=True)
        self.state_path = os.path.join(self.dir, "state.json")
        self.obs_path = os.path.join(self.dir, "observations.jsonl")
        self.lb_path = os.path.join(self.dir, "leaderboard.jsonl")
        self.hist_path = os.path.join(self.dir, "history.jsonl")
        self._state = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.state_path):
            with open(self.state_path) as fh:
                return json.load(fh)
        return {
            "seq": {"config": 0, "cycle": 0, "trade": 0},
            "config_history": [], "paper_trades": [], "last_cycle": None,
            "latest_observations": [], "latest_leaderboard": [], "latest_traders": [],
            "history": [], "consensus_watch": {}, "trader_series": {}, "agreement": {},
        }

    def _save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self._state, fh, indent=2, default=str)
        os.replace(tmp, self.state_path)  # atomic

    def _append(self, path, rows, max_lines: Optional[int] = None):
        with open(path, "a") as fh:
            for r in rows:
                fh.write(json.dumps(r, default=str) + "\n")
        if max_lines:
            self._rotate(path, max_lines)

    def _rotate(self, path, max_lines, size_gate: int = 3_000_000):
        """Keep an append-only log bounded: once it's sizeable, trim to the most
        recent `max_lines`. The size gate keeps the common (small-file) path cheap."""
        try:
            if os.path.getsize(path) < size_gate:
                return
            with open(path) as fh:
                lines = fh.readlines()
            if len(lines) <= max_lines:
                return
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                fh.writelines(lines[-max_lines:])
            os.replace(tmp, path)
        except OSError:
            pass

    def _next(self, k):
        self._state["seq"][k] += 1
        return self._state["seq"][k]

    # config
    def latest_config(self):
        ch = self._state["config_history"]
        return dict(ch[-1]) if ch else None

    def insert_config(self, payload):
        row = {**payload, "id": self._next("config"), "created_at": _now_iso()}
        self._state["config_history"].append(row)
        self._save()
        return dict(row)

    def config_history(self, limit=50):
        return [dict(r) for r in reversed(self._state["config_history"][-limit:])]

    # cycles
    def create_cycle(self, payload):
        row = {**payload, "id": self._next("cycle"), "run_at": _now_iso()}
        self._state["last_cycle"] = row
        self._save()
        return dict(row)

    def update_cycle(self, cycle_id, patch):
        lc = self._state.get("last_cycle")
        if lc and lc["id"] == cycle_id:
            lc.update(patch)
            self._save()

    # snapshots / observations
    def insert_leaderboard(self, rows):
        rows = [dict(r) for r in rows]
        self._state["latest_leaderboard"] = rows
        self._append(self.lb_path, rows, max_lines=20000)
        self._save()

    def insert_observations(self, rows, snapshot_cap: int = 250):
        rows = [dict(r) for r in rows]
        # dashboard snapshot: keep the most-agreed positions (bounded for payload size)
        snap = sorted(rows, key=lambda o: o.get("overlap", 0), reverse=True)[:snapshot_cap]
        self._state["latest_observations"] = snap
        # persistent empirical record: only consensus (>=2) so the file stays bounded at scale
        self._append(self.obs_path, [r for r in rows if (r.get("overlap") or 0) >= 2], max_lines=60000)
        self._save()

    def latest_observations(self, limit=500):
        obs = sorted(self._state.get("latest_observations", []),
                     key=lambda o: o.get("overlap", 0), reverse=True)
        return [dict(o) for o in obs[:limit]]

    def latest_leaderboard(self):
        return [dict(r) for r in sorted(self._state.get("latest_leaderboard", []),
                                        key=lambda r: r.get("rank", 999))]

    # paper trades
    def open_trades(self, strategy=None):
        return [dict(t) for t in self._state["paper_trades"]
                if t["status"] == "OPEN" and (strategy is None or t["strategy"] == strategy)]

    def insert_trade(self, payload):
        for t in self._state["paper_trades"]:
            if (t["status"] == "OPEN" and t["strategy"] == payload["strategy"]
                    and t["condition_id"] == payload["condition_id"]
                    and t["outcome_index"] == payload["outcome_index"]):
                return None
        row = {**payload, "id": self._next("trade"),
               "created_at": _now_iso(), "updated_at": _now_iso()}
        self._state["paper_trades"].append(row)
        self._save()
        return dict(row)

    def update_trade(self, trade_id, patch):
        for t in self._state["paper_trades"]:
            if t["id"] == trade_id:
                t.update(patch)
                t["updated_at"] = _now_iso()
                self._save()

    def all_trades(self):
        return [dict(t) for t in self._state["paper_trades"]]

    def last_cycle(self):
        lc = self._state.get("last_cycle")
        return dict(lc) if lc else None

    def set_traders(self, rows):
        self._state["latest_traders"] = [dict(r) for r in rows]
        self._save()

    def latest_traders(self):
        return [dict(r) for r in self._state.get("latest_traders", [])]

    def append_history(self, rec):
        h = self._state.setdefault("history", [])
        h.append(rec)
        if len(h) > 1500:
            del h[:len(h) - 1500]
        self._append(self.hist_path, [rec], max_lines=20000)
        self._save()

    def history(self, limit=1000):
        return [dict(r) for r in self._state.get("history", [])[-limit:]]

    def get_consensus_watch(self):
        return dict(self._state.get("consensus_watch", {}))

    def set_consensus_watch(self, d):
        self._state["consensus_watch"] = d
        self._save()

    def get_trader_series(self):
        return dict(self._state.get("trader_series", {}))

    def set_trader_series(self, d):
        self._state["trader_series"] = d
        self._save()

    def set_agreement(self, d):
        self._state["agreement"] = d
        self._save()

    def get_agreement(self):
        return dict(self._state.get("agreement", {}))

    def get_health(self):
        return dict(self._state.get("health", {}))

    def set_health(self, d):
        self._state["health"] = d
        self._save()

    def get_us_catalog(self):
        return dict(self._state.get("us_catalog", {}))

    def set_us_catalog(self, d):
        self._state["us_catalog"] = d
        self._save()

    def get_wallet_config(self):
        return dict(self._state.get("wallet_config", {}))

    def set_wallet_config(self, d):
        self._state["wallet_config"] = d

    def get_cohort_state(self):
        return dict(self._state.get("cohort_state", {}))

    def set_cohort_state(self, d):
        self._state["cohort_state"] = d
        self._save()
