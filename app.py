"""Ranked Flex team dashboard backed by the Riot Games API.

Run with: streamlit run app.py
"""
from __future__ import annotations

import os
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
from itertools import combinations
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st


QUEUE_ID = 440
QUEUE_TYPE = "RANKED_FLEX_SR"
DATABASE_PATH = Path("data/riot_cache.sqlite3")
ROSTER_PATH = Path("players.txt")
PLATFORMS = {"Europe West": "euw1", "Europe Nordic & East": "eun1", "North America": "na1", "Korea": "kr", "Japan": "jp1", "Oceania": "oc1"}
REGIONS = {"euw1": "europe", "eun1": "europe", "na1": "americas", "kr": "asia", "jp1": "asia", "oc1": "sea"}
RANK_SCORE = {"CHALLENGER": 900, "GRANDMASTER": 800, "MASTER": 700, "DIAMOND": 600, "EMERALD": 500, "PLATINUM": 400, "GOLD": 300, "SILVER": 200, "BRONZE": 100, "IRON": 0}
REQUEST_TIMES: list[float] = []


st.set_page_config(page_title="Flex Stats", page_icon="🎮", layout="wide")


def db() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DATABASE_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS cache (
        cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at TEXT NOT NULL
    )""")
    return con


def cached_json(key: str, max_age: timedelta | None) -> Any | None:
    with closing(db()) as con:
        row = con.execute("SELECT payload, fetched_at FROM cache WHERE cache_key = ?", (key,)).fetchone()
    if not row:
        return None
    fetched_at = datetime.fromisoformat(row[1])
    if max_age and datetime.now(timezone.utc) - fetched_at > max_age:
        return None
    return __import__("json").loads(row[0])


def store_json(key: str, payload: Any) -> None:
    import json
    with closing(db()) as con:
        con.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?)", (key, json.dumps(payload), datetime.now(timezone.utc).isoformat()))
        con.commit()


def cached_at(key: str) -> str | None:
    with closing(db()) as con:
        row = con.execute("SELECT fetched_at FROM cache WHERE cache_key = ?", (key,)).fetchone()
    return row[0] if row else None


def api_key() -> str:
    return str(st.secrets.get("RIOT_API_KEY", os.getenv("RIOT_API_KEY", ""))).strip()


def throttle() -> None:
    """Stay under conservative Riot application limits (18/s and 90/2 min)."""
    global REQUEST_TIMES
    now = time.monotonic()
    REQUEST_TIMES = [point for point in REQUEST_TIMES if now - point < 120]
    one_second = [point for point in REQUEST_TIMES if now - point < 1]
    delays = []
    if len(one_second) >= 18:
        delays.append(1 - (now - one_second[0]) + 0.05)
    if len(REQUEST_TIMES) >= 90:
        delays.append(120 - (now - REQUEST_TIMES[0]) + 0.25)
    if delays:
        time.sleep(max(delays))
        return throttle()
    REQUEST_TIMES.append(now)


def riot_get(url: str, key: str, cache_key: str, max_age: timedelta | None, params: dict[str, Any] | None = None) -> Any:
    saved = cached_json(cache_key, max_age)
    if saved is not None:
        return saved
    for attempt in range(4):
        throttle()
        response = requests.get(url, headers={"X-Riot-Token": key}, params=params, timeout=30)
        if response.status_code == 429 and attempt < 3:
            time.sleep(float(response.headers.get("Retry-After", min(2 ** attempt, 10))))
            continue
        if response.status_code == 404:
            return None
        if not response.ok:
            raise RuntimeError(f"Riot API returned {response.status_code}: {response.text[:300]}")
        payload = response.json()
        store_json(cache_key, payload)
        return payload
    raise RuntimeError("Riot API rate limit retries were exhausted.")


def parse_players(raw: str) -> list[dict[str, str]]:
    """Format: RiotID#TAG | Display name, one per line."""
    players = []
    for line in raw.splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        account, _, display = value.partition("|")
        game_name, sep, tag = account.strip().partition("#")
        if not sep or not game_name.strip() or not tag.strip():
            raise ValueError(f"Use RiotID#TAG | Name. Invalid line: {line}")
        players.append({"riot_id": f"{game_name.strip()}#{tag.strip()}", "game_name": game_name.strip(), "tag": tag.strip(), "name": display.strip() or game_name.strip()})
    if len(players) < 2:
        raise ValueError("Add at least two players.")
    return players


