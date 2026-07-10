"""Download Ranked Flex data to SQLite without discarding any Riot API fields."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

DATABASE = Path("data/riot_cache.sqlite3")
ROSTER = Path("players.txt")
QUEUE_ID = 440
PLATFORM = os.getenv("RIOT_PLATFORM", "euw1")
ROUTES = {"euw1": "europe", "eun1": "europe", "na1": "americas", "kr": "asia", "jp1": "asia", "oc1": "sea"}
REQUESTS: list[float] = []


def connect() -> sqlite3.Connection:
    DATABASE.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DATABASE)
    con.execute("""CREATE TABLE IF NOT EXISTS riot_payloads (
        kind TEXT NOT NULL, resource_id TEXT NOT NULL, payload TEXT NOT NULL,
        fetched_at TEXT NOT NULL, PRIMARY KEY(kind, resource_id)
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS tracked_players (
        riot_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, puuid TEXT,
        updated_at TEXT NOT NULL
    )""")
    return con


def roster() -> list[tuple[str, str]]:
    values = []
    for line in ROSTER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        accounts, _, label = line.partition("|")
        account_values = [account.strip() for account in accounts.split(",") if account.strip()]
        if not account_values or any("#" not in account for account in account_values):
            raise ValueError(f"Invalid roster entry: {line}. Use RiotID#TAG[, Alternate#TAG] | Display name")
        display_name = label.strip() or account_values[0].split("#", 1)[0].strip()
        values.extend((account, display_name) for account in account_values)
    if len(values) < 2:
        raise ValueError("Add at least two player Riot IDs to players.txt.")
    return values


def limit() -> None:
    global REQUESTS
    now = time.monotonic()
    REQUESTS = [item for item in REQUESTS if now - item < 120]
    last_second = [item for item in REQUESTS if now - item < 1]
    waits = ([1.05 - (now - last_second[0])] if len(last_second) >= 18 else []) + ([120.25 - (now - REQUESTS[0])] if len(REQUESTS) >= 90 else [])
    if waits:
        time.sleep(max(waits))
        limit()
    else:
        REQUESTS.append(now)


def key() -> str:
    secret_file = Path(".streamlit/secrets.toml")
    if secret_file.exists():
        for line in secret_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("RIOT_API_KEY"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return os.getenv("RIOT_API_KEY", "")


def get(url: str, api_key: str) -> object:
    for attempt in range(4):
        limit()
        response = requests.get(url, headers={"X-Riot-Token": api_key}, timeout=30)
        if response.status_code == 429 and attempt < 3:
            time.sleep(float(response.headers.get("Retry-After", 5)))
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError("Riot rate-limit retries exhausted")


def put(con: sqlite3.Connection, kind: str, resource_id: str, payload: object) -> None:
    con.execute("INSERT OR REPLACE INTO riot_payloads VALUES (?, ?, ?, ?)", (kind, resource_id, json.dumps(payload), datetime.now(timezone.utc).isoformat()))


def cached(con: sqlite3.Connection, kind: str, resource_id: str, max_age: timedelta | None) -> object | None:
    row = con.execute("SELECT payload, fetched_at FROM riot_payloads WHERE kind=? AND resource_id=?", (kind, resource_id)).fetchone()
    if not row or (max_age and datetime.now(timezone.utc) - datetime.fromisoformat(row[1]) > max_age):
        return None
    return json.loads(row[0])


def retrieve(con: sqlite3.Connection, kind: str, resource_id: str, url: str, api_key: str, max_age: timedelta | None) -> object:
    result = cached(con, kind, resource_id, max_age)
    if result is None:
        result = get(url, api_key)
        put(con, kind, resource_id, result)
        con.commit()
    return result


def main() -> None:
    api_key = key()
    if not api_key:
        raise RuntimeError("Set RIOT_API_KEY in .streamlit/secrets.toml or your environment.")
    regional = ROUTES[PLATFORM]
    match_count = int(os.getenv("FLEX_MATCH_COUNT", "100"))
    with connect() as con:
        ids: set[str] = set()
        for riot_id, name in roster():
            game_name, tag = riot_id.split("#", 1)
            account = retrieve(con, "account", riot_id.casefold(), f"https://{regional}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{quote(game_name, safe='')}/{quote(tag, safe='')}", api_key, timedelta(days=30))
            puuid = account["puuid"]
            con.execute("INSERT OR REPLACE INTO tracked_players VALUES (?, ?, ?, ?)", (riot_id, name, puuid, datetime.now(timezone.utc).isoformat()))
            retrieve(con, "summoner", puuid, f"https://{PLATFORM}.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{puuid}", api_key, timedelta(days=30))
            retrieve(con, "rank", puuid, f"https://{PLATFORM}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}", api_key, timedelta(minutes=30))
            match_ids = retrieve(con, "match_ids", f"{puuid}:{match_count}", f"https://{regional}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?queue={QUEUE_ID}&start=0&count={match_count}", api_key, timedelta(minutes=15))
            ids.update(match_ids)
        con.commit()
        print(f"Discovered {len(ids)} unique Ranked Flex matches.")
        for number, match_id in enumerate(sorted(ids), 1):
            # The untouched Match-V5 response includes every post-game participant field.
            retrieve(con, "match", match_id, f"https://{regional}.api.riotgames.com/lol/match/v5/matches/{match_id}", api_key, None)
            # Timeline is retained too: frames, events, positions, gold and XP progress.
            retrieve(con, "timeline", match_id, f"https://{regional}.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline", api_key, None)
            print(f"{number}/{len(ids)} {match_id}")
    print(f"SQLite archive updated: {DATABASE.resolve()}")


if __name__ == "__main__":
    main()
