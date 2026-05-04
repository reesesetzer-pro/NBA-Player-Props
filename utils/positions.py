"""utils/positions.py — disk-cached player position lookup.

Maps player_id → 5-position role (PG/SG/SF/PF/C) using NBA Stats commonplayerinfo,
backed by a local JSON cache so we don't hit the API on every run. The daily
pos_def_sync warms the cache; edge_engine reads from it.

Before this module, edge_engine hardcoded every player's position to "SG", so
every matchup multiplier looked up `(opp_team, "SG", stat)` regardless of whom
was actually playing. That made the matchup adjustment effectively noise.
"""
from __future__ import annotations
import json
import time
from pathlib import Path
from typing import Optional

_CACHE_PATH = Path(__file__).resolve().parent.parent / ".positions_cache.json"
_position_cache: Optional[dict[str, str]] = None


def _load_cache() -> dict[str, str]:
    global _position_cache
    if _position_cache is None:
        try:
            with open(_CACHE_PATH) as f:
                _position_cache = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            _position_cache = {}
    return _position_cache


def _save_cache() -> None:
    if _position_cache is None:
        return
    try:
        with open(_CACHE_PATH, "w") as f:
            json.dump(_position_cache, f)
    except OSError:
        pass


def _parse_height_inches(height: str) -> Optional[int]:
    """Parse NBA Stats height format like '6-9' → 81 inches. None if unparseable."""
    if not height or "-" not in str(height):
        return None
    try:
        ft, inch = str(height).split("-")
        return int(ft) * 12 + int(inch)
    except (ValueError, IndexError):
        return None


def _normalize_position(pos: str, height: Optional[str] = None) -> str:
    """Map commonplayerinfo's POSITION field to the 5-position framework.

    The API returns full words ('Guard', 'Forward', 'Center') or hyphenated
    combos ('Forward-Guard'), not abbreviations like 'F' or 'G'. Since the
    primary descriptor doesn't distinguish PG vs SG or SF vs PF, we fall back
    to height to break ties:
        Guard:   < 6'4" → PG, ≥ 6'4" → SG
        Forward: < 6'9" → SF, ≥ 6'9" → PF
    Without height, defaults to SG / SF respectively.
    """
    pos_u = (pos or "").upper().strip()
    if not pos_u:
        return "SF"
    primary = pos_u.split("-")[0].strip()
    h_in = _parse_height_inches(height)

    if primary in ("GUARD", "G"):
        return "SG" if (h_in is not None and h_in >= 76) else "PG"
    if primary in ("FORWARD", "F"):
        return "PF" if (h_in is not None and h_in >= 81) else "SF"
    if primary in ("CENTER", "C"):
        return "C"
    return {"PG": "PG", "SG": "SG", "SF": "SF", "PF": "PF"}.get(primary, "SF")


def get_position(player_id: int, *, fetch_if_missing: bool = True) -> str:
    """Return the player's normalized position (PG/SG/SF/PF/C).

    Cache-first; only hits commonplayerinfo when missing AND
    `fetch_if_missing=True`. Returns 'SF' as the safe default.
    """
    if not player_id:
        return "SF"
    cache = _load_cache()
    key = str(player_id)
    if key in cache:
        return cache[key]
    if not fetch_if_missing:
        return "SF"
    try:
        from nba_api.stats.endpoints import commonplayerinfo
        df = commonplayerinfo.CommonPlayerInfo(player_id=player_id).get_data_frames()[0]
        pos_raw = str(df.iloc[0].get("POSITION", ""))
        height  = str(df.iloc[0].get("HEIGHT", ""))
        pos = _normalize_position(pos_raw, height)
        cache[key] = pos
        _save_cache()
        time.sleep(0.6)
        return pos
    except Exception:
        return "SF"


def bulk_get_positions(player_ids: list[int], *, fetch_missing: bool = True) -> dict[int, str]:
    """Return {player_id: position}. Disk-cache hits are free; misses optionally
    fetch from the API (rate-limited at 0.6s/call).
    """
    cache = _load_cache()
    result: dict[int, str] = {}
    for pid in player_ids:
        if not pid:
            continue
        pid_int = int(pid)
        key = str(pid_int)
        if key in cache:
            result[pid_int] = cache[key]
        elif fetch_missing:
            result[pid_int] = get_position(pid_int, fetch_if_missing=True)
        else:
            result[pid_int] = "SF"
    return result
