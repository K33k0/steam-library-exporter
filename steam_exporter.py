"""Export a Steam library and achievement progress using Python's standard library."""
import argparse
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import urlopen


class SteamError(Exception):
    pass


class SteamClient:
    def __init__(self, key, retries=3, delay=0.25):
        self.key, self.retries, self.delay = key, retries, delay

    def get(self, method, **params):
        url = 'https://api.steampowered.com/' + method + '/?' + urlencode({'key': self.key, **params})
        for attempt in range(self.retries + 1):
            time.sleep(self.delay)
            try:
                with urlopen(url, timeout=30) as response:
                    return json.load(response)
            except HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == self.retries:
                    raise SteamError(f'Steam returned HTTP {exc.code}') from None
                time.sleep(min(2 ** attempt, 30))
            except (URLError, TimeoutError, OSError):
                if attempt == self.retries:
                    raise SteamError('Steam request failed; check your connection and try again') from None
                time.sleep(2 ** attempt)
            except (ValueError, UnicodeError):
                raise SteamError('Steam returned an invalid JSON response') from None

    def resolve(self, profile):
        profile = profile.strip().rstrip('/')
        if '://' in profile:
            parsed = urlparse(profile)
            parts = parsed.path.strip('/').split('/')
            if parsed.hostname not in ('steamcommunity.com', 'www.steamcommunity.com') or len(parts) != 2 or parts[0] not in ('id', 'profiles'):
                raise SteamError('Use a Steam community profile URL, vanity name, or SteamID64')
            profile = parts[1]
        if profile.isdigit() and len(profile) == 17:
            return profile
        result = self.get('ISteamUser/ResolveVanityURL/v1', vanityurl=profile).get('response', {})
        if result.get('success') != 1:
            raise SteamError('Steam profile could not be resolved')
        return result['steamid']


def achievement_data(client, steamid, appid, language):
    result = {'status': 'unavailable', 'total': None, 'unlocked': None, 'completion_percent': None, 'items': []}
    try:
        schema = client.get('ISteamUserStats/GetSchemaForGame/v2', appid=appid, l=language)
        game = schema.get('game')
        if not isinstance(game, dict) or not game:
            raise SteamError('Achievement schema unavailable')
        definitions = game.get('availableGameStats', {}).get('achievements', [])
        result['total'] = len(definitions)
        result['items'] = [{**item, 'achieved': None, 'unlocktime': None} for item in definitions]
        if not definitions:
            result.update(status='no_achievements', unlocked=0)
            return result
        progress = client.get('ISteamUserStats/GetPlayerAchievements/v1', steamid=steamid, appid=appid, l=language).get('playerstats', {})
        if not progress.get('success') or not isinstance(progress.get('achievements'), list):
            raise SteamError('Player achievement progress unavailable (possibly private)')
        states = {item['apiname']: item for item in progress['achievements']}
        if any(item['name'] not in states for item in definitions):
            raise SteamError('Player achievement progress is incomplete')
        result['items'] = [{**item, 'achieved': bool(states[item['name']]['achieved']), 'unlocktime': states[item['name']].get('unlocktime', 0)} for item in definitions]
        unlocked = sum(item['achieved'] for item in result['items'])
        result.update(status='ok', unlocked=unlocked, completion_percent=round(unlocked / len(definitions) * 100, 2))
    except SteamError as exc:
        result['error'] = str(exc)
    return result


def export_library(client, profile, language='english', progress=None):
    steamid = client.resolve(profile)
    response = client.get('IPlayerService/GetOwnedGames/v1', steamid=steamid, include_appinfo=1, include_played_free_games=1, include_free_sub=1).get('response', {})
    if 'game_count' not in response:
        raise SteamError('Library unavailable. Check the API key, SteamID, and Steam Game details privacy setting')
    owned = response.get('games', [])
    if response['game_count'] != len(owned):
        raise SteamError('Steam returned an incomplete library; try again later')
    games = []
    for index, game in enumerate(sorted(owned, key=lambda g: (g.get('name', '').casefold(), g['appid'])), 1):
        if progress:
            progress(index, len(owned), game.get('name', str(game['appid'])))
        games.append({**game, 'hours_played': round(game.get('playtime_forever', 0) / 60, 4), 'achievements': achievement_data(client, steamid, game['appid'], language)})
    known = [g for g in games if g['achievements']['unlocked'] is not None]
    return {
        'schema_version': 1,
        'exported_at': datetime.now(timezone.utc).isoformat(),
        'steamid': steamid,
        'source': 'Steam Web API',
        'games': games,
        'summary': {
            'game_count': len(games),
            'hours_played': round(sum(g.get('playtime_forever', 0) for g in games) / 60, 4),
            'achievement_total_known': sum(g['achievements']['total'] or 0 for g in games),
            'achievement_unlocked_known': sum(g['achievements']['unlocked'] for g in known),
            'games_with_unavailable_achievements': len(games) - len(known),
            'achievement_counts_complete': len(known) == len(games),
        },
    }


def save_export(data, destination):
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False, suffix='.tmp') as stream:
            temporary = stream.name
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('profile', nargs='?', default=os.getenv('STEAM_PROFILE'), help='SteamID64, profile URL, or vanity name (or STEAM_PROFILE)')
    parser.add_argument('-o', '--output', default='exports/steam-library.json')
    parser.add_argument('--language', default='english')
    parser.add_argument('--strict', action='store_true', help='Exit 2 after saving if any achievement data is unavailable')
    args = parser.parse_args(argv)
    if not args.profile:
        parser.error('provide a Steam profile or set STEAM_PROFILE')
    key = os.getenv('STEAM_API_KEY') or getpass.getpass('Steam Web API key (hidden): ')
    if not key.strip():
        parser.error('a Steam Web API key is required')
    try:
        data = export_library(SteamClient(key.strip()), args.profile, args.language,
                              lambda i, n, name: print(f'[{i}/{n}] {name}', file=sys.stderr))
        save_export(data, args.output)
    except (SteamError, OSError) as exc:
        print(f'Export failed: {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Export cancelled.', file=sys.stderr)
        return 130
    summary = data['summary']
    print(f"Saved {summary['game_count']} games to {args.output}")
    missing = summary['games_with_unavailable_achievements']
    if missing:
        print(f'Warning: achievement progress unavailable for {missing} games; see per-game status.', file=sys.stderr)
    return 2 if args.strict and missing else 0


if __name__ == '__main__':
    sys.exit(main())
