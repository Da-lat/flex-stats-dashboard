"""Feed archived Ranked Flex games into the established custom-dashboard renderer."""
from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from functools import lru_cache
from html import escape
from itertools import combinations
from pathlib import Path
from zoneinfo import ZoneInfo


DATABASE = Path("data/riot_cache.sqlite3")
EXPORTED_HISTORY = Path("data/reference_match_history.json")
OUTPUT_DIRECTORY = Path("site")
ROSTER_FILE = Path("players.txt")
REFERENCE_GENERATOR = Path(r"C:\Users\brand\Documents\Coding\Python\Custom_match_dashboards\data_analysis_customs.py")
ROLE_MAP = {"MIDDLE": "MID", "BOTTOM": "BOT", "UTILITY": "SUPP"}
VALID_ROLES = {"TOP", "JUNGLE", "MID", "BOT", "SUPP"}
MINIMUM_GAME_SECONDS = 10 * 60
UK_TIMEZONE = ZoneInfo("Europe/London")
PING_TYPES = {
    "allInPings": "All In",
    "assistMePings": "Assist Me",
    "basicPings": "Basic",
    "commandPings": "Command",
    "dangerPings": "Danger",
    "enemyMissingPings": "Enemy Missing",
    "enemyVisionPings": "Enemy Vision",
    "getBackPings": "Get Back",
    "holdPings": "Hold",
    "needVisionPings": "Need Vision",
    "onMyWayPings": "On the Way",
    "pushPings": "Push",
    "retreatPings": "Retreat",
    "visionClearedPings": "Vision Cleared",
}
SHARED_NAV_HTML = """<nav class="shared-dashboard-nav">
    <a href="index.html#overview">Dashboard</a>
    <a href="index.html#players">Players</a>
    <a href="index.html#champions">Champions</a>
    <a href="index.html#combos">Combos</a>
    <a href="index.html#matches">Matches</a>
    <a href="index_teams.html#teams">Teams</a>
    <a href="index_showcases.html">Showcases</a>
    <a href="index_experimental.html#custom-meta">Experimental</a>
  </nav>"""
SHARED_NAV_CSS = """<style id="shared-dashboard-nav-style">
  nav.shared-dashboard-nav {
    display: flex; gap: 8px; flex-wrap: wrap;
    padding: 12px max(24px, calc((100vw - 1320px) / 2));
    background: #0f1721; border-bottom: 1px solid #243142;
    position: sticky; top: 0; z-index: 100; width: 100%;
  }
  nav.shared-dashboard-nav a {
    color: #e8edf3; text-decoration: none; font-weight: 700;
    font-size: 0.91rem; padding: 8px 10px; border-radius: 6px;
  }
  nav.shared-dashboard-nav a:hover { background: #1a2633; }
</style>"""


def winrate_color(winrate: float) -> str:
    """Return a performance colour calibrated around a 50% baseline."""
    stops = (
        (0.00, (155, 43, 61)),
        (0.40, (207, 71, 86)),
        (0.47, (222, 143, 65)),
        (0.50, (205, 174, 84)),
        (0.53, (132, 184, 89)),
        (0.56, (67, 190, 112)),
        (0.60, (27, 170, 98)),
        (0.70, (10, 126, 72)),
        (1.00, (6, 91, 52)),
    )
    value = max(0.0, min(1.0, float(winrate)))
    for (lower, start), (upper, end) in zip(stops, stops[1:]):
        if value <= upper:
            amount = (value - lower) / (upper - lower)
            channels = tuple(round(a + ((b - a) * amount)) for a, b in zip(start, end))
            return f"rgb{channels}"
    return "rgb(6, 91, 52)"


def winrate_text_color(winrate: float) -> str:
    return "#17212b" if 0.43 <= winrate < 0.60 else "#ffffff"


def tracked_real_names() -> set[str]:
    return {
        line.split("|", 1)[1].strip()
        for line in ROSTER_FILE.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "|" in line
    }


def display_role(participant: dict) -> str:
    role = participant.get("teamPosition") or participant.get("individualPosition") or "UNKNOWN"
    return ROLE_MAP.get(str(role).upper(), str(role).upper())


def is_meaningful_match(info: dict) -> bool:
    if int(info.get("gameDuration", 0) or 0) < MINIMUM_GAME_SECONDS:
        return False
    participants = info.get("participants", [])
    return len(participants) == 10 and all(display_role(participant) in VALID_ROLES for participant in participants)


def participant_name(participant: dict, tracked: dict[str, str]) -> str:
    puuid = participant.get("puuid", "")
    if puuid in tracked:
        return tracked[puuid]
    # The reference dashboard requires ten entries per match. Keep opponents
    # anonymous so the flex group's leaderboard is not diluted by hundreds of
    # one-game enemy profiles, while preserving normal five-player team shape.
    # The reference renderer recognises the Anonymous prefix and removes these
    # placeholders from all aggregate/player-facing statistic views.
    return f"Anonymous opposition {display_role(participant)}"


def export_match_history() -> int:
    with sqlite3.connect(DATABASE) as con:
        tracked = dict(con.execute("SELECT puuid, display_name FROM tracked_players WHERE puuid IS NOT NULL"))
        payloads = [json.loads(row[0]) for row in con.execute("SELECT payload FROM riot_payloads WHERE kind='match'")]
    if not tracked or not payloads:
        raise RuntimeError("No Riot archive found. Run python sync_riot.py first.")

    matches = []
    for payload in payloads:
        metadata, info = payload.get("metadata", {}), payload.get("info", {})
        if not is_meaningful_match(info):
            continue
        teams: dict[int, list[dict]] = {}
        for participant in info.get("participants", []):
            teams.setdefault(participant.get("teamId"), []).append(participant)
        tracked_teams = [team for team in teams.values() if sum(p.get("puuid") in tracked for p in team) >= 2]
        if not tracked_teams:
            continue
        # A ranked match has two teams; represent it in the exact win/lose
        # schema consumed by data_analysis_customs.py.
        winners = next((team for team in teams.values() if team and team[0].get("win")), [])
        losers = next((team for team in teams.values() if team and not team[0].get("win")), [])
        if len(winners) != 5 or len(losers) != 5:
            continue

        def side(team: list[dict]) -> list[dict]:
            return [
                {
                    "player": participant.get("riotIdGameName") or participant.get("summonerName") or "Unknown",
                    "name": participant_name(participant, tracked),
                    "role": display_role(participant),
                    "champion": participant.get("championName", "Unknown"),
                    "kda": f"{participant.get('kills', 0)}/{participant.get('deaths', 0)}/{participant.get('assists', 0)}",
                    # Extra Riot fields remain available in this export as well
                    # as in their untouched form in SQLite.
                    "damage_to_champions": participant.get("totalDamageDealtToChampions", 0),
                    "vision_score": participant.get("visionScore", 0),
                    "cs": participant.get("totalMinionsKilled", 0) + participant.get("neutralMinionsKilled", 0),
                    "gold": participant.get("goldEarned", 0),
                }
                for participant in team
            ]

        timestamp = info.get("gameEndTimestamp") or info.get("gameCreation")
        matches.append(
            {
                "win": side(winners),
                "lose": side(losers),
                "timestamp": datetime.fromtimestamp(timestamp / 1000, timezone.utc).isoformat().replace("+00:00", "Z") if timestamp else "",
                "checksum": metadata.get("matchId", ""),
            }
        )
    OUTPUT_DIRECTORY.mkdir(exist_ok=True)
    EXPORTED_HISTORY.parent.mkdir(exist_ok=True)
    EXPORTED_HISTORY.write_text(json.dumps(matches, ensure_ascii=False), encoding="utf-8")
    return len(matches)


@lru_cache(maxsize=1)
def tracked_match_log() -> list[dict[str, object]]:
    """Return roster-only match rows, cached for all generated sections."""
    with sqlite3.connect(DATABASE) as con:
        tracked = dict(con.execute("SELECT puuid, display_name FROM tracked_players WHERE puuid IS NOT NULL"))
        payloads = [json.loads(row[0]) for row in con.execute("SELECT payload FROM riot_payloads WHERE kind='match'")]
    match_numbers = {
        str(match.get("checksum", "")): index
        for index, match in enumerate(json.loads(EXPORTED_HISTORY.read_text(encoding="utf-8")), start=1)
    }
    rows = []
    for payload in payloads:
        info = payload.get("info", {})
        if not is_meaningful_match(info):
            continue
        teams: dict[int, list[dict]] = {}
        for participant in info.get("participants", []):
            teams.setdefault(participant.get("teamId"), []).append(participant)
        roster_team = next((team for team in teams.values() if sum(p.get("puuid") in tracked for p in team) >= 2), None)
        if not roster_team:
            continue
        tracked_participants = [p for p in roster_team if p.get("puuid") in tracked]
        tracked_picks = sorted(
            {
                (tracked[p["puuid"]], str(p.get("championName", "Unknown")))
                for p in tracked_participants
            }
        )
        names = sorted({name for name, _champion in tracked_picks})
        match_id = str(payload.get("metadata", {}).get("matchId", ""))
        timestamp = info.get("gameEndTimestamp") or info.get("gameCreation")
        rows.append({
            "id": match_id,
            "number": match_numbers.get(match_id, 0),
            "played": datetime.fromtimestamp(timestamp / 1000, UK_TIMEZONE).strftime("%d %b %Y, %H:%M %Z") if timestamp else "-",
            "sort": timestamp or 0,
            "players": names,
            "champions": [f"{name}: {champion}" for name, champion in tracked_picks],
            "participant_stats": [
                {
                    "name": tracked[p["puuid"]],
                    "champion": str(p.get("championName", "Unknown")),
                    "role": display_role(p),
                    "damage": int(p.get("totalDamageDealtToChampions", 0) or 0),
                    "vision": int(p.get("visionScore", 0) or 0),
                    "vision_wards_bought": int(p.get("visionWardsBoughtInGame", 0) or 0),
                    "wards_killed": int(p.get("wardsKilled", 0) or 0),
                    "wards_placed": int(p.get("wardsPlaced", 0) or 0),
                    "max_cs_advantage": p.get("challenges", {}).get("maxCsAdvantageOnLaneOpponent"),
                    "kill_participation": p.get("challenges", {}).get("killParticipation"),
                    "lane_advantage": p.get("challenges", {}).get("laningPhaseGoldExpAdvantage"),
                    "skillshots_dodged": p.get("challenges", {}).get("skillshotsDodged"),
                    "pings": {
                        key: int(p.get(key, 0) or 0)
                        for key in PING_TYPES
                        if key in p
                    },
                }
                for p in tracked_participants
            ],
            "result": "Win" if roster_team[0].get("win") else "Loss",
            "minutes": round(info.get("gameDuration", 0) / 60, 1),
        })
    rows.sort(key=lambda row: int(row["sort"]), reverse=True)
    return rows


