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
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _missing_column(msg: str) -> Optional[str]:
    """PostgREST PGRST204 names a column that doesn't exist; pull it out so the
    caller can drop that field and retry (tolerates forward-only schema drift)."""
    m = re.search(r"Could not find the '([^']+)' column", msg or "")
    return m.group(1) if m else None


# close reasons that mean WE bailed on an adverse price move (vs the cohort selling
# or the market resolving) — these arm the re-entry cooldown so we don't re-buy a
# market we just stopped out of while it's still falling.
_STOP_REASONS = ("stop_loss", "trailing_stop", "time_stop")

# The durable `observations` table keeps only CONSENSUS rows (overlap>=2). The
# dashboard's signal tabs instead read a bounded latest-cycle snapshot (all
# overlaps, strongest first) held in kv_store, so single-wallet rows still show
# without the table ballooning. This mirrors FileStore's split (snapshot + log).
_OBS_SNAPSHOT_CAP = 400


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
    # (strategy, condition_id, outcome_index) keys we stopped out of since `since_iso`,
    # used for the re-entry cooldown (don't re-buy a market we just bailed on).
    def recently_stopped(self, since_iso: str, reasons=None) -> set: return set()
    # mark MANY open trades in one shot (each row needs at least an 'id'). Default
    # loops; PostgrestStore overrides with a single bulk upsert so the follow-all
    # control's ~580 positions don't cost ~580 sequential writes per cycle.
    def update_trades_bulk(self, rows: list) -> None:
        for r in rows or []:
            self.update_trade(r["id"], {k: v for k, v in r.items() if k != "id"})

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
    # free-tier disk guard: delete bulky time-series rows (observations / leaderboard
    # snapshots) older than the retention window. NEVER touches paper_trades or
    # kv_store (the durable track record + analytics) — optional; default no-op.
    def prune_snapshots(self, retain_hours: int = 48) -> dict: return {}
    # the precomputed data.json blob served by the Worker (D1 deployment) — default no-op
    def set_site_blob(self, body: str, name: str = "data.json") -> None: return None


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

    def _delete(self, table: str, **filters):
        # caller passes PostgREST ops, e.g. observed_at="lt.2026-06-20T00:00:00Z"
        self._req("DELETE", table, params=filters, prefer="return=minimal")

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
        if not rows:
            return
        # Dashboard feed: a bounded snapshot of THIS cycle (all overlaps, strongest
        # first) lives in kv_store — that's what the page's signal tabs read, so
        # single-wallet rows still display without bloating the table. Mirrors
        # FileStore's snapshot. observed_at is stamped to match the table's column.
        ts = _now_iso()
        snap = sorted((dict(r) for r in rows),
                      key=lambda o: o.get("overlap") or 0, reverse=True)[:_OBS_SNAPSHOT_CAP]
        for r in snap:
            r.setdefault("observed_at", ts)
        self._kv_set("obs_snapshot", snap)
        # Durable empirical record: only CONSENSUS (overlap>=2). Single-wallet holds
        # are ~80% of the volume and nothing reads them back historically, so we don't
        # persist them; prune_snapshots() then keeps even the consensus log to ~48h.
        keep = [r for r in rows if (r.get("overlap") or 0) >= 2]
        if keep:
            self._insert("observations", keep, return_rows=False)

    def latest_observations(self, limit=500):
        # the dashboard's signal tabs read the latest-cycle snapshot (all overlaps)
        snap = self._kv_get("obs_snapshot", [])
        snap = sorted(snap, key=lambda o: o.get("overlap") or 0, reverse=True)
        return snap[:limit]

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

    def recently_stopped(self, since_iso, reasons=_STOP_REASONS):
        rows = self._select(
            "paper_trades", select="strategy,condition_id,outcome_index",
            status="eq.CLOSED", close_reason=f"in.({','.join(reasons)})",
            exit_at=f"gte.{since_iso}")
        return {(r["strategy"], r["condition_id"], r["outcome_index"]) for r in rows}

    def update_trades_bulk(self, rows):
        # One upsert on the primary key (id) marks many open trades at once, instead
        # of N sequential PATCHes — without this the ~580-position follow-all control
        # blows the cycle's time budget and the runs pile up / time out. Rows carry
        # the NOT-NULL columns too so the (unused) insert path can't fail validation.
        # Falls back to per-row if the server rejects the bulk upsert.
        if not rows:
            return
        payload = [{**r, "updated_at": _now_iso()} for r in rows]
        for i in range(0, len(payload), 500):
            chunk = payload[i:i + 500]
            try:
                self._req("POST", "paper_trades", body=chunk,
                          prefer="resolution=merge-duplicates,return=minimal")
            except RuntimeError:
                for r in chunk:
                    try:
                        self.update_trade(r["id"], {k: v for k, v in r.items()
                                                    if k not in ("id", "updated_at")})
                    except RuntimeError:
                        pass

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

    # -- retention (free-tier disk guard) -----------------------------------
    def prune_snapshots(self, retain_hours=48):
        """Delete observations + leaderboard snapshots older than the retention
        window so the free-tier database stays well under its size cap. The
        durable record — paper_trades (the track record) and kv_store (analytics,
        consensus watch, the dashboard snapshot) — is NEVER pruned. Self-throttled
        to ~once an hour via a kv timestamp so it's cheap to call every cycle, and
        never raises (a failed prune must not break the poll cycle)."""
        try:
            now = datetime.now(timezone.utc)
            last = self._kv_get("last_prune", None)
            if last:
                try:
                    if (now - datetime.fromisoformat(last)).total_seconds() < 3000:  # ~50 min
                        return {}
                except (ValueError, TypeError):
                    pass
            cutoff = (now - timedelta(hours=retain_hours)).isoformat()
            self._delete("observations", observed_at=f"lt.{cutoff}")
            self._delete("leaderboard_snapshots", captured_at=f"lt.{cutoff}")
            self._kv_set("last_prune", now.isoformat())
            print(f"prune_snapshots: pruned observations/leaderboard older than {retain_hours}h (< {cutoff})")
            return {"cutoff": cutoff}
        except Exception as e:                                # never break the cycle
            print(f"prune_snapshots skipped: {e}")
            return {}


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

    def recently_stopped(self, since_iso, reasons=_STOP_REASONS):
        return {(t["strategy"], t["condition_id"], t["outcome_index"]) for t in self._trades
                if t.get("status") == "CLOSED" and t.get("close_reason") in reasons
                and str(t.get("exit_at") or "") >= since_iso}

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

    def recently_stopped(self, since_iso, reasons=_STOP_REASONS):
        return {(t["strategy"], t["condition_id"], t["outcome_index"])
                for t in self._state["paper_trades"]
                if t.get("status") == "CLOSED" and t.get("close_reason") in reasons
                and str(t.get("exit_at") or "") >= since_iso}

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


