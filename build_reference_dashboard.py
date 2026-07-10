"""Feed archived Ranked Flex games into the established custom-dashboard renderer."""
from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from html import escape
from itertools import combinations
from pathlib import Path


DATABASE = Path("data/riot_cache.sqlite3")
EXPORTED_HISTORY = Path("data/reference_match_history.json")
OUTPUT_DIRECTORY = Path("site")
REFERENCE_GENERATOR = Path(r"C:\Users\brand\Documents\Coding\Python\Custom_match_dashboards\data_analysis_customs.py")
ROLE_MAP = {"MIDDLE": "MID", "BOTTOM": "BOT", "UTILITY": "SUPP"}
VALID_ROLES = {"TOP", "JUNGLE", "MID", "BOT", "SUPP"}
MINIMUM_GAME_SECONDS = 10 * 60
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


def tracked_match_log() -> tuple[list[dict[str, object]], dict[str, list[str]]]:
    """Return roster-only match rows for the HTML log and player profiles."""
    with sqlite3.connect(DATABASE) as con:
        tracked = dict(con.execute("SELECT puuid, display_name FROM tracked_players WHERE puuid IS NOT NULL"))
        payloads = [json.loads(row[0]) for row in con.execute("SELECT payload FROM riot_payloads WHERE kind='match'")]
    match_numbers = {
        str(match.get("checksum", "")): index
        for index, match in enumerate(json.loads(EXPORTED_HISTORY.read_text(encoding="utf-8")), start=1)
    }
    rows, by_player = [], {name: [] for name in set(tracked.values())}
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
        tracked_picks = sorted(
            {
                (tracked[p["puuid"]], str(p.get("championName", "Unknown")))
                for p in roster_team
                if p.get("puuid") in tracked
            }
        )
        names = sorted({name for name, _champion in tracked_picks})
        match_id = str(payload.get("metadata", {}).get("matchId", ""))
        timestamp = info.get("gameEndTimestamp") or info.get("gameCreation")
        rows.append({
            "id": match_id,
            "number": match_numbers.get(match_id, 0),
            "played": datetime.fromtimestamp(timestamp / 1000, timezone.utc).strftime("%d %b %Y, %H:%M UTC") if timestamp else "-",
            "sort": timestamp or 0,
            "players": names,
            "champions": [f"{name} — {champion}" for name, champion in tracked_picks],
            "result": "Win" if roster_team[0].get("win") else "Loss",
            "minutes": round(info.get("gameDuration", 0) / 60, 1),
        })
        for name in names:
            by_player.setdefault(name, []).append(match_id)
    rows.sort(key=lambda row: int(row["sort"]), reverse=True)
    for ids in by_player.values():
        ids.sort(reverse=True)
    return rows, by_player


def roster_only_sections() -> tuple[str, str]:
    rows, by_player = tracked_match_log()
    profile_rows = "".join(
        f"<tr><td>{escape(name)}</td><td>{len(ids)}</td><td><details><summary>Show match IDs</summary><div class=\"match-id-list\">{escape(' · '.join(ids))}</div></details></td></tr>"
        for name, ids in sorted(by_player.items())
    )
    profiles = f"""
    <section class=\"table-panel roster-match-ids\">
      <div class=\"section-heading\"><h3>Player Match IDs</h3><small>Eligible flex matches involving at least two tracked players</small></div>
      <div class=\"table-wrap\"><table><thead><tr><th>Player</th><th>Eligible games</th><th>Match IDs</th></tr></thead><tbody>{profile_rows}</tbody></table></div>
    </section>
    """
    log_rows = "".join(
        f"<tr id=\"match-{row['number']}\" data-match-search=\"match {row['number']} {escape(str(row['id']))} {escape(' '.join(row['players']))} {escape(' '.join(row['champions']))}\"><td>Match {row['number']}</td><td class=\"match-id\"><a href=\"https://www.leagueofgraphs.com/match/euw/{escape(str(row['id']).removeprefix('EUW1_'))}\" target=\"_blank\" rel=\"noopener noreferrer\">{escape(str(row['id']))}</a></td><td>{escape(str(row['played']))}</td><td class=\"{'result-win' if row['result'] == 'Win' else 'result-loss'}\">{escape(str(row['result']))}</td><td>{escape(', '.join(row['players']))}</td><td>{escape(', '.join(row['champions']))}</td><td>{row['minutes']}</td></tr>"
        for row in rows
    )
    matches = f"""
    <section id=\"matches\" class=\"section\">
      <div class=\"section-title\"><div><h2>Matches</h2><p class=\"note\">Roster-only log. Each row includes the tracked players who shared a Ranked Flex team.</p></div></div>
      <section class=\"table-panel\"><div class=\"section-heading\"><h3>Match database</h3><input id=\"match-db-search\" class=\"table-search\" type=\"search\" placeholder=\"Search match, player, or champion\"></div><div class=\"table-wrap\"><table class=\"sortable-table\"><thead><tr><th>Match #</th><th>Riot Match ID</th><th>Played</th><th>Result</th><th>Tracked players</th><th>Tracked champions</th><th>Minutes</th></tr></thead><tbody>{log_rows}</tbody></table></div></section>
    </section>
    <script>document.getElementById('match-db-search')?.addEventListener('input', event => {{ const query = event.target.value.toLowerCase(); document.querySelectorAll('#matches tbody tr').forEach(row => row.hidden = !row.dataset.matchSearch.toLowerCase().includes(query)); }});</script>
    """
    return profiles, matches


def tracked_combo_section() -> str:
    rows, _by_player = tracked_match_log()
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

        def render_combo_rows(items: list[dict[str, object]], accent: str) -> str:
            if not items:
                return '<div class="empty-state">No tracked-player combination meets the minimum sample yet.</div>'
            return "".join(
                f"""
                <div class="combo-row" data-rank="{rank:02d}" style="--bar-accent: {accent};">
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
                <div class="combo-compare-column combo-compare-best"><h4>Best {label}</h4><div class="combo-chart">{render_combo_rows(best, '#62a8ff')}</div></div>
                <div class="combo-compare-column combo-compare-worst"><h4>Worst {label}</h4><div class="combo-chart">{render_combo_rows(worst, '#ff6f81')}</div></div>
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
    _profiles, matches = roster_only_sections()
    match_id_by_number = {int(row["number"]): str(row["id"]) for row in tracked_match_log()[0] if row["number"]}
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
    roster_count = len(tracked_match_log()[1])
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
    rendered = rendered.replace("</nav>", '<a href="#combos">Combos</a><a href="#matches">Matches</a></nav>', 1)
    rendered = rendered.replace("</main>", tracked_combos + matches + "</main>", 1)
    rendered = rendered.replace(
        "</head>",
        "<style>.roster-match-ids{margin:20px 0}.match-id-list{max-width:800px;padding:10px 0;color:#9fb9d1;line-height:1.7;word-break:break-all}.match-id{font-family:monospace;white-space:nowrap}.result-win{color:#59c58b;font-weight:700}.result-loss{color:#f07983;font-weight:700}</style></head>",
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
    print(f"Rendered {count} eligible flex games in reference dashboard style.")


if __name__ == "__main__":
    main()