def timeline_frame(timeline: dict, minute: int) -> dict:
    """Return the frame closest to an exact minute in a Match-V5 timeline."""
    frames = timeline.get("info", {}).get("frames", [])
    target = minute * 60_000
    return min(frames, key=lambda frame: abs(int(frame.get("timestamp", 0)) - target)) if frames else {}


@lru_cache(maxsize=1)
def experimental_player_records() -> list[dict[str, object]]:
    """Hydrate tracked-player records with their matching timeline metrics."""
    with sqlite3.connect(DATABASE) as con:
        tracked = dict(con.execute("SELECT puuid, display_name FROM tracked_players WHERE puuid IS NOT NULL"))
        matches = {
            resource_id: json.loads(payload)
            for resource_id, payload in con.execute(
                "SELECT resource_id, payload FROM riot_payloads WHERE kind='match'"
            )
        }
        timelines = {
            resource_id: json.loads(payload)
            for resource_id, payload in con.execute(
                "SELECT resource_id, payload FROM riot_payloads WHERE kind='timeline'"
            )
        }

    records = []
    for match_id, payload in matches.items():
        info = payload.get("info", {})
        timeline = timelines.get(match_id)
        if not timeline or not is_meaningful_match(info):
            continue
        participants = info.get("participants", [])
        teams: dict[int, list[dict]] = defaultdict(list)
        for participant in participants:
            teams[int(participant.get("teamId", 0))].append(participant)
        roster_team = next(
            (team for team in teams.values() if sum(p.get("puuid") in tracked for p in team) >= 2),
            None,
        )
        if not roster_team:
            continue

        frames = {minute: timeline_frame(timeline, minute) for minute in (5, 10, 15)}
        timeline_events = [
            event
            for frame in timeline.get("info", {}).get("frames", [])
            for event in frame.get("events", [])
        ]
        epic_events = [event for event in timeline_events if event.get("type") == "ELITE_MONSTER_KILL"]
        building_events = [event for event in timeline_events if event.get("type") == "BUILDING_KILL"]

        for participant in roster_team:
            puuid = participant.get("puuid")
            if puuid not in tracked:
                continue
            participant_id = int(participant.get("participantId", 0))
            team_id = int(participant.get("teamId", 0))
            role = display_role(participant)
            opponent = next(
                (
                    candidate
                    for candidate in participants
                    if int(candidate.get("teamId", 0)) != team_id and display_role(candidate) == role
                ),
                None,
            )
            opponent_id = int(opponent.get("participantId", 0)) if opponent else 0
            differentials = {}
            snapshots = {}
            for minute, frame in frames.items():
                participant_frame = frame.get("participantFrames", {}).get(str(participant_id), {})
                opponent_frame = frame.get("participantFrames", {}).get(str(opponent_id), {})
                own_cs = int(participant_frame.get("minionsKilled", 0)) + int(participant_frame.get("jungleMinionsKilled", 0))
                opponent_cs = int(opponent_frame.get("minionsKilled", 0)) + int(opponent_frame.get("jungleMinionsKilled", 0))
                snapshots[minute] = {
                    "gold": int(participant_frame.get("totalGold", 0)),
                    "xp": int(participant_frame.get("xp", 0)),
                    "cs": own_cs,
                    "jungle_cs": int(participant_frame.get("jungleMinionsKilled", 0)),
                    "level": int(participant_frame.get("level", 0)),
                }
                differentials[minute] = {
                    "gold": int(participant_frame.get("totalGold", 0)) - int(opponent_frame.get("totalGold", 0)),
                    "xp": int(participant_frame.get("xp", 0)) - int(opponent_frame.get("xp", 0)),
                    "cs": own_cs - opponent_cs,
                    "level": int(participant_frame.get("level", 0)) - int(opponent_frame.get("level", 0)),
                }

            objective_involvement = Counter()
            for event in epic_events:
                involved = participant_id == int(event.get("killerId", 0)) or participant_id in event.get("assistingParticipantIds", [])
                if not involved:
                    continue
                monster = str(event.get("monsterType", "")).upper()
                subtype = str(event.get("monsterSubType", "")).upper()
                if monster == "DRAGON":
                    objective_involvement["dragon"] += 1
                elif monster == "BARON_NASHOR":
                    objective_involvement["baron"] += 1
                elif monster == "RIFTHERALD":
                    objective_involvement["herald"] += 1
                elif monster == "HORDE":
                    objective_involvement["grub"] += 1
                elif monster == "ATAKHAN" or "ATAKHAN" in subtype:
                    objective_involvement["atakhan"] += 1

            takedown_events = [
                event
                for event in timeline_events
                if event.get("type") == "CHAMPION_KILL"
                and (
                    participant_id == int(event.get("killerId", 0))
                    or participant_id in event.get("assistingParticipantIds", [])
                )
            ]
            converted_takedowns = 0
            for takedown in takedown_events:
                start = int(takedown.get("timestamp", 0))
                end = start + 90_000
                converted = any(
                    start <= int(event.get("timestamp", 0)) <= end
                    and int(event.get("killerTeamId", 0) or participants[int(event.get("killerId", 0)) - 1].get("teamId", 0) if int(event.get("killerId", 0)) else 0) == team_id
                    for event in epic_events
                ) or any(
                    start <= int(event.get("timestamp", 0)) <= end
                    and int(event.get("teamId", 0)) != team_id
                    for event in building_events
                )
                converted_takedowns += int(converted)

            full_clear_minute = None
            if role == "JUNGLE":
                for frame in timeline.get("info", {}).get("frames", []):
                    jungle_cs = int(frame.get("participantFrames", {}).get(str(participant_id), {}).get("jungleMinionsKilled", 0))
                    if jungle_cs >= 24:
                        full_clear_minute = int(frame.get("timestamp", 0)) / 60_000
                        break

            records.append(
                {
                    "name": tracked[puuid],
                    "match_id": match_id,
                    "role": role,
                    "win": bool(participant.get("win")),
                    "minutes": float(info.get("gameDuration", 0)) / 60,
                    "participant": participant,
                    "challenges": participant.get("challenges", {}),
                    "diff": differentials,
                    "snapshot": snapshots,
                    "objectives": dict(objective_involvement),
                    "converted_takedowns": converted_takedowns,
                    "timeline_takedowns": len(takedown_events),
                    "full_clear_minute": full_clear_minute,
                }
            )
    return records


