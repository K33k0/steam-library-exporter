# Steam Library JSON Exporter

Export your Steam library to a reusable, UTF-8 JSON file. Runs on Windows, macOS, and Linux with **Python 3.10+**, with no extra packages.

- Every library game exposed by Steam, including unplayed titles and played free games.
- Hours played, original playtime in minutes, and all other game fields returned by Steam.
- Total and unlocked achievements per game, completion percentage, names, descriptions, icons, and unlock timestamps where available. Public Steam Community XML is used as a fallback when the Web API withholds player progress.
- Summary totals and explicit coverage flags when achievement data is unavailable.
- Retries for temporary failures, hidden API-key entry, and atomic output replacement.

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
python steam_exporter.py PROFILE [-o PATH] [--language english] [--strict]
```

Each run fetches fresh data. Large libraries take several minutes because achievement data requires up to two requests per game, with pacing and retries. Progress appears in the terminal. Exit codes: `0` = export saved (possibly partial achievement coverage); `1` = export failed; `2` = argument error, or `--strict` saved an export with unavailable achievement data; `130` = cancelled during export. Review the summary or use `--strict` in automation.

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

data = export_library(SteamClient(os.environ['STEAM_API_KEY']), os.environ['STEAM_PROFILE'])
save_export(data, 'exports/steam-library.json')
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

Alternatively, use **Code → Download ZIP** on GitHub and extract it. Exports, `.env`, and Python caches are ignored by Git. Keep your personal export private unless you intentionally want to share it.