# --------------------------------------------------------------------------- #
# Shared kv-backed dashboard trackers (JSON blobs), expressed in terms of
# _kv_get / _kv_set. Mixed into D1Store; PostgrestStore keeps its own copies
# (left untouched so the Supabase fallback path can't regress).
# --------------------------------------------------------------------------- #
class _KvTrackers:
    def set_traders(self, rows): self._kv_set("latest_traders", [dict(r) for r in rows])
    def latest_traders(self): return self._kv_get("latest_traders", [])

    def append_history(self, rec):
        h = self._kv_get("history", [])
        h.append(rec)
        if len(h) > 1500:
            h = h[-1500:]
        self._kv_set("history", h)

    def history(self, limit=1000): return self._kv_get("history", [])[-limit:]
    def get_consensus_watch(self): return self._kv_get("consensus_watch", {})
    def set_consensus_watch(self, d): self._kv_set("consensus_watch", d)
    def get_trader_series(self): return self._kv_get("trader_series", {})
    def set_trader_series(self, d): self._kv_set("trader_series", d)
    def set_agreement(self, d): self._kv_set("agreement", d)
    def get_agreement(self): return self._kv_get("agreement", {})
    def get_health(self): return self._kv_get("health", {})
    def set_health(self, d): self._kv_set("health", d)
    def get_us_catalog(self): return self._kv_get("us_catalog", {})
    def set_us_catalog(self, d): self._kv_set("us_catalog", d)
    def get_wallet_config(self): return self._kv_get("wallet_config", {})
    def set_wallet_config(self, d): self._kv_set("wallet_config", d)
    def get_cohort_state(self): return self._kv_get("cohort_state", {})
    def set_cohort_state(self, d): self._kv_set("cohort_state", d)