def advanced_stat_awards() -> list[dict[str, object]]:
    totals: dict[str, dict[str, float]] = {}
    for match in tracked_match_log():
        for participant in match["participant_stats"]:
            name = str(participant["name"])
            record = totals.setdefault(
                name,
                {
                    "games": 0, "damage": 0, "vision": 0,
                    "cs_gap_total": 0, "cs_gap_games": 0,
                    "kp_total": 0, "kp_games": 0,
                    "lane_total": 0, "lane_games": 0,
                    "dodged_total": 0, "dodged_games": 0,
                },
            )
            record["games"] += 1
            record["damage"] += int(participant["damage"])
            record["vision"] += int(participant["vision"])
            cs_gap = participant.get("max_cs_advantage")
            if cs_gap is not None:
                record["cs_gap_total"] += float(cs_gap)
                record["cs_gap_games"] += 1
            for value_key, total_key, games_key in (
                ("kill_participation", "kp_total", "kp_games"),
                ("lane_advantage", "lane_total", "lane_games"),
                ("skillshots_dodged", "dodged_total", "dodged_games"),
            ):
                value = participant.get(value_key)
                if value is not None:
                    record[total_key] += float(value)
                    record[games_key] += 1

    qualified = [(name, row) for name, row in totals.items() if row["games"] >= 10]
    highest_damage = max(qualified, key=lambda item: item[1]["damage"] / item[1]["games"])
    highest_vision = max(qualified, key=lambda item: item[1]["vision"] / item[1]["games"])
    cs_gap_qualified = [item for item in qualified if item[1]["cs_gap_games"] >= 10]
    highest_cs_gap = max(
        cs_gap_qualified,
        key=lambda item: item[1]["cs_gap_total"] / item[1]["cs_gap_games"],
    )
    def highest_average(total_key: str, games_key: str):
        candidates = [item for item in qualified if item[1][games_key] >= 10]
        return max(candidates, key=lambda item: item[1][total_key] / item[1][games_key])

    hero_name, hero = highest_average("kp_total", "kp_games")
    lane_name, lane = highest_average("lane_total", "lane_games")
    feet_name, feet = highest_average("dodged_total", "dodged_games")
    damage_name, damage = highest_damage
    vision_name, vision = highest_vision
    awards = [
        {
            "title": "DPS Check",
            "winner": damage_name,
            "stat": f'{damage["damage"] / damage["games"]:,.0f} damage per game',
            "detail": f'Average damage to champions over {int(damage["games"])} games.',
            "theme": "red",
            "badge": "K",
        },
        {
            "title": "All Seeing",
            "winner": vision_name,
            "stat": f'{vision["vision"] / vision["games"]:.1f} vision per game',
            "detail": f'Average vision score over {int(vision["games"])} games.',
            "theme": "blue",
            "badge": "SUPP",
        },
    ]
    gap_name, gap = highest_cs_gap
    awards.append(
        {
            "title": "Player Gap",
            "winner": gap_name,
            "stat": f'+{gap["cs_gap_total"] / gap["cs_gap_games"]:,.1f} average max CS advantage per game',
            "detail": f'Average maxCsAdvantageOnLaneOpponent over {int(gap["cs_gap_games"])} recorded games.',
            "theme": "gold",
            "badge": "TOP",
        }
    )
    awards.extend(
        [
            {
                "title": "Hero",
                "winner": hero_name,
                "stat": f'{hero["kp_total"] / hero["kp_games"] * 100:.1f}% kill participation per game',
                "detail": f'Average killParticipation over {int(hero["kp_games"])} recorded games.',
                "theme": "blue",
                "badge": "KDA",
            },
            {
                "title": "Lane King",
                "winner": lane_name,
                "stat": f'+{lane["lane_total"] / lane["lane_games"]:.3f} average lane advantage',
                "detail": f'Average laningPhaseGoldExpAdvantage over {int(lane["lane_games"])} recorded games.',
                "theme": "gold",
                "badge": "WR",
            },
            {
                "title": "Happy Feet",
                "winner": feet_name,
                "stat": f'{feet["dodged_total"] / feet["dodged_games"]:.1f} skillshots dodged per game',
                "detail": f'Average skillshotsDodged over {int(feet["dodged_games"])} recorded games.',
                "theme": "green",
                "badge": "GP",
            },
        ]
    )
    return awards


def experimental_ping_chart() -> str:
    player_stats: dict[str, dict[str, object]] = {}
    for match in tracked_match_log():
        for participant in match["participant_stats"]:
            name = str(participant["name"])
            record = player_stats.setdefault(
                name,
                {
                    "games": 0,
                    "totals": {key: 0 for key in PING_TYPES},
                },
            )
            record["games"] += 1
            for key, value in participant["pings"].items():
                record["totals"][key] += int(value)

    # Riot leaves several legacy ping fields at zero in every match. Keeping
    # those columns adds noise without conveying any information.
    relevant_ping_types = [
        key
        for key in PING_TYPES
        if sum(int(record["totals"][key]) for record in player_stats.values()) > 0
    ]

    averages = {
        name: {
            key: record["totals"][key] / record["games"] if record["games"] else 0.0
            for key in relevant_ping_types
        }
        for name, record in player_stats.items()
    }
    for name, row in averages.items():
        row["total"] = sum(row[key] for key in relevant_ping_types)

    displayed_columns = [*relevant_ping_types, "total"]
    column_labels = {**PING_TYPES, "total": "Total / Game"}
    column_max = {
        key: max((row[key] for row in averages.values()), default=0.0)
        for key in displayed_columns
    }
    column_median = {}
    for key in displayed_columns:
        values = sorted(row[key] for row in averages.values() if row[key] > 0)
        middle = len(values) // 2
        column_median[key] = (
            (values[middle - 1] + values[middle]) / 2
            if len(values) % 2 == 0 and values
            else values[middle] if values else 0.0
        )

    def heat_style(key: str, value: float) -> tuple[str, str]:
        """Return a robust colour and outlier label for a ping average."""
        if value <= 0:
            return "background:rgba(98,168,255,.025)", "No recorded pings"
        median = column_median[key]
        ratio = value / median if median else 1.0
        if ratio >= 5:
            return "background:rgba(255,76,105,.72)", "Extreme outlier"
        if ratio >= 3:
            return "background:rgba(255,139,72,.64)", "Strong outlier"
        if ratio >= 1.75:
            return "background:rgba(246,199,94,.53);color:#fff2c2", "Above typical"
        strength = value / column_max[key] if column_max[key] else 0.0
        return f"background:rgba(98,168,255,{0.07 + 0.38 * strength:.3f})", "Typical range"

    headers = "".join(
        f'<th class="{"ping-total-heading" if key == "total" else ""}" data-type="number" '
        f'title="Average {escape(column_labels[key])} pings per eligible game">{escape(column_labels[key])}</th>'
        for key in displayed_columns
    )
    omitted_count = len(PING_TYPES) - len(relevant_ping_types)
    omitted_note = f" · {omitted_count} all-zero types hidden" if omitted_count else ""
    rows = []
    for name in sorted(player_stats):
        cells = []
        for key in displayed_columns:
            value = averages[name][key]
            style, outlier_label = heat_style(key, value)
            cells.append(
                f'<td class="ping-heat-cell {"ping-total-cell" if key == "total" else ""}" data-sort="{value:.6f}" '
                f'style="{style}" title="{escape(column_labels[key])}: {value:.2f} per game across '
                f'{player_stats[name]["games"]} games · {outlier_label}">{value:.2f}</td>'
            )
        rows.append(
            f'<tr><td class="ping-player"><strong>{escape(name)}</strong></td>'
            f'<td class="number-cell" data-sort="{player_stats[name]["games"]}">{player_stats[name]["games"]}</td>'
            f'{"".join(cells)}</tr>'
        )
    return f"""
    <section id="ping-averages" class="section ping-experiment">
      <div class="section-title"><div><h2>Ping Averages</h2><p class="note">Average pings per eligible tracked-player game. Blue is typical, gold is above typical, orange is a strong outlier and red is extreme. Hover any value for details.</p></div></div>
      <section class="table-panel"><div class="section-heading"><h3>Communication heatmap</h3><small>{len(player_stats)} tracked players · {len(relevant_ping_types)} useful ping types{omitted_note}</small></div><div class="table-wrap ping-table-wrap"><table class="sortable-table ping-table"><thead><tr><th>Player</th><th data-type="number">Games</th>{headers}</tr></thead><tbody>{''.join(rows)}</tbody></table></div></section>
    </section>
    <style>.ping-table-wrap{{max-height:720px;overflow:auto}}.ping-table{{min-width:1350px}}.ping-table th{{white-space:normal;min-width:88px;line-height:1.15}}.ping-table th:first-child{{min-width:120px}}.ping-player{{position:sticky;left:0;z-index:1;background:#111b27}}.ping-heat-cell{{text-align:center;font-weight:800;font-variant-numeric:tabular-nums}}.ping-total-heading,.ping-total-cell{{border-left:2px solid #6686aa!important}}.ping-total-cell{{font-size:1.02rem}}</style>
    """


def experimental_vision_chart() -> str:
    metrics = {
        "vision": "Vision Score",
        "vision_wards_bought": "Control Wards Bought",
        "wards_killed": "Wards Killed",
        "wards_placed": "Wards Placed",
    }
    player_stats: dict[str, dict[str, float]] = {}
    for match in tracked_match_log():
        for participant in match["participant_stats"]:
            name = str(participant["name"])
            record = player_stats.setdefault(
                name,
                {"games": 0, **{key: 0.0 for key in metrics}},
            )
            record["games"] += 1
            for key in metrics:
                record[key] += float(participant[key])

    averages = {
        name: {
            key: record[key] / record["games"] if record["games"] else 0.0
            for key in metrics
        }
        for name, record in player_stats.items()
    }
    column_max = {
        key: max((row[key] for row in averages.values()), default=0.0)
        for key in metrics
    }
    headers = "".join(
        f'<th data-type="number" title="Average {escape(label)} per eligible game">{escape(label)}</th>'
        for label in metrics.values()
    )
    rows = []
    for name in sorted(player_stats, key=lambda player: (-averages[player]["vision"], player)):
        cells = []
        for key, label in metrics.items():
            value = averages[name][key]
            strength = value / column_max[key] if column_max[key] else 0.0
            cells.append(
                f'<td class="vision-average-cell" data-sort="{value:.6f}" '
                f'style="background:rgba(89,197,139,{0.05 + 0.48 * strength:.3f})" '
                f'title="{escape(label)}: {value:.2f} per game across {int(player_stats[name]["games"])} games">{value:.2f}</td>'
            )
        rows.append(
            f'<tr><td><strong>{escape(name)}</strong></td>'
            f'<td class="number-cell" data-sort="{int(player_stats[name]["games"])}">{int(player_stats[name]["games"])}</td>'
            f'{"".join(cells)}</tr>'
        )

    return f"""
    <section id="vision-averages" class="section vision-experiment">
      <div class="section-title"><div><h2>Vision Control Averages</h2><p class="note">Per-game averages from eligible matches for tracked players only. Darker green indicates a higher value within that statistic.</p></div></div>
      <section class="table-panel"><div class="section-heading"><h3>Vision and warding</h3><small>{len(player_stats)} tracked players</small></div><div class="table-wrap"><table class="sortable-table vision-average-table"><thead><tr><th>Player</th><th data-type="number">Games</th>{headers}</tr></thead><tbody>{''.join(rows)}</tbody></table></div></section>
    </section>
    <style>.vision-average-table{{min-width:760px}}.vision-average-cell{{text-align:center;font-weight:800;font-variant-numeric:tabular-nums}}</style>
    """


