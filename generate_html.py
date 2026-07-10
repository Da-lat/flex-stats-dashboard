"""Generate the static, shareable Flex dashboard from the local Riot SQLite archive."""
from __future__ import annotations

import html
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

DATABASE = Path("data/riot_cache.sqlite3")
OUTPUT = Path("site/index.html")


def number(value: float) -> str:
    return f"{value:,.0f}"


def database_rows() -> tuple[dict[str, str], list[dict]]:
    with sqlite3.connect(DATABASE) as con:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"tracked_players", "riot_payloads"}.issubset(tables):
            return {}, []
        names = dict(con.execute("SELECT puuid, display_name FROM tracked_players WHERE puuid IS NOT NULL"))
        raw = con.execute("SELECT payload FROM riot_payloads WHERE kind='match'").fetchall()
    return names, [json.loads(row[0]) for row in raw]


def cell(value: object) -> str:
    return f"<td>{html.escape(str(value))}</td>"


def main() -> None:
    names, matches = database_rows()
    if not names or not matches:
        raise RuntimeError("No downloaded matches found. Run python sync_riot.py first.")
    player_rows, champ_rows, pair_rows, games = [], [], Counter(), []
    for match in matches:
        info = match.get("info", {})
        grouped: dict[int, list[dict]] = defaultdict(list)
        for participant in info.get("participants", []):
            if participant.get("puuid") in names:
                grouped[participant.get("teamId")].append(participant)
        for team in grouped.values():
            if len(team) < 2:
                continue
            won = bool(team[0].get("win"))
            team_names = sorted(names[p["puuid"]] for p in team)
            for first in range(len(team_names)):
                for second in range(first + 1, len(team_names)):
                    pair_rows[(team_names[first], team_names[second], won)] += 1
            games.append({"id": match.get("metadata", {}).get("matchId", ""), "date": datetime.fromtimestamp((info.get("gameEndTimestamp") or info.get("gameCreation", 0)) / 1000, timezone.utc).strftime("%d %b %Y %H:%M UTC"), "players": ", ".join(team_names), "result": "WIN" if won else "LOSS", "minutes": round(info.get("gameDuration", 0) / 60, 1)})
            for p in team:
                player_rows.append({"name": names[p["puuid"]], "games": 1, "wins": int(won), "kills": p.get("kills", 0), "deaths": p.get("deaths", 0), "assists": p.get("assists", 0), "damage": p.get("totalDamageDealtToChampions", 0), "vision": p.get("visionScore", 0), "cs": p.get("totalMinionsKilled", 0) + p.get("neutralMinionsKilled", 0), "duration": max(info.get("gameDuration", 1) / 60, 1), "champion": p.get("championName", "Unknown")})
    aggregate: dict[str, Counter] = defaultdict(Counter)
    champions: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for row in player_rows:
        values = {key: value for key, value in row.items() if key not in {"name", "champion"}}
        aggregate[row["name"]].update(values)
        champions[(row["name"], row["champion"])].update(values)
    leaderboard = []
    for name, row in aggregate.items():
        leaderboard.append({"name": name, **row, "wr": row["wins"] / row["games"] * 100, "kda": (row["kills"] + row["assists"]) / max(1, row["deaths"]), "dpm": row["damage"] / row["duration"], "csm": row["cs"] / row["duration"]})
    leaderboard.sort(key=lambda item: (-item["wr"], -item["games"]))
    champion_list = sorted(({"name": n, "champion": c, **r, "wr": r["wins"] / r["games"] * 100} for (n, c), r in champions.items()), key=lambda r: (-r["games"], -r["wr"]))
    duos = []
    for first, second in sorted({key[:2] for key in pair_rows}):
        wins, losses = pair_rows[(first, second, True)], pair_rows[(first, second, False)]
        duos.append({"duo": f"{first} + {second}", "games": wins + losses, "wins": wins, "wr": wins / (wins + losses) * 100})
    duos.sort(key=lambda r: -r["games"])
    team_wins = sum(1 for game in games if game["result"] == "WIN")
    lead_html = "".join(
        "<tr>{}</tr>".format("".join(cell(value) for value in (
            r["name"], r["games"], r["wins"], f'{r["wr"]:.1f}%', f'{r["kda"]:.2f}',
            number(r["dpm"]), f'{r["csm"]:.1f}', number(r["vision"] / r["games"]),
        ))) for r in leaderboard
    )
    champ_html = "".join(
        "<tr>{}</tr>".format("".join(cell(value) for value in (
            r["name"], r["champion"], r["games"], f'{r["wr"]:.1f}%', r["kills"], r["deaths"], r["assists"],
        ))) for r in champion_list
    )
    duo_html = "".join(
        "<tr>{}</tr>".format("".join(cell(value) for value in (
            r["duo"], r["games"], r["wins"], f'{r["wr"]:.1f}%',
        ))) for r in duos
    )
    game_html = "".join(f"<tr><td><a href='https://www.leagueofgraphs.com/match/{html.escape(g['id'].replace('_', '/'))}' target='_blank'>{html.escape(g['id'])}</a></td>{cell(g['date'])}{cell(g['players'])}{cell(g['result'])}{cell(g['minutes'])}</tr>" for g in sorted(games, key=lambda r: r['date'], reverse=True))
    generated = datetime.now().strftime("%d %B %Y, %H:%M")
    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text(f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Flex Team Records</title><style>
    @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&family=Space+Grotesk:wght@600;700&display=swap');
    *{{box-sizing:border-box}} body{{margin:0;background:#07111f;color:#ebf2fc;font:15px 'DM Sans',sans-serif}} header{{padding:42px max(6vw,24px) 28px;background:radial-gradient(circle at 78% 0,#174d68 0,transparent 40%),linear-gradient(120deg,#101b34,#09121f)}} h1,h2{{font-family:'Space Grotesk',sans-serif}} h1{{font-size:clamp(30px,5vw,54px);margin:0}} p{{color:#a9bdd0}} main{{max-width:1440px;margin:auto;padding:28px 24px 70px}} .nav{{display:flex;gap:10px;flex-wrap:wrap;margin:20px 0}} .nav a{{color:#b9d7f7;text-decoration:none;border:1px solid #285174;padding:8px 12px;border-radius:99px}} .metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:15px;margin:24px 0}} .metric,.panel{{background:linear-gradient(145deg,#12253b,#0c1727);border:1px solid #1e4263;border-radius:16px;box-shadow:0 15px 40px #0004}} .metric{{padding:18px}} .metric small{{color:#86a8c9;text-transform:uppercase;letter-spacing:.08em}} .metric strong{{display:block;font:700 30px 'Space Grotesk';margin-top:7px}} .panel{{padding:18px;margin:20px 0;overflow:auto}} h2{{margin:0 0 5px;color:#cce8ff}} table{{width:100%;border-collapse:collapse;min-width:700px}} th{{color:#7eb8e8;text-align:left;text-transform:uppercase;font-size:11px;letter-spacing:.07em}} td,th{{padding:12px 9px;border-bottom:1px solid #1c334a}} tr:hover{{background:#19314a66}} td:nth-child(n+2){{font-variant-numeric:tabular-nums}} a{{color:#75caff}} .win{{color:#65deb2}} footer{{color:#7895b3;margin:32px 0 0}} @media(max-width:750px){{.metrics{{grid-template-columns:repeat(2,1fr)}}}}
    </style></head><body><header><h1>Ranked Flex Team Records</h1><p>Every tracked team game, generated from the local Riot API archive. Updated {generated}.</p><nav class="nav"><a href="#players">Player leaderboard</a><a href="#duos">Duo records</a><a href="#champions">Champion records</a><a href="#matches">Match history</a></nav></header><main>
    <section class="metrics"><div class="metric"><small>Eligible team games</small><strong>{len(games)}</strong></div><div class="metric"><small>Team win rate</small><strong class="win">{team_wins / len(games) * 100:.1f}%</strong></div><div class="metric"><small>Tracked players</small><strong>{len(names)}</strong></div><div class="metric"><small>Archived API matches</small><strong>{len(matches)}</strong></div></section>
    <section class="panel" id="players"><h2>Player leaderboard</h2><p>Damage, vision, CS, and KDA are from Riot’s full participant payload.</p><table><thead><tr><th>Player</th><th>Games</th><th>Wins</th><th>WR</th><th>KDA</th><th>Damage/min</th><th>CS/min</th><th>Vision/game</th></tr></thead><tbody>{lead_html}</tbody></table></section>
    <section class="panel" id="duos"><h2>Duo records</h2><table><thead><tr><th>Duo</th><th>Games</th><th>Wins</th><th>WR</th></tr></thead><tbody>{duo_html}</tbody></table></section>
    <section class="panel" id="champions"><h2>Champion records</h2><table><thead><tr><th>Player</th><th>Champion</th><th>Games</th><th>WR</th><th>Kills</th><th>Deaths</th><th>Assists</th></tr></thead><tbody>{champ_html}</tbody></table></section>
    <section class="panel" id="matches"><h2>Eligible match history</h2><table><thead><tr><th>Match ID</th><th>Played</th><th>Tracked teammates</th><th>Result</th><th>Minutes</th></tr></thead><tbody>{game_html}</tbody></table></section><footer>Raw match payloads and timelines are retained in <code>data/riot_cache.sqlite3</code>; generated HTML never includes your API key.</footer></main></body></html>''', encoding="utf-8")
    print(f"Wrote {OUTPUT.resolve()}")


if __name__ == "__main__":
    main()
