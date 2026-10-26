"""Export a Steam library and achievement progress using Python's standard library."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import getpass
import json
import os
import sqlite3
from pathlib import Path
import sys
import tempfile
import time
import webbrowser
import xml.etree.ElementTree as ET
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import urlopen
from steam_performance import Store, BudgetError, retry_after


class SteamError(Exception):
    pass


class NoStatsError(SteamError):
    """Steam explicitly reports that this app has no statistics."""


class RateLimitError(SteamError):
    pass


class SteamClient:
    def __init__(self, key, retries=3, delay=0.25, cache_dir=None,
                 refresh=False, schema_ttl=604800, progress_ttl=300):
        self.key, self.retries, self.delay = key, retries, delay
        self.store = Store(Path(cache_dir) / 'state.sqlite3' if cache_dir else None)
        self.scope = 'api:' + Store.fingerprint(key.strip().casefold())
        self.refresh, self.schema_ttl, self.progress_ttl = refresh, schema_ttl, progress_ttl

    def _request(self, url, scope, interval):
        for attempt in range(self.retries + 1):
            try:
                self.store.acquire(scope, interval)
            except BudgetError as exc:
                raise RateLimitError(str(exc)) from None
            try:
                with urlopen(url, timeout=30) as response:
                    return response.read()
            except HTTPError as exc:
                if exc.code == 400:
                    try:
                        body = json.load(exc)
                    except (ValueError, OSError, UnicodeError):
                        body = {}
                    if isinstance(body, dict) and body.get('playerstats', {}).get('error') == 'Requested app has no stats':
                        raise NoStatsError('Requested app has no stats') from None
                if exc.code == 429:
                    wait = max(5 * 2 ** attempt, retry_after(exc.headers.get('Retry-After') if exc.headers else None))
                    self.store.cooldown(scope, wait, interval)
                    if attempt == self.retries or wait > 120:
                        raise RateLimitError('Steam rate limited requests; retry later') from None
                elif exc.code in (500, 502, 503, 504) and attempt < self.retries:
                    time.sleep(2 ** attempt)
                else:
                    # A 403 is not retried. Cache it briefly to avoid repeatedly
                    # calling a denied API endpoint on subsequent exports.
                    raise SteamError(f'Steam returned HTTP {exc.code}') from None
            except (URLError, TimeoutError, OSError):
                if attempt == self.retries:
                    raise SteamError('Steam request failed; check your connection and try again') from None
                time.sleep(2 ** attempt)

    def get(self, method, **params):
        schema = 'GetSchemaForGame' in method
        ttl = self.schema_ttl if schema else self.progress_ttl if 'GetPlayerAchievements' in method else 86400 if 'ResolveVanityURL' in method else 0
        cache_key = json.dumps([self.scope, method, params], sort_keys=True)
        cached = None if self.refresh else self.store.read(cache_key, max(ttl, self.schema_ttl, 3600))
        if cached:
            age = time.time() - cached['fetched_at']
            if cached.get('no_stats') and age < self.schema_ttl:
                raise NoStatsError('Requested app has no stats')
            if cached.get('forbidden') and age < 3600:
                raise SteamError('Steam returned HTTP 403')
            if 'data' in cached and age < ttl:
                return cached['data']
            with self.store.lock:
                self.store.hits -= 1
        url = 'https://api.steampowered.com/' + method + '/?' + urlencode({'key': self.key, **params})
        try:
            data = json.loads(self._request(url, self.scope, self.delay))
            if not isinstance(data, dict):
                raise ValueError()
        except NoStatsError:
            self.store.write(cache_key, {'no_stats': True, 'fetched_at': time.time()})
            raise
        except SteamError as exc:
            if str(exc) == 'Steam returned HTTP 403':
                self.store.write(cache_key, {'forbidden': True, 'fetched_at': time.time()})
            raise
        except (ValueError, UnicodeError):
            raise SteamError('Steam returned an invalid JSON response') from None
        # Do not retain transient or private player failures as useful progress.
        if schema or 'GetPlayerAchievements' not in method or (data.get('playerstats', {}).get('success') and isinstance(data.get('playerstats', {}).get('achievements'), list)):
            self.store.write(cache_key, {'data': data, 'fetched_at': time.time()})
        return data

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

    def community_achievements(self, steamid, appid, language):
        """Read public Steam Community XML when the Web API denies progress."""
        url = f'https://steamcommunity.com/profiles/{steamid}/stats/{int(appid)}/?' + urlencode({'tab': 'achievements', 'xml': 1, 'l': language})
        cache_key = json.dumps(['community', steamid, appid, language])
        cached = None if self.refresh else self.store.read(cache_key, self.progress_ttl)
        if cached is not None:
            return cached
        try:
            root = ET.fromstring(self._request(url, 'community', 1))
        except RateLimitError:
            raise
        except (SteamError, ET.ParseError):
            raise SteamError('Public Steam Community achievement data unavailable') from None
        if root.tag != 'playerstats' or root.findtext('privacyState') != 'public' or root.findtext('player/steamID64') != steamid:
            raise SteamError('Public Steam Community achievement data unavailable')
        states = {}
        for item in root.findall('achievements/achievement'):
            name, closed = item.findtext('apiname'), item.get('closed')
            if not name or name in states or closed not in ('0', '1'):
                raise SteamError('Invalid Steam Community achievement data')
            stamp = item.findtext('unlockTimestamp')
            states[name] = {'achieved': closed == '1', 'unlocktime': int(stamp) if stamp and stamp.isdigit() else (0 if closed == '0' else None)}
        self.store.write(cache_key, states)
        return states


def achievement_data(client, steamid, appid, language):
    result = {'status': 'unavailable', 'total': None, 'unlocked': None, 'completion_percent': None, 'items': []}
    try:
        schema = client.get('ISteamUserStats/GetSchemaForGame/v2', appid=appid, l=language)
        game = schema.get('game')
        if not isinstance(game, dict) or not game:
            # Empty schemas also occur for apps without stats. Require Steam's
            # explicit no-stats response before assigning a real zero count.
            client.get('ISteamUserStats/GetPlayerAchievements/v1', steamid=steamid, appid=appid, l=language)
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
    except NoStatsError:
        result.update(status='no_achievements', total=0, unlocked=0, items=[])
    except RateLimitError as exc:
        # Do not switch hosts in response to rate limits or exhausted budgets.
        result['error'] = str(exc)
    except SteamError as exc:
        result['error'] = str(exc)
        fallback = getattr(client, 'community_achievements', None)
        if result['total'] and fallback:
            try:
                states = fallback(steamid, appid, language)
                names = {item['name'] for item in result['items']}
                if set(states) != names:
                    # Community XML lowercases IDs for some games. Accept that
                    # only when case folding is one-to-one on both sides.
                    folded = {name.casefold(): value for name, value in states.items()}
                    if len(folded) == len(states) == len(names) and len({name.casefold() for name in names}) == len(names) and set(folded) == {name.casefold() for name in names}:
                        states = {name: folded[name.casefold()] for name in names}
                if set(states) != {item['name'] for item in result['items']}:
                    raise SteamError('Incomplete Steam Community achievement data')
                result['items'] = [{**item, **states[item['name']]} for item in result['items']]
                unlocked = sum(item['achieved'] for item in result['items'])
                result.update(status='ok', unlocked=unlocked, completion_percent=round(unlocked / result['total'] * 100, 2), source='Steam Community XML')
                result.pop('error')
            except SteamError:
                pass
    return result


def export_library(client, profile, language='english', progress=None, workers=8):
    if not 1 <= workers <= 16:
        raise SteamError('workers must be between 1 and 16')
    steamid = client.resolve(profile)
    response = client.get('IPlayerService/GetOwnedGames/v1', steamid=steamid, include_appinfo=1, include_played_free_games=1, include_free_sub=1).get('response', {})
    if 'game_count' not in response:
        raise SteamError('Library unavailable. Check the API key, SteamID, and Steam Game details privacy setting')
    owned = response.get('games', [])
    if response['game_count'] != len(owned):
        raise SteamError('Steam returned an incomplete library; try again later')
    games = sorted(owned, key=lambda g: (g.get('name', '').casefold(), g['appid']))
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        tasks = {pool.submit(achievement_data, client, steamid, game['appid'], language): index for index, game in enumerate(games)}
        for completed, task in enumerate(as_completed(tasks), 1):
            index = tasks[task]
            game = games[index]
            games[index] = {**game, 'hours_played': round(game.get('playtime_forever', 0) / 60, 4), 'achievements': task.result()}
            if progress:
                progress(completed, len(owned), game.get('name', str(game['appid'])))
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
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
    parser.add_argument('--interactive', action='store_true', help='Prompt for a profile if one was not supplied')
    parser.add_argument('--config', default='steam-exporter.local.json', help='Optional local JSON with profile and api_key')
    parser.add_argument('--workers', type=int, default=8, help='Concurrent games, 1-16 (default: 8); rate limit is shared')
    parser.add_argument('--cache-dir', default='.steam-cache', help='Cache and shared request-budget directory')
    parser.add_argument('--refresh', action='store_true', help='Fetch fresh responses, bypassing cache reads; limits still apply')
    parser.add_argument('--progress-ttl', type=int, default=300, help='Reuse player progress for this many seconds (0-86400; default: 300)')
    parser.add_argument('--schema-ttl', type=int, default=604800, help='Reuse definitions/no-stats for this many seconds (0-2592000; default: 604800)')
    args = parser.parse_args(argv)
    config = {}
    try:
        if Path(args.config).exists():
            config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
            if not isinstance(config, dict) or any(not isinstance(config.get(field, ''), str) for field in ('profile', 'api_key')):
                raise ValueError('profile and api_key must be strings')
    except (OSError, ValueError) as exc:
        print(f'Cannot read local config: {type(exc).__name__}. Check the JSON file and its field types.', file=sys.stderr)
        return 1
    args.profile = args.profile or config.get('profile', '')
    if args.interactive and not args.profile:
        try:
            print('Find your profile: https://steamcommunity.com/my/')
            print('Sign in if needed, then copy the address of your profile page.')
            args.profile = input('Paste profile URL, SteamID64, or vanity name (Enter opens profile page): ').strip()
            if not args.profile:
                webbrowser.open('https://steamcommunity.com/my/')
                args.profile = input('Paste the profile page URL here: ').strip()
        except (EOFError, KeyboardInterrupt):
            print('Export cancelled.', file=sys.stderr)
            return 130
    if not args.profile:
        parser.error('provide a Steam profile or set STEAM_PROFILE')
    if not 1 <= args.workers <= 16 or not 0 <= args.progress_ttl <= 86400 or not 0 <= args.schema_ttl <= 2592000:
        parser.error('workers or cache TTL outside the allowed range')
    key = os.getenv('STEAM_API_KEY') or config.get('api_key', '')
    if not key:
        try:
            print('Find or register your key: https://steamcommunity.com/dev/apikey')
            print('Sign in to Steam, then copy the key shown on that page.')
            key = getpass.getpass('Paste API key (hidden; Enter opens key page): ')
            if not key and args.interactive:
                webbrowser.open('https://steamcommunity.com/dev/apikey')
                key = getpass.getpass('Paste API key (hidden): ')
        except (EOFError, KeyboardInterrupt):
            print('Export cancelled.', file=sys.stderr)
            return 130
    if not key.strip():
        parser.error('a Steam Web API key is required')
    try:
        client = SteamClient(key.strip(), cache_dir=args.cache_dir, refresh=args.refresh,
                             schema_ttl=args.schema_ttl, progress_ttl=args.progress_ttl)
        started = time.monotonic()
        try:
            data = export_library(client, args.profile, args.language,
                                  lambda i, n, name: print(f'[{i}/{n}] {name}', file=sys.stderr), workers=args.workers)
            data['fetch'] = {'requests': client.store.requests, 'cache_hits': client.store.hits,
                             'progress_cache_seconds': args.progress_ttl, 'schema_cache_seconds': args.schema_ttl,
                             'elapsed_seconds': round(time.monotonic() - started, 2)}
        finally:
            client.store.close()
        save_export(data, args.output)
    except (SteamError, OSError, sqlite3.Error) as exc:
        print(f'Export failed: {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Export cancelled.', file=sys.stderr)
        return 130
    summary = data['summary']
    print(f"Saved {summary['game_count']} games to {args.output}")
    print(f"{data['fetch']['requests']} requests, {data['fetch']['cache_hits']} cache hits in {data['fetch']['elapsed_seconds']}s")
    missing = summary['games_with_unavailable_achievements']
    if missing:
        print(f'Warning: achievement progress unavailable for {missing} games; see per-game status.', file=sys.stderr)
    return 2 if args.strict and missing else 0


if __name__ == '__main__':
    sys.exit(main())