def apply_overall_rankings(
    rows: list[dict[str, object]],
    columns: list[tuple[str, str, int, str, bool]],
    *,
    scope_key: str | None = None,
    minimum_games: int = 10,
    lower_is_better: set[str] | None = None,
) -> None:
    """Add peer percentiles and a transparent equal-weight overall score."""
    lower_is_better = lower_is_better or set()
    scopes: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        scopes[str(row.get(scope_key, "ALL")) if scope_key else "ALL"].append(row)

    for scope_rows in scopes.values():
        qualified = [row for row in scope_rows if int(row["games"]) >= minimum_games]
        for row in scope_rows:
            row["metric_ranks"] = {}
            row["metric_percentiles"] = {}
            row["qualified"] = row in qualified
        for key, _label, _decimals, _suffix, _bipolar in columns:
            ordered = sorted(
                qualified,
                key=lambda row: float(row.get(key, 0) or 0),
                reverse=key not in lower_is_better,
            )
            for index, row in enumerate(ordered, start=1):
                value = float(row.get(key, 0) or 0)
                tied_positions = [
                    position
                    for position, candidate in enumerate(ordered, start=1)
                    if float(candidate.get(key, 0) or 0) == value
                ]
                average_rank = sum(tied_positions) / len(tied_positions)
                percentile = 100.0 if len(ordered) == 1 else 100 * (len(ordered) - average_rank) / (len(ordered) - 1)
                row["metric_ranks"][key] = average_rank
                row["metric_percentiles"][key] = percentile
        for row in qualified:
            percentiles = list(row["metric_percentiles"].values())
            row["overall_score"] = sum(percentiles) / len(percentiles) if percentiles else 0.0
        ordered_scores = sorted(qualified, key=lambda row: (-float(row["overall_score"]), str(row["name"])))
        for rank, row in enumerate(ordered_scores, start=1):
            row["overall_rank"] = rank
            row["peer_count"] = len(qualified)
        for row in scope_rows:
            if row not in qualified:
                row["overall_score"] = -1.0
                row["overall_rank"] = 0
                row["peer_count"] = len(qualified)


def metric_table_html(
    rows: list[dict[str, object]],
    columns: list[tuple[str, str, int, str, bool]],
    table_class: str,
    row_attributes=None,
) -> str:
    """Render a sortable heat table; bipolar columns distinguish leads/deficits."""
    maxima = {
        key: max((abs(float(row.get(key, 0) or 0)) for row in rows), default=0.0)
        for key, _label, _decimals, _suffix, _bipolar in columns
    }
    positive_minima = {
        key: min((float(row.get(key, 0) or 0) for row in rows if float(row.get(key, 0) or 0) > 0), default=0.0)
        for key, _label, _decimals, _suffix, _bipolar in columns
    }
    headers = "".join(
        f'<th data-type="number" title="{escape(label)}">{escape(label)}</th>'
        for _key, label, _decimals, _suffix, _bipolar in columns
    )
    body = []
    for row in rows:
        attrs = row_attributes(row) if row_attributes else ""
        qualified = bool(row.get("qualified", True))
        rank = int(row.get("overall_rank", 0) or 0)
        score = float(row.get("overall_score", -1))
        rank_html = f"#{rank}" if qualified and rank else "—"
        score_html = f"{score:.1f}" if qualified and score >= 0 else "Provisional"
        cells = []
        for key, label, decimals, suffix, bipolar in columns:
            value = float(row.get(key, 0) or 0)
            strength = abs(value) / maxima[key] if maxima[key] else 0.0
            if key == "full_clear" and maxima[key] > positive_minima[key]:
                # A lower clear time is better, so invert this column's heat.
                strength = (maxima[key] - value) / (maxima[key] - positive_minima[key])
            if bipolar and value < 0:
                background = f"rgba(240,121,131,{0.05 + 0.48 * strength:.3f})"
            else:
                background = f"rgba(89,197,139,{0.05 + 0.48 * strength:.3f})"
            metric_rank = row.get("metric_ranks", {}).get(key)
            peer_count = int(row.get("peer_count", 0) or 0)
            comparison = f" · Rank {metric_rank:g} of {peer_count}" if metric_rank is not None else " · Provisional sample"
            cells.append(
                f'<td class="analytics-heat-cell" data-sort="{value:.8f}" '
                f'style="background:{background}" title="{escape(label)}: {value:.{decimals}f}{escape(suffix)}{comparison}">'
                f'{value:.{decimals}f}{escape(suffix)}</td>'
            )
        body.append(
            f'<tr {attrs}><td class="analytics-player"><strong>{escape(str(row["name"]))}</strong></td>'
            f'<td class="analytics-rank" data-sort="{rank if rank else 9999}">{rank_html}</td>'
            f'<td class="analytics-score" data-sort="{score:.5f}">{score_html}</td>'
            f'<td class="number-cell" data-sort="{int(row["games"])}">{int(row["games"])}</td>{"".join(cells)}</tr>'
        )
    return (
        f'<div class="table-wrap analytics-table-wrap"><table class="sortable-table analytics-table {escape(table_class)}">'
        f'<thead><tr><th>Player</th><th data-type="number">Rank</th><th data-type="number">Overall Score</th><th data-type="number">Games</th>{headers}</tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>'
    )


def experimental_early_game_chart() -> str:
    records = experimental_player_records()
    grouped: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for record in records:
        grouped[(str(record["name"]), "ALL")].append(record)
        grouped[(str(record["name"]), str(record["role"]))].append(record)

    rows = []
    for (name, role), player_records in grouped.items():
        if role != "ALL" and len(player_records) < 3:
            continue
        games = len(player_records)
        row: dict[str, object] = {"name": name, "role": role, "games": games}
        for minute in (10, 15):
            for metric in ("gold", "xp", "cs", "level"):
                row[f"{metric}{minute}"] = sum(float(record["diff"][minute][metric]) for record in player_records) / games
        ahead = [record for record in player_records if float(record["diff"][15]["gold"]) > 0]
        behind = [record for record in player_records if float(record["diff"][15]["gold"]) < 0]
        row["ahead15"] = 100 * len(ahead) / games
        row["ahead_wr"] = 100 * sum(bool(record["win"]) for record in ahead) / len(ahead) if ahead else 0
        row["behind_wr"] = 100 * sum(bool(record["win"]) for record in behind) / len(behind) if behind else 0
        rows.append(row)
    columns = [
        ("gold10", "Gold Diff @10", 0, "", True), ("gold15", "Gold Diff @15", 0, "", True),
        ("xp10", "XP Diff @10", 0, "", True), ("xp15", "XP Diff @15", 0, "", True),
        ("cs10", "CS Diff @10", 1, "", True), ("cs15", "CS Diff @15", 1, "", True),
        ("level10", "Level Diff @10", 2, "", True), ("level15", "Level Diff @15", 2, "", True),
        ("ahead15", "Games Ahead @15", 1, "%", False),
        ("ahead_wr", "WR When Ahead @15", 1, "%", False),
        ("behind_wr", "WR When Behind @15", 1, "%", False),
    ]
    apply_overall_rankings(rows, columns, scope_key="role", minimum_games=10)
    rows.sort(key=lambda row: (str(row["role"]) != "ALL", str(row["role"]), int(row["overall_rank"]) or 9999, str(row["name"])))
    table = metric_table_html(
        rows,
        columns,
        "early-game-table",
        lambda row: f'data-early-role="{escape(str(row["role"]))}" style="{"" if row["role"] == "ALL" else "display:none"}"',
    )
    return f"""
    <section id="early-game-performance" class="section analytics-experiment">
      <div class="section-title"><div><h2>Early-Game Performance</h2><p class="note">Anonymous same-role opponent comparisons from timeline snapshots. Overall score is the equal-weight average of each metric's peer percentile.</p></div><label class="analytics-filter-label">Role <select id="early-role-filter" class="analytics-filter"><option value="ALL">All roles</option>{''.join(f'<option value="{role}">{role}</option>' for role in sorted(VALID_ROLES))}</select></label></div>
      <section class="table-panel"><div class="section-heading"><h3>Lane state at 10 and 15 minutes</h3><small>Ranked within selected role · 10 games to qualify</small></div>{table}</section>
    </section>
    """


