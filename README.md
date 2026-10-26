# Steam Library JSON Exporter

Export your Steam library to a reusable, UTF-8 JSON file. Runs on Windows, macOS, and Linux with **Python 3.10+**, with no extra packages.

- Every library game exposed by Steam, including unplayed titles and played free games.
- Hours played, original playtime in minutes, and all other game fields returned by Steam.
- Total and unlocked achievements per game, completion percentage, names, descriptions, icons, and unlock timestamps where available. Public Steam Community XML is used as a fallback when the Web API withholds player progress.
- Summary totals and explicit coverage flags when achievement data is unavailable.
- Concurrent fetching, persistent caching, shared request pacing, retries for temporary failures, hidden API-key entry, and atomic output replacement.

## First-time setup

1. Find your profile: open Steam, click your username, then **View my profile**. Copy the profile page URL. It looks like `https://steamcommunity.com/id/yourname/` or `https://steamcommunity.com/profiles/7656.../`. A vanity name or 17-digit SteamID64 also works.
2. In **Edit Profile → Privacy Settings**, make **My profile** and **Game details** public. Disable the option that keeps total playtime private if you want playtime exposed. You can restore your settings after exporting.
3. Get your own [Steam Web API key](https://steamcommunity.com/dev/apikey). Never put it in this repository or in a public issue.
4. Install [Python](https://www.python.org/downloads/) if needed. On Windows, enable its PATH option during installation.
5. Open a terminal in this folder and run:

```powershell
python steam_exporter.py "https://steamcommunity.com/id/yourname/"
```

Paste your API key at the hidden prompt. Output is saved to `exports/steam-library.json`. If Windows uses the Python launcher, substitute `py` for `python`.

## Repeated and automated exports

PowerShell, for the current terminal session:

```powershell
$env:STEAM_PROFILE = 'https://steamcommunity.com/id/yourname/'
# Set STEAM_API_KEY securely in your environment, or use the hidden prompt.
python steam_exporter.py --output exports/my-library.json
```

For unattended use, supply both `STEAM_PROFILE` and `STEAM_API_KEY` through your environment or a secret manager. The program does not read `.env` files. No key is included in the JSON or logged in request errors.

```text
python steam_exporter.py PROFILE [-o PATH] [--workers 8] [--refresh] [--strict]
```

The library and playtimes are fetched fresh each run. Definitions and player achievements use the cache described below. Progress appears as games complete; the final JSON stays sorted by game name and app ID. Exit codes: `0` = export saved (possibly partial achievement coverage); `1` = export failed; `2` = argument error, or `--strict` saved an export with unavailable achievement data; `130` = cancelled during export. Review the summary or use `--strict` in automation.

## Speed, caching and request limits

Eight workers overlap network waits by default. All workers share a limit of **four Web API request starts per second**, with Community XML paced separately at **one per second**. More workers do not multiply either rate. `--workers 1` runs sequentially; up to 16 workers are allowed.

The CLI stores responses and request accounting in `.steam-cache/state.sqlite3` (ignored by Git). Defaults:

| Data | Cache lifetime |
| --- | --- |
| Achievement definitions and explicit no-stats responses | 7 days |
| Player achievement progress, including public Community fallback | 5 minutes |
| Vanity-name resolution | 1 day |
| API 403 responses | 1 hour, to avoid repeated denied requests |
| Library and playtime | Always fresh |

For the fastest repeat exports, run normally. To refresh player progress while retaining definitions:

```text
python steam_exporter.py PROFILE --progress-ttl 0
```

To bypass all cache reads, use `--refresh`. It still enforces pacing, cooldowns and the request budget. You can set `--progress-ttl SECONDS` (0–86400), `--schema-ttl SECONDS` (0–2592000), or `--cache-dir PATH`. Longer progress TTLs trade freshness for fewer requests. Failed requests and rate-limit responses are not treated as successful cached data. A `fetch` section in the JSON reports actual network requests, cache hits, cache lifetimes and elapsed seconds. `exported_at` is the export creation time, not a claim that cached values were fetched at that instant.

Valve's [Web API terms](https://steamcommunity.com/dev/apiterms) limit use to **100,000 calls per day** and prohibit degrading the service. Valve does not publish a guaranteed safe per-second allowance in those terms. The four-per-second cap is this project's conservative operating choice, not a Valve-approved allowance or a guarantee against throttling. With this cache database, the exporter stops starting API requests at **90,000 attempts per key over a rolling 24 hours**, leaving headroom. Retries and failed attempts count. The ledger persists between runs, and SQLite coordinates pacing, budgets and cooldowns across processes sharing the same directory.

On HTTP 429, every worker using that host shares a cooldown, respects `Retry-After` (seconds or HTTP date), and reduces its request rate. Requests already in flight may finish. Long cooldowns stop further attempts until a later run instead of waiting indefinitely. A rate limit or exhausted budget never triggers a Community fallback. HTTP 403 is not retried. No key rotation, proxy rotation or quota evasion is used.

Calls made with the same key by other programs, machines or separate cache directories cannot be counted here. Use the same cache directory for this exporter and account for other usage; do not erase its ledger to evade limits. Cold exports still require many requests. Warm exports avoid most of them, but speed depends on cache age and Steam response times.

Cached Steam data (including profile identifiers and achievement progress) stays on the device running the exporter, alongside its local request ledger. The API key is never written to the cache; accounting uses a SHA-256 fingerprint. The project has no hosted data collection service. Keep both cache and personal exports out of public repositories.

## JSON format

`schema_version` identifies the export format. `exported_at` is UTC. SteamIDs are strings to preserve precision. Each `games` entry preserves Steam's raw game fields alongside:

```json
{
  "appid": 123,
  "name": "Example game",
  "playtime_forever": 90,
  "hours_played": 1.5,
  "achievements": {
    "status": "ok",
    "total": 2,
    "unlocked": 1,
    "completion_percent": 50.0,
    "items": [
      {"name": "FIRST", "displayName": "First", "achieved": true, "unlocktime": 1700000000},
      {"name": "LAST", "displayName": "Last", "achieved": false, "unlocktime": 0}
    ]
  }
}
```

This is an illustrative game entry. `unlocktime` is Unix seconds; zero means no unlock timestamp. Achievement statuses:

| Status | Meaning |
| --- | --- |
| `ok` | Schema and complete player progress returned |
| `no_achievements` | Available schema has no achievement definitions, or Steam explicitly reports that the app has no stats; total/unlocked are zero |
| `unavailable` | Schema or progress could not be fetched; unknown counts are `null`, with an `error` field |

When player progress is unavailable, known schema totals and definitions are retained. Unknown achievement states are `null`. A public Community XML fallback must match every schema achievement and the requested SteamID before its counts are accepted; these entries include `source: "Steam Community XML"`. Unlock timestamps omitted by that source remain `null` for unlocked achievements. Summary fields ending in `_known` sum only available values; check `achievement_counts_complete` before treating them as full-library totals.

## Scope and limitations

This exports the library Steam's Web API exposes for the account. It cannot guarantee a complete account/license inventory: private data, family-shared titles, non-Steam shortcuts, DLC/license records, and games omitted by Steam are outside the export. Steam may withhold or delay playtime and achievement information. A schema without achievement definitions is reported as `no_achievements`; this reflects the API response. Achievement failures do not remove the game from the output. The exporter does not sign into Steam or store your API key.

API references: [GetOwnedGames](https://partner.steamgames.com/doc/webapi/IPlayerService#GetOwnedGames), [GetSchemaForGame and GetPlayerAchievements](https://partner.steamgames.com/doc/webapi/ISteamUserStats).

## Python reuse

```python
import os
from steam_exporter import SteamClient, export_library, save_export

client = SteamClient(os.environ['STEAM_API_KEY'], cache_dir='.steam-cache')
try:
    data = export_library(client, os.environ['STEAM_PROFILE'], workers=8)
    save_export(data, 'exports/steam-library.json')
finally:
    client.store.close()
```

## Tests

```text
python -m unittest discover -s tests -v
```

Tests use mocked Steam responses and need no key or network. GitHub Actions runs them on Windows and Linux.

## Get the source

```text
git clone https://github.com/K33k0/steam-library-exporter.git
cd steam-library-exporter
```

Alternatively, use **Code → Download ZIP** on GitHub and extract it. Exports, `.env`, response caches and Python caches are ignored by Git. Keep your personal export private unless you intentionally want to share it.