# --------------------------------------------------------------------------- #
# Cloudflare D1 (SQLite over the HTTP query API)
#
# Same Store interface as PostgrestStore, but talks to a D1 database via the
# Cloudflare REST query endpoint using only `requests`. This is the Cloudflare-
# native backend: no Supabase, no egress bill (D1 is priced by rows, not
# bandwidth; the Worker serves data.json from `site_blob`). Arrays + kv blobs are
# stored as JSON TEXT; booleans as 0/1; timestamps as ISO strings.
# --------------------------------------------------------------------------- #
class D1Store(_KvTrackers, Store):
    _JSON_COLS = {
        "observations": {"holder_wallets", "holder_usernames", "holder_ranks",
                         "holder_sizes", "holder_avg_prices"},
        "paper_trades": {"holders_at_entry"},
    }
    _BOOL_COLS = {
        "paper_trades": {"resolved_won"},
        "config_history": {"control_respects_guardrails"},
        "observations": {"market_closed", "market_active"},
        "leaderboard_snapshots": {"in_cohort"},
    }

    def __init__(self, account_id: str, database_id: str, token: str, timeout: float = 45.0):
        if not (account_id and database_id and token):
            raise ValueError("D1Store needs account_id, database_id and token.")
        self.url = (f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
                    f"/d1/database/{database_id}/query")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}",
                                     "Content-Type": "application/json"})

    # -- low-level ----------------------------------------------------------
    def _run(self, sql, params=None):
        r = self.session.post(self.url, data=json.dumps({"sql": sql, "params": params or []}),
                              timeout=self.timeout)
        if not r.ok:
            raise RuntimeError(f"D1 {r.status_code}: {r.text[:300]}")
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(f"D1 error: {data.get('errors')}")
        res = (data.get("result") or [{}])[0] or {}
        return res.get("results") or [], res.get("meta") or {}

    @staticmethod
    def _bind(v):
        if isinstance(v, bool):
            return 1 if v else 0
        if isinstance(v, (list, dict)):
            return json.dumps(v, default=str)
        return v

    def _out(self, table, row):
        if not row:
            return row
        for k in self._JSON_COLS.get(table, ()):
            if isinstance(row.get(k), str):
                try:
                    row[k] = json.loads(row[k])
                except (ValueError, TypeError):
                    row[k] = []
        for k in self._BOOL_COLS.get(table, ()):
            if row.get(k) is not None:
                row[k] = bool(row[k])
        return row

    @staticmethod
    def _dropped_col(err, keys):
        m = re.search(r"(?:has no column named|no such column:?)\s+(\w+)", err or "")
        return m.group(1) if (m and m.group(1) in keys) else None

    def _select(self, table, where="", params=None, order="", limit=None, offset=None, cols="*"):
        sql = f"SELECT {cols} FROM {table}"
        if where:
            sql += f" WHERE {where}"
        if order:
            sql += f" ORDER BY {order}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        if offset:
            sql += f" OFFSET {int(offset)}"
        rows, _ = self._run(sql, params)
        return [self._out(table, r) for r in rows]

    def _insert(self, table, row):
        # tolerate forward schema drift the way PostgrestStore does: if a column
        # isn't in the D1 table yet, drop it and retry (feature stays dormant until
        # the ALTER runs); a UNIQUE violation (dup OPEN trade) returns None.
        row = dict(row)
        for _ in range(8):
            cols = list(row.keys())
            qcols = ",".join(f'"{c}"' for c in cols)   # quote identifiers ("window" is a keyword)
            sql = f"INSERT INTO {table} ({qcols}) VALUES ({','.join('?' * len(cols))})"
            try:
                _, meta = self._run(sql, [self._bind(row[c]) for c in cols])
                return {**row, "id": meta.get("last_row_id")}
            except RuntimeError as e:
                s = str(e)
                if "UNIQUE constraint failed" in s:
                    return None
                col = self._dropped_col(s, row)
                if col:
                    row.pop(col)
                    continue
                raise
        return None

    def _update(self, table, patch, where, params):
        patch = dict(patch)
        for _ in range(8):
            cols = list(patch.keys())
            if not cols:
                return
            sets = ",".join(f'"{c}"=?' for c in cols)   # quote identifiers ("window" is a keyword)
            vals = [self._bind(patch[c]) for c in cols] + list(params)
            try:
                self._run(f"UPDATE {table} SET {sets} WHERE {where}", vals)
                return
            except RuntimeError as e:
                col = self._dropped_col(str(e), patch)
                if col:
                    patch.pop(col)
                    continue
                raise

    # -- config -------------------------------------------------------------
    def latest_config(self):
        rows = self._select("config_history", order="id DESC", limit=1)
        return rows[0] if rows else None

    def insert_config(self, payload):
        return self._insert("config_history", payload)

    def config_history(self, limit=50):
        return self._select("config_history", order="id DESC", limit=limit)

    # -- cycles -------------------------------------------------------------
    def create_cycle(self, payload):
        return self._insert("cycles", payload)

    def update_cycle(self, cycle_id, patch):
        self._update("cycles", patch, "id=?", [cycle_id])

    def last_cycle(self):
        rows = self._select("cycles", order="id DESC", limit=1)
        return rows[0] if rows else None

    # -- snapshots / observations -------------------------------------------
    def insert_leaderboard(self, rows):
        for r in rows or []:
            self._insert("leaderboard_snapshots", r)

    def insert_observations(self, rows):
        if not rows:
            return
        ts = _now_iso()
        snap = sorted((dict(r) for r in rows),
                      key=lambda o: o.get("overlap") or 0, reverse=True)[:_OBS_SNAPSHOT_CAP]
        for r in snap:
            r.setdefault("observed_at", ts)
        self._kv_set("obs_snapshot", snap)                 # dashboard feed (all overlaps)
        for r in rows:                                     # durable log: consensus only
            if (r.get("overlap") or 0) >= 2:
                self._insert("observations", r)

    def latest_observations(self, limit=500):
        snap = self._kv_get("obs_snapshot", [])
        snap = sorted(snap, key=lambda o: o.get("overlap") or 0, reverse=True)
        return snap[:limit]

    def latest_leaderboard(self):
        cyc = self._select("cycles", cols="id", order="id DESC", limit=1)
        if not cyc:
            return []
        return self._select("leaderboard_snapshots", where="cycle_id=?",
                            params=[cyc[0]["id"]], order="rank ASC")

    # -- paper trades -------------------------------------------------------
    def open_trades(self, strategy=None):
        if strategy:
            return self._select("paper_trades", where="status='OPEN' AND strategy=?", params=[strategy])
        return self._select("paper_trades", where="status='OPEN'")

    def insert_trade(self, payload):
        return self._insert("paper_trades", payload)

    def update_trade(self, trade_id, patch):
        self._update("paper_trades", {**patch, "updated_at": _now_iso()}, "id=?", [trade_id])

    def all_trades(self):
        out, offset, page = [], 0, 1000
        while True:
            rows = self._select("paper_trades", order="id ASC", limit=page, offset=offset)
            out.extend(rows)
            if len(rows) < page or len(out) >= 100000:
                break
            offset += page
        return out

    def recently_stopped(self, since_iso, reasons=_STOP_REASONS):
        ph = ",".join("?" * len(reasons))
        rows = self._select("paper_trades", cols="strategy,condition_id,outcome_index",
                            where=f"status='CLOSED' AND close_reason IN ({ph}) AND exit_at>=?",
                            params=[*reasons, since_iso])
        return {(r["strategy"], r["condition_id"], r["outcome_index"]) for r in rows}

    # -- kv blobs -----------------------------------------------------------
    def _kv_get(self, key, default):
        rows, _ = self._run("SELECT value FROM kv_store WHERE key=? LIMIT 1", [key])
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except (ValueError, TypeError):
            return default

    def _kv_set(self, key, value):
        self._run("INSERT INTO kv_store (key,value,updated_at) VALUES (?,?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                  [key, json.dumps(value, default=str), _now_iso()])

    # -- data.json blob (the Worker serves this at /data.json) --------------
    def set_site_blob(self, body, name="data.json"):
        self._run("INSERT INTO site_blob (name,body,updated_at) VALUES (?,?,?) "
                  "ON CONFLICT(name) DO UPDATE SET body=excluded.body, updated_at=excluded.updated_at",
                  [name, body, _now_iso()])

    # -- retention ----------------------------------------------------------
    def prune_snapshots(self, retain_hours=48):
        try:
            now = datetime.now(timezone.utc)
            last = self._kv_get("last_prune", None)
            if last:
                try:
                    if (now - datetime.fromisoformat(last)).total_seconds() < 3000:
                        return {}
                except (ValueError, TypeError):
                    pass
            cutoff = (now - timedelta(hours=retain_hours)).isoformat()
            self._run("DELETE FROM observations WHERE observed_at < ?", [cutoff])
            self._run("DELETE FROM leaderboard_snapshots WHERE captured_at < ?", [cutoff])
            self._kv_set("last_prune", now.isoformat())
            return {"cutoff": cutoff}
        except Exception as e:
            print(f"prune_snapshots skipped: {e}")
            return {}