def experimental_objective_chart() -> str:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for record in experimental_player_records():
        grouped[str(record["name"])].append(record)
    rows = []
    for name, player_records in grouped.items():
        games = len(player_records)
        minutes = sum(float(record["minutes"]) for record in player_records)
        total = lambda fn: sum(float(fn(record) or 0) for record in player_records)
        rows.append({
            "name": name, "games": games,
            "objective_damage_game": total(lambda r: r["participant"].get("damageDealtToObjectives", 0)) / games,
            "objective_damage_min": total(lambda r: r["participant"].get("damageDealtToObjectives", 0)) / minutes,
            "turret_damage": total(lambda r: r["participant"].get("damageDealtToTurrets", 0)) / games,
            "plates": total(lambda r: r["challenges"].get("turretPlatesTaken", 0)) / games,
            **{key: total(lambda r, objective=key: r["objectives"].get(objective, 0)) / games for key in ("dragon", "baron", "herald", "grub", "atakhan")},
            "steals": total(lambda r: r["participant"].get("objectivesStolen", 0)) / games,
            "no_smite_steals": total(lambda r: r["challenges"].get("epicMonsterStolenWithoutSmite", 0)) / games,
            "spawn_secures": total(lambda r: r["challenges"].get("epicMonsterKillsWithin30SecondsOfSpawn", 0)) / games,
            "near_enemy_jungler": total(lambda r: r["challenges"].get("epicMonsterKillsNearEnemyJungler", 0)) / games,
            "first_turret": 100 * total(lambda r: bool(r["participant"].get("firstTowerKill") or r["participant"].get("firstTowerAssist"))) / games,
            "conversion": 100 * total(lambda r: r["converted_takedowns"]) / total(lambda r: r["timeline_takedowns"]) if total(lambda r: r["timeline_takedowns"]) else 0,
        })
    columns = [
        ("objective_damage_game", "Objective Damage / Game", 0, "", False), ("objective_damage_min", "Objective Damage / Min", 1, "", False),
        ("turret_damage", "Turret Damage / Game", 0, "", False), ("plates", "Plates / Game", 2, "", False),
        ("dragon", "Dragon Involvement / Game", 2, "", False), ("baron", "Baron Involvement / Game", 2, "", False),
        ("herald", "Herald Involvement / Game", 2, "", False), ("grub", "Grub Involvement / Game", 2, "", False),
        ("atakhan", "Atakhan Involvement / Game", 2, "", False), ("steals", "Steals / Game", 3, "", False),
        ("no_smite_steals", "No-Smite Steals / Game", 3, "", False), ("spawn_secures", "Secures Within 30s Spawn / Game", 2, "", False),
        ("near_enemy_jungler", "Secures Near Enemy Jungler / Game", 2, "", False),
        ("first_turret", "First-Turret Involvement", 1, "%", False), ("conversion", "Takedown to Objective (90s)", 1, "%", False),
    ]
    apply_overall_rankings(rows, columns, minimum_games=10)
    rows.sort(key=lambda row: (int(row["overall_rank"]) or 9999, str(row["name"])))
    return f"""
    <section id="objective-contribution" class="section analytics-experiment">
      <div class="section-title"><div><h2>Objective Contribution</h2><p class="note">Personal objective pressure and timeline-confirmed participation. Overall score equally weights every displayed metric's peer percentile.</p></div></div>
      <section class="table-panel"><div class="section-heading"><h3>Objective pressure and conversion</h3><small>{len(rows)} tracked players · 10 games to qualify</small></div>{metric_table_html(rows, columns, "objective-table")}</section>
    </section>
    """


def experimental_mechanics_chart() -> str:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for record in experimental_player_records():
        grouped[str(record["name"])].append(record)
    rows = []
    for name, player_records in grouped.items():
        games = len(player_records)
        minutes = sum(float(record["minutes"]) for record in player_records)
        total = lambda fn: sum(float(fn(record) or 0) for record in player_records)
        rows.append({
            "name": name, "games": games,
            "hits": total(lambda r: r["challenges"].get("skillshotsHit", 0)) / games,
            "dodges": total(lambda r: r["challenges"].get("skillshotsDodged", 0)) / games,
            "close_dodges": total(lambda r: r["challenges"].get("dodgeSkillShotsSmallWindow", 0)) / games,
            "ability_casts": total(lambda r: sum(float(r["participant"].get(f"spell{slot}Casts", 0) or 0) for slot in range(1, 5))) / minutes,
            "summoner_casts": total(lambda r: float(r["participant"].get("summoner1Casts", 0) or 0) + float(r["participant"].get("summoner2Casts", 0) or 0)) / games,
            "cleanses": total(lambda r: r["challenges"].get("quickCleanse", 0)) / games,
            "flash_multikills": total(lambda r: r["challenges"].get("multikillsAfterAggressiveFlash", 0)) / games,
            "immobilisations": total(lambda r: r["challenges"].get("enemyChampionImmobilizations", 0)) / games,
            "saves": total(lambda r: r["challenges"].get("saveAllyFromDeath", 0)) / games,
            "low_hp": total(lambda r: r["challenges"].get("survivedSingleDigitHpCount", 0)) / games,
        })
    columns = [
        ("hits", "Skillshots Hit / Game", 1, "", False), ("dodges", "Skillshots Dodged / Game", 1, "", False),
        ("close_dodges", "Close Dodges / Game", 2, "", False), ("ability_casts", "Ability Casts / Min", 1, "", False),
        ("summoner_casts", "Summoner Casts / Game", 1, "", False), ("cleanses", "Quick Cleanses / Game", 3, "", False),
        ("flash_multikills", "Aggressive-Flash Multikills / Game", 3, "", False),
        ("immobilisations", "Enemies Immobilised / Game", 1, "", False), ("saves", "Allies Saved / Game", 2, "", False),
        ("low_hp", "Single-Digit HP Survivals / Game", 2, "", False),
    ]
    apply_overall_rankings(rows, columns, minimum_games=10)
    rows.sort(key=lambda row: (int(row["overall_rank"]) or 9999, str(row["name"])))
    return f"""
    <section id="mechanics-performance" class="section analytics-experiment">
      <div class="section-title"><div><h2>Mechanics</h2><p class="note">Execution and survival signals from Riot challenge and cast counters. Overall score equally weights every displayed metric's peer percentile.</p></div></div>
      <section class="table-panel"><div class="section-heading"><h3>Mechanical activity</h3><small>{len(rows)} tracked players · 10 games to qualify</small></div>{metric_table_html(rows, columns, "mechanics-table")}</section>
    </section>
    """


def experimental_jungle_chart() -> str:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for record in experimental_player_records():
        if record["role"] == "JUNGLE":
            grouped[str(record["name"])].append(record)
    rows = []
    for name, player_records in grouped.items():
        games = len(player_records)
        total = lambda fn: sum(float(fn(record) or 0) for record in player_records)
        clear_samples = [float(record["full_clear_minute"]) for record in player_records if record["full_clear_minute"] is not None]
        rows.append({
            "name": name, "games": games,
            "first_clear_cs": total(lambda r: r["snapshot"][5]["jungle_cs"]) / games,
            "jungle_cs10": total(lambda r: r["snapshot"][10]["jungle_cs"]) / games,
            "initial_buffs": total(lambda r: r["challenges"].get("initialBuffCount", 0)) / games,
            "initial_crabs": total(lambda r: r["challenges"].get("initialCrabCount", 0)) / games,
            "enemy_camps": total(lambda r: r["challenges"].get("enemyJungleMonsterKills", 0)) / games,
            "buffs_stolen": total(lambda r: r["challenges"].get("buffsStolen", 0)) / games,
            "scuttles": total(lambda r: r["challenges"].get("scuttleCrabKills", 0)) / games,
            "objective_involvement": total(lambda r: sum(float(value) for value in r["objectives"].values())) / games,
            "steals": total(lambda r: r["participant"].get("objectivesStolen", 0)) / games,
            "early_takedowns": total(lambda r: r["challenges"].get("takedownsFirstXMinutes", 0)) / games,
            "full_clear": sum(clear_samples) / len(clear_samples) if clear_samples else 0,
            "gold10": total(lambda r: r["diff"][10]["gold"]) / games, "gold15": total(lambda r: r["diff"][15]["gold"]) / games,
            "xp10": total(lambda r: r["diff"][10]["xp"]) / games, "xp15": total(lambda r: r["diff"][15]["xp"]) / games,
        })
    columns = [
        ("first_clear_cs", "Jungle CS @5 (First-Clear Proxy)", 1, "", False), ("jungle_cs10", "Jungle CS @10", 1, "", False),
        ("initial_buffs", "Initial Buffs", 2, "", False), ("initial_crabs", "Initial Crabs", 2, "", False),
        ("enemy_camps", "Enemy Jungle Monsters / Game", 1, "", False), ("buffs_stolen", "Buffs Stolen / Game", 2, "", False),
        ("scuttles", "Scuttles / Game", 2, "", False), ("objective_involvement", "Epic Objective Involvement / Game", 2, "", False),
        ("steals", "Objective Steals / Game", 3, "", False), ("early_takedowns", "Early Takedowns / Game", 2, "", False),
        ("full_clear", "Time to 24 Jungle CS", 2, " min", False),
        ("gold10", "Jungle Gold Diff @10", 0, "", True), ("gold15", "Jungle Gold Diff @15", 0, "", True),
        ("xp10", "Jungle XP Diff @10", 0, "", True), ("xp15", "Jungle XP Diff @15", 0, "", True),
    ]
    apply_overall_rankings(rows, columns, minimum_games=10, lower_is_better={"full_clear"})
    rows.sort(key=lambda row: (int(row["overall_rank"]) or 9999, str(row["name"])))
    return f"""
    <section id="jungle-performance" class="section analytics-experiment">
      <div class="section-title"><div><h2>Jungle Dashboard</h2><p class="note">Jungle-role games only. Overall score equally weights peer percentiles; a faster time to 24 jungle CS scores higher.</p></div></div>
      <section class="table-panel"><div class="section-heading"><h3>Pathing, invasion and objective control</h3><small>{len(rows)} tracked junglers · {sum(int(row['games']) for row in rows)} games · 10 games to qualify</small></div>{metric_table_html(rows, columns, "jungle-table")}</section>
    </section>
    """


