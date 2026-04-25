"""
utils/db.py — Supabase client + paginated fetch helpers.

NBA tables are prefixed `nba_` to avoid collision with NHL/Golf tables in the
shared Supabase project.
"""
from __future__ import annotations
from typing import Optional, List, Dict, Any
import pandas as pd
from supabase import create_client, Client

from config import SUPABASE_URL, SUPABASE_KEY

_client: Optional[Client] = None


def get_client() -> Client:
    global _client
    if _client is None:
        if not (SUPABASE_URL and SUPABASE_KEY):
            raise RuntimeError("SUPABASE_URL / SUPABASE_KEY missing — set them in .env")
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client


def _clean_value(v):
    """Make one value JSON-safe: NaN/Inf → None (PostgREST rejects them)."""
    if v is None:
        return None
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            return None
    return v


def _clean_row(row: Dict) -> Dict:
    return {k: _clean_value(v) for k, v in row.items()}


def upsert(table: str, rows: List[Dict], on_conflict: str = "id", chunk: int = 500) -> None:
    """Upsert in chunks. Cleans NaN/Inf floats and dedupes within each batch
    by the conflict key (Postgres rejects upserts with duplicate conflict
    targets in a single statement)."""
    if not rows:
        return
    cleaned = [_clean_row(r) for r in rows]
    # Dedupe by conflict column — last occurrence wins (most-recently-fetched)
    if on_conflict and "," not in on_conflict:
        seen: Dict = {}
        for r in cleaned:
            seen[r.get(on_conflict)] = r
        cleaned = list(seen.values())
    client = get_client()
    for i in range(0, len(cleaned), chunk):
        client.table(table).upsert(cleaned[i:i + chunk], on_conflict=on_conflict).execute()


def fetch(table: str, filters: Optional[Dict] = None, limit: int = 1000) -> pd.DataFrame:
    """Single-page fetch (max 1000 by default to stay under Supabase row cap)."""
    q = get_client().table(table).select("*").limit(limit)
    if filters:
        for col, val in filters.items():
            q = q.eq(col, val)
    resp = q.execute()
    return pd.DataFrame(resp.data) if resp.data else pd.DataFrame()


def fetch_in(table: str, col: str, values: List[Any]) -> pd.DataFrame:
    """Fetch all rows where `col` is in `values`. Avoids the silent 1000-row cap
    by scoping the query — use this when you have a known set of IDs."""
    if not values:
        return pd.DataFrame()
    resp = get_client().table(table).select("*").in_(col, values).execute()
    return pd.DataFrame(resp.data) if resp.data else pd.DataFrame()


def fetch_all(table: str, filters: Optional[Dict] = None, page: int = 1000) -> pd.DataFrame:
    """Paginated full-table fetch. Use sparingly — prefer fetch_in when possible."""
    client = get_client()
    out: List[Dict] = []
    offset = 0
    while True:
        q = client.table(table).select("*").range(offset, offset + page - 1)
        if filters:
            for col, val in filters.items():
                q = q.eq(col, val)
        resp = q.execute()
        rows = resp.data or []
        out.extend(rows)
        if len(rows) < page:
            break
        offset += page
    return pd.DataFrame(out) if out else pd.DataFrame()
