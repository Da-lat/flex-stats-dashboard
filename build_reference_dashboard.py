"""Feed archived Ranked Flex games into the established custom-dashboard renderer."""
from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import sys
from collections import Counter
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
                    "max_cs_advantage": p.get("challenges", {}).get("maxCsAdvantageOnLaneOpponent"),
                }
                for p in tracked_participants
            ],
            "result": "Win" if roster_team[0].get("win") else "Loss",
            "minutes": round(info.get("gameDuration", 0) / 60, 1),
        })
    rows.sort(key=lambda row: int(row["sort"]), reverse=True)
    return rows


def advanced_stat_awards() -> list[dict[str, object]]:
    totals: dict[str, dict[str, float]] = {}
    largest_cs_gap: tuple[float, str, dict[str, object], dict[str, object]] | None = None
    for match in tracked_match_log():
        for participant in match["participant_stats"]:
            name = str(participant["name"])
            record = totals.setdefault(name, {"games": 0, "damage": 0, "vision": 0})
            record["games"] += 1
            record["damage"] += int(participant["damage"])
            record["vision"] += int(participant["vision"])
            cs_gap = participant.get("max_cs_advantage")
            if cs_gap is not None:
                candidate = (float(cs_gap), name, participant, match)
                if largest_cs_gap is None or candidate[0] > largest_cs_gap[0]:
                    largest_cs_gap = candidate

    qualified = [(name, row) for name, row in totals.items() if row["games"] >= 10]
    highest_damage = max(qualified, key=lambda item: item[1]["damage"] / item[1]["games"])
    highest_vision = max(qualified, key=lambda item: item[1]["vision"] / item[1]["games"])
    damage_name, damage = highest_damage
    vision_name, vision = highest_vision
    awards = [
        {
            "title": "DPS Check",
            "winner": damage_name,
            "stat": f'{damage["damage"] / damage["games"]:,.0f} damage per game',
            "detail": f'Average damage to champions over {int(damage["games"])} games.',
            "theme": "red",
            "badge": "DPS",
        },
        {
            "title": "All Seeing",
            "winner": vision_name,
            "stat": f'{vision["vision"] / vision["games"]:.1f} vision per game',
            "detail": f'Average vision score over {int(vision["games"])} games.',
            "theme": "blue",
            "badge": "EYE",
        },
    ]
    if largest_cs_gap is not None:
        gap, name, participant, match = largest_cs_gap
        awards.append(
            {
                "title": "Player Gap",
                "winner": name,
                "stat": f"+{gap:,.1f} maximum CS advantage",
                "detail": f'{participant["champion"]} ({participant["role"]}) · Match {match["number"]}',
                "theme": "gold",
                "badge": "CS",
                "match_id": match["number"],
            }
        )
    return awards


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