def experimental_analytics_script_and_style() -> str:
    return """
    <style id="advanced-analytics-style">
      .analytics-table-wrap{max-height:720px;overflow:auto}.analytics-table{min-width:1500px}
      .analytics-table th{white-space:normal;min-width:105px;line-height:1.15}.analytics-table th:first-child{min-width:120px}
      .analytics-player{position:sticky;left:0;z-index:1;background:#111b27}.analytics-heat-cell{text-align:center;font-weight:800;font-variant-numeric:tabular-nums}
      .analytics-rank{font-size:1.05rem;font-weight:900;color:#f3cc68;text-align:center}.analytics-score{font-weight:900;color:#75dba6;text-align:center;white-space:nowrap}
      .analytics-filter-label{display:flex;align-items:center;gap:8px;color:#a9c9e8;font-weight:800}.analytics-filter{background:#111b27;color:#eaf4ff;border:1px solid #31445a;border-radius:7px;padding:8px 12px}
      .early-game-table{min-width:1450px}.objective-table,.jungle-table{min-width:1900px}.mechanics-table{min-width:1450px}
    </style>
    <script id="advanced-analytics-script">
      (() => {
        const filter = document.getElementById("early-role-filter");
        if (!filter) return;
        filter.addEventListener("change", () => {
          document.querySelectorAll("[data-early-role]").forEach(row => {
            row.style.display = row.dataset.earlyRole === filter.value ? "" : "none";
          });
        });
      })();
    </script>
    """


def order_custom_meta_by_tier(html: str) -> str:
    """Order meta spotlights and rows by tier, then contested score descending."""
    tier_order = {tier: index for index, tier in enumerate(("S", "A", "B", "C", "D", "E", "F"))}

    spotlight_pattern = re.compile(
        r'(<div class="meta-spotlight-grid">)(.*?)(</div>)',
        flags=re.DOTALL,
    )
    spotlight_match = spotlight_pattern.search(html)
    if spotlight_match:
        cards = re.findall(r'<article class="meta-spotlight-card.*?</article>', spotlight_match.group(2), flags=re.DOTALL)

        def card_key(card: str) -> tuple[int, float]:
            tier_match = re.search(r'tier-([a-z])', card)
            score_match = re.search(r'<b>[A-Z] Tier / ([0-9.]+)</b>', card)
            tier = tier_match.group(1).upper() if tier_match else "Z"
            score = float(score_match.group(1)) if score_match else 0.0
            return tier_order.get(tier, 99), -score

        ordered_cards = "\n            ".join(sorted(cards, key=card_key))
        html = html[:spotlight_match.start()] + spotlight_match.group(1) + "\n            " + ordered_cards + "\n            " + spotlight_match.group(3) + html[spotlight_match.end():]

    table_pattern = re.compile(
        r'(<table id="custom-meta-tier-list".*?<tbody>)(.*?)(</tbody>)',
        flags=re.DOTALL,
    )
    table_match = table_pattern.search(html)
    if table_match:
        rows = re.findall(r'<tr>.*?</tr>', table_match.group(2), flags=re.DOTALL)

        def row_key(row: str) -> tuple[int, float]:
            values = re.findall(r'data-sort="([^"]*)"', row)
            tier = values[0].upper() if values else "Z"
            score = float(values[-1]) if values else 0.0
            return tier_order.get(tier, 99), -score

        ordered_rows = "\n".join(sorted(rows, key=row_key))
        html = html[:table_match.start()] + table_match.group(1) + ordered_rows + table_match.group(3) + html[table_match.end():]
    return html


def roster_matches_section() -> str:
    rows = tracked_match_log()
    def champion_pairs_html(champions: list[str]) -> str:
        pairs = []
        for pair in champions:
            player, champion = pair.split(": ", 1)
            pairs.append(f"<strong>{escape(player)}</strong>: {escape(champion)}")
        return ", ".join(pairs)

    log_rows = "".join(
        f"<tr id=\"match-{row['number']}\" data-match-search=\"match {row['number']} {escape(str(row['id']))} {escape(' '.join(row['players']))} {escape(' '.join(row['champions']))}\"><td>Match {row['number']}</td><td class=\"match-id\"><a href=\"https://www.leagueofgraphs.com/match/euw/{escape(str(row['id']).removeprefix('EUW1_'))}\" target=\"_blank\" rel=\"noopener noreferrer\">{escape(str(row['id']))}</a></td><td>{escape(str(row['played']))}</td><td class=\"{'result-win' if row['result'] == 'Win' else 'result-loss'}\">{escape(str(row['result']))}</td><td>{escape(', '.join(row['players']))}</td><td>{champion_pairs_html(row['champions'])}</td><td>{row['minutes']}</td></tr>"
        for row in rows
    )
    matches = f"""
    <section id=\"matches\" class=\"section\">
      <div class=\"section-title\"><div><h2>Matches</h2><p class=\"note\">Roster-only log. Each row includes the tracked players who shared a Ranked Flex team.</p></div></div>
      <section class=\"table-panel\"><div class=\"section-heading\"><h3>Match database</h3><input id=\"match-db-search\" class=\"table-search\" type=\"search\" placeholder=\"Search match, player, or champion\"></div><div class=\"table-wrap\"><table class=\"sortable-table\"><thead><tr><th>Match #</th><th>Riot Match ID</th><th>Played</th><th>Result</th><th>Tracked players</th><th>Tracked champions</th><th>Minutes</th></tr></thead><tbody>{log_rows}</tbody></table></div></section>
    </section>
    <script>document.getElementById('match-db-search')?.addEventListener('input', event => {{ const query = event.target.value.toLowerCase(); document.querySelectorAll('#matches tbody tr').forEach(row => row.hidden = !row.dataset.matchSearch.toLowerCase().includes(query)); }});</script>
    """
    return matches


def tracked_combo_section() -> str:
    rows = tracked_match_log()
    minimum_games = {2: 20, 3: 15, 4: 15, 5: 5}
    panels = []
    for size in range(2, 6):
        records: dict[tuple[str, ...], dict[str, int]] = {}
        for row in rows:
            for group in combinations(sorted(row["players"]), size):
                record = records.setdefault(group, {"games": 0, "wins": 0})
                record["games"] += 1
                record["wins"] += int(row["result"] == "Win")
        qualified = [
            {
                "players": group,
                "games": record["games"],
                "wins": record["wins"],
                "winrate": record["wins"] / record["games"],
            }
            for group, record in records.items()
            if record["games"] >= minimum_games[size]
        ]
        best = sorted(qualified, key=lambda item: (-item["winrate"], -item["games"], item["players"]))[:10]
        worst = sorted(qualified, key=lambda item: (item["winrate"], -item["games"], item["players"]))[:10]

        def render_combo_rows(items: list[dict[str, object]]) -> str:
            if not items:
                return '<div class="empty-state">No tracked-player combination meets the minimum sample yet.</div>'
            return "".join(
                f"""
                <div class="combo-row" data-rank="{rank:02d}" style="--bar-accent: {winrate_color(float(item['winrate']))};">
                  <div class="combo-label"><span>{escape(' + '.join(item['players']))}</span><small>{item['wins']}-{item['games'] - item['wins']}, {item['games']} games</small></div>
                  <div class="bar-track"><div class="bar-fill" style="width: {item['winrate'] * 100:.2f}%"></div></div>
                  <b>{item['winrate'] * 100:.1f}%</b>
                </div>
                """
                for rank, item in enumerate(items, start=1)
            )

        label = {2: "Duos", 3: "Trios", 4: "Four-player teams", 5: "Five-player teams"}[size]
        panels.append(
            f"""
            <section class="chart-panel combo-comparison-panel">
              <div class="combo-comparison-heading"><h3>{label}</h3><p class="chart-note">Minimum sample: {minimum_games[size]} games together</p></div>
              <div class="combo-compare-grid">
                <div class="combo-compare-column combo-compare-best"><h4>Best {label}</h4><div class="combo-chart">{render_combo_rows(best)}</div></div>
                <div class="combo-compare-column combo-compare-worst"><h4>Worst {label}</h4><div class="combo-chart">{render_combo_rows(worst)}</div></div>
              </div>
            </section>
            """
        )
    return f"""
    <section id="combos" class="section">
      <div class="section-title"><div><h2>Best Tracked-Player Combos</h2><p class="note">Win-rate leaders when these tracked players appeared together on the same Ranked Flex team.</p></div></div>
      <div class="combo-comparison-stack">{''.join(panels)}</div>
    </section>
    """