def resolve_players(players: list[dict[str, str]], platform: str, key: str) -> list[dict[str, str]]:
    regional = REGIONS[platform]
    resolved = []
    for player in players:
        account_key = f"account:{regional}:{player['riot_id'].lower()}"
        account = riot_get(f"https://{regional}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{quote(player['game_name'], safe='')}/{quote(player['tag'], safe='')}", key, account_key, timedelta(days=30))
        if not account:
            raise RuntimeError(f"Could not find {player['riot_id']}.")
        puuid = account["puuid"]
        summoner = riot_get(f"https://{platform}.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{puuid}", key, f"summoner:{platform}:{puuid}", timedelta(days=30))
        resolved.append({**player, "puuid": puuid, "account": f"{account.get('gameName', player['game_name'])}#{account.get('tagLine', player['tag'])}", "summoner_id": (summoner or {}).get("id", "")})
    return resolved


def fetch_data(players: list[dict[str, str]], platform: str, match_count: int, force_refresh: bool) -> tuple[list[dict[str, str]], dict[str, Any], dict[str, Any]]:
    key = api_key()
    if not key:
        raise RuntimeError("Set RIOT_API_KEY in .streamlit/secrets.toml or your environment.")
    if force_refresh:
        # Match-list cache is intentionally made stale; match payloads remain safely reusable forever.
        with closing(db()) as con:
            con.execute("DELETE FROM cache WHERE cache_key LIKE 'match-ids:%'")
            con.commit()
    resolved = resolve_players(players, platform, key)
    regional = REGIONS[platform]
    match_ids: set[str] = set()
    ranks: dict[str, Any] = {}
    for player in resolved:
        puuid = player["puuid"]
        ranks[puuid] = riot_get(f"https://{platform}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}", key, f"rank:{platform}:{puuid}", timedelta(minutes=30)) or []
        ids = riot_get(f"https://{regional}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids", key, f"match-ids:{regional}:{puuid}:{match_count}", timedelta(minutes=15), {"queue": QUEUE_ID, "start": 0, "count": match_count}) or []
        match_ids.update(ids)
    matches = {}
    progress = st.progress(0, text="Loading cached and new match details…")
    for index, match_id in enumerate(sorted(match_ids)):
        matches[match_id] = riot_get(f"https://{regional}.api.riotgames.com/lol/match/v5/matches/{match_id}", key, f"match:{regional}:{match_id}", None)
        progress.progress((index + 1) / max(len(match_ids), 1), text=f"Loaded {index + 1}/{len(match_ids)} matches")
    progress.empty()
    return resolved, ranks, matches


