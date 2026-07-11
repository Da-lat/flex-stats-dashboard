# Ranked Flex Stats Dashboard

A static HTML dashboard for a fixed group of League players. It aggregates Ranked Flex games (`queue=440`) where at least two tracked players are on the same team.

## Setup

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .streamlit\secrets.toml.example .streamlit\secrets.toml
python sync_riot.py
python build_reference_dashboard.py
```

Put the Riot API key in `.streamlit/secrets.toml`. Add the fixed roster to `players.txt` (the dashboard loads it automatically), one player per line, for example:

```text
Wyn#EUW | Wyn
Welshy#CYMRU, Petez#Wales | Pete
```

Open `site/index.html` in a browser after generating it. The reference-style builder uses the proven renderer from `Custom_match_dashboards` to generate its full linked HTML dashboard suite. Re-run the two commands when you want to refresh.

## Manual refresh and publish

You can update the dashboard without using AI. From PowerShell in the project folder, run:

```powershell
python sync_riot.py
python build_reference_dashboard.py
```

The first command downloads and caches newly available Riot data. The second command rebuilds the static HTML files in `site/`.

To publish the refreshed dashboard to GitHub Pages, run:

```powershell
git add players.txt site
git commit -m "Refresh Riot Flex data"
git push
```

The complete refresh and publish sequence is:

```powershell
python sync_riot.py
python build_reference_dashboard.py
git add players.txt site
git commit -m "Refresh Riot Flex data"
git push
```

The Riot API key must be valid in `.streamlit/secrets.toml`. Existing matches and timelines are reused from SQLite, while match-ID lists refresh every 15 minutes. GitHub Pages deploys automatically after the push.

## Archive and cache behaviour

`data/riot_cache.sqlite3` is a persistent archive, not merely an aggregate cache. It stores the untouched JSON returned by Riot for accounts, summoners, ranks, match lists, every Match-V5 response, and every Match-V5 timeline. This preserves all available post-game participant fields, including damage, vision, objectives, wards, items, multikills, CS, gold and challenges, plus timeline frames and events.

Completed match and timeline payloads are never redownloaded. Match-ID lists refresh every 15 minutes and rank/profile data refreshes every 30 minutes.