def tracked_champion_history_section() -> str:
    rows = tracked_match_log()
    champions: dict[str, dict[str, object]] = {}
    for row in rows:
        for pair in row["champions"]:
            player, champion = pair.split(": ", 1)
            record = champions.setdefault(
                champion,
                {"players": Counter(), "matches": [], "wins": 0},
            )
            record["players"][player] += 1
            record["matches"].append((row["number"], row["id"]))
            record["wins"] += int(row["result"] == "Win")

    table_rows = []
    for champion, record in sorted(champions.items()):
        matches = record["matches"]
        games = len(matches)
        wins = int(record["wins"])
        players = ", ".join(
            f"{escape(name)} ({count})"
            for name, count in record["players"].most_common()
        )
        match_links = ", ".join(
            f'<a href="https://www.leagueofgraphs.com/match/euw/{escape(str(match_id).removeprefix("EUW1_"))}" target="_blank" rel="noopener noreferrer">Match {number}</a>'
            for number, match_id in matches
        )
        table_rows.append(
            f'<tr data-champion-history-search="{escape(champion)} {players}"><td><strong>{escape(champion)}</strong></td>'
            f'<td>{players}</td><td>{wins}-{games - wins}</td><td>{wins / games * 100:.1f}%</td>'
            f'<td><details><summary>{games} linked match{"es" if games != 1 else ""}</summary><div class="champion-match-links">{match_links}</div></details></td></tr>'
        )
    return f"""
    <section id="champion-history" class="section">
      <div class="section-title"><div><h2>Tracked Champion History</h2><p class="note">Roster-only champion pilots and their linked match history.</p></div></div>
      <section class="table-panel"><div class="section-heading"><h3>Champion pilots</h3><input id="champion-history-search" class="table-search" type="search" placeholder="Search champion or player"></div><div class="table-wrap"><table class="sortable-table"><thead><tr><th>Champion</th><th>Tracked players (games)</th><th>Record</th><th>Win rate</th><th>Match history</th></tr></thead><tbody>{''.join(table_rows)}</tbody></table></div></section>
    </section>
    <script>document.getElementById('champion-history-search')?.addEventListener('input', event => {{ const query = event.target.value.toLowerCase(); document.querySelectorAll('[data-champion-history-search]').forEach(row => row.hidden = !row.dataset.championHistorySearch.toLowerCase().includes(query)); }});</script>
    """