def flex_rank(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((entry for entry in entries if entry.get("queueType") == QUEUE_TYPE), None)


def build_tables(players: list[dict[str, str]], ranks: dict[str, Any], matches: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    tracked = {p["puuid"]: p for p in players}
    records, champions, pairs, games = [], [], Counter(), []
    for match_id, match in matches.items():
        info = match.get("info", {})
        participants = [p for p in info.get("participants", []) if p.get("puuid") in tracked]
        teams: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for p in participants:
            teams[p.get("teamId")].append(p)
        eligible = [team for team in teams.values() if len(team) >= 2]
        if not eligible:
            continue
        for team in eligible:
            team_names = [tracked[p["puuid"]]["name"] for p in team]
            won = bool(team[0].get("win"))
            games.append({"Match ID": match_id, "Played": datetime.fromtimestamp((info.get("gameEndTimestamp") or info.get("gameCreation", 0)) / 1000, tz=timezone.utc), "Players": ", ".join(team_names), "Result": "Win" if won else "Loss", "Duration": round(info.get("gameDuration", 0) / 60, 1)})
            for a, b in combinations(sorted(team_names), 2):
                pairs[(a, b, won)] += 1
            for p in team:
                name = tracked[p["puuid"]]["name"]
                duration = max(info.get("gameDuration", 1) / 60, 1)
                records.append({"Player": name, "Games": 1, "Wins": int(p.get("win", False)), "Kills": p.get("kills", 0), "Deaths": p.get("deaths", 0), "Assists": p.get("assists", 0), "Damage": p.get("totalDamageDealtToChampions", 0), "Vision": p.get("visionScore", 0), "CS": p.get("totalMinionsKilled", 0) + p.get("neutralMinionsKilled", 0), "Minutes": duration})
                champions.append({"Player": name, "Champion": p.get("championName", "Unknown"), "Games": 1, "Wins": int(p.get("win", False)), "Kills": p.get("kills", 0), "Deaths": p.get("deaths", 0), "Assists": p.get("assists", 0)})
    raw = pd.DataFrame(records)
    if raw.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(games)
    player_df = raw.groupby("Player", as_index=False).sum(numeric_only=True)
    player_df["Losses"] = player_df["Games"] - player_df["Wins"]
    player_df["Win Rate %"] = (100 * player_df["Wins"] / player_df["Games"]).round(1)
    player_df["KDA"] = ((player_df["Kills"] + player_df["Assists"]) / player_df["Deaths"].clip(lower=1)).round(2)
    player_df["Damage / Min"] = (player_df["Damage"] / player_df["Minutes"]).round()
    player_df["CS / Min"] = (player_df["CS"] / player_df["Minutes"]).round(1)
    champion_df = pd.DataFrame(champions).groupby(["Player", "Champion"], as_index=False).sum(numeric_only=True)
    champion_df["Win Rate %"] = (100 * champion_df["Wins"] / champion_df["Games"]).round(1)
    champion_df["KDA"] = ((champion_df["Kills"] + champion_df["Assists"]) / champion_df["Deaths"].clip(lower=1)).round(2)
    pair_rows = [{"Duo": " + ".join((a, b)), "Games": w + l, "Wins": w, "Win Rate %": round(100 * w / (w + l), 1)} for a, b in sorted({k[:2] for k in pairs}) for w, l in [(pairs[(a,b,True)], pairs[(a,b,False)])]]
    return player_df.sort_values(["Win Rate %", "Games"], ascending=False), champion_df.sort_values(["Games", "Win Rate %"], ascending=False), pd.DataFrame(pair_rows).sort_values("Games", ascending=False) if pair_rows else pd.DataFrame(), pd.DataFrame(games).sort_values("Played", ascending=False)


def rank_table(players: list[dict[str, str]], ranks: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for player in players:
        entry = flex_rank(ranks[player["puuid"]])
        wins, losses = (entry.get("wins", 0), entry.get("losses", 0)) if entry else (0, 0)
        rows.append({"Player": player["name"], "Account": player["account"], "Flex rank": f"{entry['tier'].title()} {entry['rank']} — {entry['leaguePoints']} LP" if entry else "Unranked", "Wins": wins, "Losses": losses, "Win Rate %": round(100 * wins / (wins + losses), 1) if wins + losses else 0})
    return pd.DataFrame(rows)


st.title("Ranked Flex Team Records")
st.caption("Shared ranked-flex history only: a match counts when at least two tracked players were teammates.")

with st.sidebar:
    st.header("Data settings")
    platform_label = st.selectbox("Platform", list(PLATFORMS), index=0)
    match_count = st.slider("Recent flex matches per player", 10, 100, 40, 10)
    st.caption("Match details are saved in `data/riot_cache.sqlite3`. Profiles refresh after 30 minutes, match lists after 15 minutes.")

default_roster = ROSTER_PATH.read_text(encoding="utf-8") if ROSTER_PATH.exists() else ""
raw_players = st.text_area("Tracked players", value=default_roster, placeholder="RiotID#EUW | Display name\nAnotherName#TAG | Another player", height=180, help="This loads players.txt by default. Edit the file when your fixed roster changes.")
col_a, col_b = st.columns([1, 4])
with col_a:
    analyse = st.button("Analyse flex records", type="primary", use_container_width=True)
with col_b:
    refresh = st.checkbox("Refresh match lists now")

if analyse:
    try:
        selected = parse_players(raw_players)
        players, ranks, matches = fetch_data(selected, PLATFORMS[platform_label], match_count, refresh)
        st.session_state["results"] = (players, ranks, matches)
    except Exception as exc:
        st.error(str(exc))

if "results" in st.session_state:
    players, ranks, matches = st.session_state["results"]
    player_df, champion_df, duo_df, game_df = build_tables(players, ranks, matches)
    st.subheader("Flex profiles")
    st.dataframe(rank_table(players, ranks), hide_index=True, use_container_width=True)
    if player_df.empty:
        st.warning("No sampled games contained two tracked players on the same team.")
    else:
        total_games, wins = len(game_df), int((game_df["Result"] == "Win").sum())
        a, b, c, d = st.columns(4)
        a.metric("Eligible team games", total_games)
        b.metric("Team win rate", f"{100 * wins / total_games:.1f}%")
        c.metric("Unique flex matches", len(matches))
        d.metric("Tracked players", len(players))
        st.subheader("Player leaderboard")
        st.dataframe(player_df[["Player", "Games", "Wins", "Losses", "Win Rate %", "KDA", "Damage / Min", "CS / Min", "Vision"]], hide_index=True, use_container_width=True)
        left, right = st.columns(2)
        with left:
            st.subheader("Duo records")
            st.dataframe(duo_df, hide_index=True, use_container_width=True)
        with right:
            st.subheader("Champion records")
            st.dataframe(champion_df, hide_index=True, use_container_width=True)
        st.subheader("Eligible match history")
        st.dataframe(game_df, hide_index=True, use_container_width=True, column_config={"Played": st.column_config.DatetimeColumn(format="D MMM YYYY, HH:mm")})