def render_with_qualification_thresholds() -> None:
    """Run the proven renderer while applying roster dashboard thresholds."""
    spec = importlib.util.spec_from_file_location("reference_dashboard_renderer", REFERENCE_GENERATOR)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the reference dashboard renderer.")
    renderer = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = renderer
    spec.loader.exec_module(renderer)
    renderer.MIN_CHAMPION_GAMES = 10
    renderer.MIN_COMBO_GAMES = 10
    # Enforce the roster boundary at the renderer input so every downstream
    # calculation (awards, charts, teams, showcases and experimental metrics)
    # receives tracked appearances only. Raw Riot payloads remain intact in
    # SQLite; this filter controls dashboard analytics and presentation.
    original_load_appearances = renderer.load_appearances
    allowed_names = tracked_real_names()
    def load_tracked_appearances(*args, **kwargs):
        raw_matches, appearances = original_load_appearances(*args, **kwargs)
        return raw_matches, [
            row
            for row in appearances
            if row.name in allowed_names
        ]
    renderer.load_appearances = load_tracked_appearances
    original_aggregate = renderer.aggregate
    renderer.aggregate = lambda appearances, keys: original_aggregate(
        [row for row in appearances if not renderer.is_spotlight_excluded_player(row.name)],
        keys,
    )
    original_role_champion_pool = renderer.role_champion_pool_rows
    renderer.role_champion_pool_rows = lambda appearances: original_role_champion_pool(
        [row for row in appearances if not renderer.is_spotlight_excluded_player(row.name)]
    )
    original_player_role_champion_pool = renderer.player_role_champion_pool_rows
    renderer.player_role_champion_pool_rows = lambda appearances: original_player_role_champion_pool(
        [row for row in appearances if not renderer.is_spotlight_excluded_player(row.name)]
    )
    original_build_awards = renderer.build_awards
    def build_roster_awards(appearances, *args, **kwargs):
        awards = original_build_awards(appearances, *args, **kwargs)
        winning_matches: dict[int, list[object]] = {}
        for appearance in appearances:
            parsed = renderer.parse_datetime(appearance.timestamp)
            if appearance.win and parsed is not None and parsed.year >= 2025:
                winning_matches.setdefault(appearance.match_id, []).append(appearance)
        if winning_matches:
            cleanest_rows = min(
                winning_matches.values(),
                key=lambda rows: (
                    sum(row.deaths for row in rows),
                    -sum(row.kills for row in rows),
                    rows[0].match_id,
                ),
            )
            deaths = sum(row.deaths for row in cleanest_rows)
            death_label = "death" if deaths == 1 else "deaths"
            winners = ", ".join(sorted({row.name for row in cleanest_rows}))
            for award in awards:
                if award.get("title") == "Cleanest Team Win":
                    award.update(
                        winner=f"Match {cleanest_rows[0].match_id}",
                        stat=f"{deaths} tracked team {death_label}",
                        detail=f"{cleanest_rows[0].date_label} - winners: {winners}",
                        match_id=cleanest_rows[0].match_id,
                    )
                    break
        awards.extend(advanced_stat_awards())
        return awards

    renderer.build_awards = build_roster_awards
    renderer.heat_color = winrate_color
    renderer.heat_text_color = winrate_text_color
    def horizontal_champion_pool(_player, rows):
        max_games = max((int(row["games"]) for row in rows), default=1)
        items = []
        for row in rows:
            champion = str(row["champion"])
            games = int(row["games"])
            games_label = "game" if games == 1 else "games"
            volume = max(3.0, games / max_games * 100)
            items.append(
                f'<div class="compact-champion-item" title="{escape(champion)}: {games} {games_label}">'
                f'<img src="{escape(renderer.champion_icon_url(champion))}" alt="{escape(champion)}">'
                f'<span class="champion-volume-track"><i style="width:{volume:.1f}%"></i></span>'
                f'<b>{games}g</b></div>'
            )
        rows_per_column = max(1, (len(items) + 2) // 3)
        columns = [
            f'<div class="champion-pool-column" style="display:grid;gap:5px;align-content:start">{"".join(items[start:start + rows_per_column])}</div>'
            for start in range(0, len(items), rows_per_column)
        ]
        return f'<div class="compact-champion-grid horizontal-chart">{"".join(columns)}</div>'

    def vertical_champion_pool(_player, rows):
        max_games = max((int(row["games"]) for row in rows), default=1)
        items = []
        for row in rows:
            champion = str(row["champion"])
            games = int(row["games"])
            games_label = "game" if games == 1 else "games"
            height = max(4.0, games / max_games * 100)
            items.append(
                f'<div class="vertical-champion-item" title="{escape(champion)}: {games} {games_label}">'
                f'<b>{games}</b><span class="vertical-volume-track"><i style="height:{height:.1f}%"></i></span>'
                f'<img src="{escape(renderer.champion_icon_url(champion))}" alt="{escape(champion)}"></div>'
            )
        return f'<div class="vertical-champion-strip">{"".join(items)}</div>'

    renderer.champion_pool_horizontal_svg = horizontal_champion_pool
    renderer.champion_pool_vertical_svg = vertical_champion_pool
    renderer.build_dashboard(EXPORTED_HISTORY, OUTPUT_DIRECTORY / "index.html")


def main() -> None:
    if not REFERENCE_GENERATOR.exists():
        raise RuntimeError(f"Reference generator not found: {REFERENCE_GENERATOR}")
    count = export_match_history()
    render_with_qualification_thresholds()
    # The original renderer needs ten entries per match to construct its full
    # head-to-head scoreboards. Those contain the anonymous structural rows,
    # so remove that raw match-history section after rendering. All aggregate
    # sections already honour the Anonymous prefix and therefore contain only
    # the tracked roster's statistics.
    index_path = OUTPUT_DIRECTORY / "index.html"
    rendered = index_path.read_text(encoding="utf-8")
    rendered = rendered.replace("LoL Customs Dashboard", "League Flex Dashboard")
    rendered = rendered.replace(
        "Browse one player at a time. Unique-pick rate is unique champions divided by games, so champion pool depth is not just raw volume.",
        "Browse one player at a time. Switch between compact horizontal and vertical icon charts; longer bars mean more games.",
    )
    rendered = rendered.replace(
        'class="orientation-button active" data-pool-orientation="horizontal"',
        'class="orientation-button" data-pool-orientation="horizontal"',
    )
    rendered = rendered.replace(
        'class="orientation-button" data-pool-orientation="vertical"',
        'class="orientation-button active" data-pool-orientation="vertical"',
    )
    rendered = rendered.replace(
        "player-pool-grid pool-orientation-horizontal",
        "player-pool-grid pool-orientation-vertical",
    )
    rendered = re.sub(r'\s*<div class="header-actions"[^>]*>.*?</div>', "", rendered, count=1, flags=re.DOTALL)
    rendered = re.sub(r'\s*<a href="#match-history">Matches</a>', "", rendered, count=1)
    rendered = re.sub(r'\s*<a href="#combos">Combos</a>', "", rendered, count=1)
    match_history_start = rendered.find('<section id="match-history"')
    if match_history_start >= 0:
        match_history_end = rendered.find("</section>", match_history_start)
        if match_history_end >= 0:
            rendered = rendered[:match_history_start] + rendered[match_history_end + len("</section>"):]
    # Combo calculations in the reference renderer are based on every slot in
    # a five-player team. Remove this section rather than mixing structural
    # placeholders into a roster-only view.
    combos_start = rendered.find('<section id="combos"')
    deep_dive_start = rendered.find('<section id="deep-dive"', combos_start)
    if combos_start >= 0 and deep_dive_start >= 0:
        rendered = rendered[:combos_start] + rendered[deep_dive_start:]
    # Awards involving an untracked teammate would otherwise combine their
    # game totals with the roster's. Remove those specific cards.
    rendered = re.sub(
        r'<a class="award-card(?:(?!</a>).)*?Anonymous opposition(?:(?!</a>).)*?</a>',
        "",
        rendered,
        flags=re.DOTALL,
    )
    rendered = re.sub(r"Anonymous opposition (TOP|JUNGLE|MID|BOT|SUPP)", "", rendered)
    matches = roster_matches_section()
    match_id_by_number = {int(row["number"]): str(row["id"]) for row in tracked_match_log() if row["number"]}
    rendered = re.sub(
        r"Match (\d+)(?!\s*[·#])",
        lambda match: f"Match {match.group(1)} · {match_id_by_number.get(int(match.group(1)), 'Riot ID unavailable')}",
        rendered,
    )
    rendered = re.sub(
        r'href="#match-(\d+)"\s+data-award-match-id="\d+"',
        lambda match: (
            f'href="https://www.leagueofgraphs.com/match/euw/'
            f'{match_id_by_number.get(int(match.group(1)), "").removeprefix("EUW1_")}" '
            f'target="_blank" rel="noopener noreferrer"'
        ),
        rendered,
    )
    # Keep Riot IDs in award-card destinations, but not in their visible copy
    # or accessible label. Present the supporting detail as one clean timeline.
    rendered = re.sub(
        r"Match (\d+)\s+[^A-Za-z0-9\s]\s+EUW1_\d+",
        r"Match \1",
        rendered,
    )
    rendered = re.sub(
        r"\s+-\s+Match (\d+),\s*([^,<]+),\s*(Win|Loss)(?=</small>)",
        r" &middot; Match \1 &middot; \2 &middot; \3",
        rendered,
    )
    roster_count = len(tracked_real_names())
    rendered = re.sub(
        r'(<span>Players</span>\s*<strong>)\d+(</strong>\s*<small>Unique real-name entries</small>)',
        rf'\g<1>{roster_count}\g<2>',
        rendered,
        count=1,
    )
    unplayed_start = rendered.find('<section class="chart-panel unplayed-panel">')
    if unplayed_start >= 0:
        unplayed_end = rendered.find("</section>", unplayed_start)
        if unplayed_end >= 0:
            rendered = rendered[:unplayed_start] + rendered[unplayed_end + len("</section>"):]
    tracked_combos = tracked_combo_section()
    champion_history = tracked_champion_history_section()
    rendered = rendered.replace("</nav>", '<a href="#combos">Combos</a><a href="#matches">Matches</a></nav>', 1)
    rendered = rendered.replace("</main>", tracked_combos + champion_history + matches + "</main>", 1)
    rendered = rendered.replace(
        "</head>",
        "<style>.match-id{font-family:monospace;white-space:nowrap}.result-win{color:#59c58b;font-weight:700}.result-loss{color:#f07983;font-weight:700}.champion-match-links{max-width:620px;padding:8px 0;line-height:1.8}.champion-match-links a{white-space:nowrap}.compact-champion-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:5px 10px}.compact-champion-item{display:grid;grid-template-columns:32px minmax(70px,1fr) 42px;align-items:center;gap:8px;min-height:40px;padding:4px 8px 4px 5px;background:#172231;border:1px solid #2a394b;border-radius:7px}.compact-champion-item img{width:30px;height:30px;object-fit:cover;border-radius:6px}.champion-volume-track{display:block;height:9px;overflow:hidden;background:#253448;border-radius:999px}.champion-volume-track i{display:block;height:100%;background:linear-gradient(90deg,#3e8ee8,#72b5ff);border-radius:inherit}.compact-champion-item b,.vertical-champion-item b{color:#dcecff;font-size:.8rem;font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}.vertical-chart-wrap{overflow-x:auto;padding:4px 2px 10px}.vertical-champion-strip{display:flex;align-items:flex-end;gap:7px;min-width:max-content;height:210px;padding:4px 8px}.vertical-champion-item{display:grid;grid-template-rows:18px 145px 32px;justify-items:center;gap:4px;width:34px;height:100%}.vertical-champion-item b{text-align:center}.vertical-volume-track{display:flex;align-items:flex-end;width:13px;height:145px;overflow:hidden;background:#253448;border-radius:5px}.vertical-volume-track i{display:block;width:100%;background:linear-gradient(0deg,#3e8ee8,#72b5ff);border-radius:inherit}.vertical-champion-item img{width:30px;height:30px;object-fit:cover;border-radius:6px;border:1px solid #3a4d63}@media(max-width:1050px){.compact-champion-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:650px){.compact-champion-grid{grid-template-columns:1fr}.compact-champion-item{grid-template-columns:30px minmax(60px,1fr) 38px}.compact-champion-item img{width:28px;height:28px}}</style></head>",
        1,
    )
    index_path.write_text(rendered, encoding="utf-8")
    # This renderer's experimental page intentionally defines a pocket pick as
    # a 2–4 game novelty. It conflicts with the roster dashboard's 10-game
    # qualification rule, so remove that separate card entirely.
    experimental_path = OUTPUT_DIRECTORY / "index_experimental.html"
    experimental = experimental_path.read_text(encoding="utf-8")
    experimental = re.sub(
        r'\s*<section class="section experimental-hero">.*?</section>',
        "",
        experimental,
        count=1,
        flags=re.DOTALL,
    )
    experimental = re.sub(
        r'<article class="lab-award-card(?:(?!</article>).)*?Best Pocket Pick(?:(?!</article>).)*?</article>',
        "",
        experimental,
        flags=re.DOTALL,
    )
    upset_start = experimental.find('<section id="upset-detector"')
    if upset_start >= 0:
        upset_end = experimental.find("</section>", upset_start)
        if upset_end >= 0:
            experimental = experimental[:upset_start] + experimental[upset_end + len("</section>"):]
    experimental_analytics = (
        experimental_early_game_chart().strip()
        + "\n"
        + experimental_objective_chart().strip()
        + "\n"
        + experimental_mechanics_chart().strip()
        + "\n"
        + experimental_jungle_chart().strip()
        + "\n"
        + experimental_vision_chart().strip()
        + "\n"
        + experimental_ping_chart().strip()
        + "\n"
        + experimental_analytics_script_and_style().strip()
        + "\n"
    )
    champion_cards_marker = '<section id="champion-ownership"'
    if champion_cards_marker in experimental:
        # Analytics stay above the very large champion-card gallery, leaving
        # that gallery as the final page section regardless of future tables.
        experimental = experimental.replace(
            champion_cards_marker,
            experimental_analytics + champion_cards_marker,
            1,
        )
    else:
        experimental = experimental.replace(
            "</main>", experimental_analytics + "</main>", 1
        )
    experimental = order_custom_meta_by_tier(experimental)
    experimental_path.write_text(experimental, encoding="utf-8")
    for page_path in OUTPUT_DIRECTORY.glob("*.html"):
        page = page_path.read_text(encoding="utf-8")
        page = re.sub(r'\s*<div class="header-actions"[^>]*>.*?</div>', "", page, count=1, flags=re.DOTALL)
        page = re.sub(r'\s*<a class="hidden-page-link"[^>]*></a>', "", page)
        page = re.sub(r'\s*<a href="(?:index_draft_coach\.html#draft-coach|#draft-coach)">Draft Coach</a>', "", page)
        page = re.sub(r'\s*<a href="(?:index_random_pool\.html#random-champion-pool|#random-champion-pool)">Random Pool</a>', "", page)
        page = re.sub(r'<nav>.*?</nav>', SHARED_NAV_HTML, page, count=1, flags=re.DOTALL)
        page = page.replace("</head>", SHARED_NAV_CSS + "</head>", 1)
        page_path.write_text(page, encoding="utf-8")
    for removed_page in ("index_draft_coach.html", "index_random_pool.html", "index_head_to_head.html"):
        (OUTPUT_DIRECTORY / removed_page).unlink(missing_ok=True)
    forbidden_site_markers = ("anonymous opposition", "tracked_teammate", "untracked player")
    for page_path in OUTPUT_DIRECTORY.glob("*.html"):
        page_text = page_path.read_text(encoding="utf-8").lower()
        found = [marker for marker in forbidden_site_markers if marker in page_text]
        if found:
            raise RuntimeError(f"Roster-boundary audit failed for {page_path}: {found}")
    print(f"Rendered {count} eligible flex games in reference dashboard style.")


if __name__ == "__main__":
    main()
