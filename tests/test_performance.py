from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from steam_exporter import SteamClient, SteamError, RateLimitError, achievement_data, export_library
from steam_performance import Store, BudgetError, retry_after


class Clock:
    def __init__(self):
        self.now = 100000.0
    def time(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


class PerformanceTests(unittest.TestCase):
    def test_persistent_quota_and_spacing_shared_across_connections(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'cache.sqlite3'
            a = Store(path, clock.time, clock.sleep)
            b = Store(path, clock.time, clock.sleep)
            try:
                a.acquire('api', .25, budget=2)
                b.acquire('api', .25, budget=2)
                self.assertEqual(clock.now, 100000.25)
                with self.assertRaises(BudgetError):
                    a.acquire('api', .25, budget=2)
                clock.now += 86400
                b.acquire('api', .25, budget=2)
            finally:
                a.close()
                b.close()

    def test_rate_limit_cooldown_shared_and_slows_future_starts(self):
        clock = Clock()
        store = Store(clock=clock.time, sleep=clock.sleep)
        try:
            store.acquire('api', .25)
            store.cooldown('api', 10, .25)
            store.acquire('api', .25)
            self.assertEqual(clock.now, 100010)
            store.acquire('api', .25)
            self.assertEqual(clock.now, 100010.5)
            store.cooldown('api', 3600, .25)
            with self.assertRaises(BudgetError):
                store.acquire('api', .25)
        finally:
            store.close()

    def test_parallel_workers_do_not_multiply_request_rate(self):
        store = Store()
        try:
            def send(_):
                store.acquire('api', .02)
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(send, range(8)))
            stamps = [r[0] for r in store.db.execute('SELECT stamp FROM calls ORDER BY stamp')]
            self.assertTrue(all(b - a >= .019 for a, b in zip(stamps, stamps[1:])))
        finally:
            store.close()

    def test_retry_after_seconds_and_http_date(self):
        self.assertEqual(retry_after('30', now=100), 30)
        stamp = datetime.fromtimestamp(130, timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')
        self.assertEqual(retry_after(stamp, now=100), 30)
        self.assertEqual(retry_after('nonsense'), 0)

    def test_response_cache_persists_isolates_language_profile_and_key(self):
        with tempfile.TemporaryDirectory() as folder:
            body = {'game': {'gameName': 'Example'}}
            with patch('steam_exporter.urlopen') as request:
                request.return_value.__enter__.return_value.read.return_value = json.dumps(body).encode()
                a = SteamClient('private-key', delay=0, cache_dir=folder)
                a.get('ISteamUserStats/GetSchemaForGame/v2', appid=1, l='english')
                a.store.close()
                b = SteamClient('private-key', delay=0, cache_dir=folder)
                try:
                    self.assertEqual(b.get('ISteamUserStats/GetSchemaForGame/v2', appid=1, l='english'), body)
                    self.assertEqual(request.call_count, 1)
                    b.get('ISteamUserStats/GetSchemaForGame/v2', appid=1, l='french')
                    self.assertEqual(request.call_count, 2)
                    raw = '\n'.join(str(r) for r in b.store.db.execute('SELECT * FROM cache'))
                    self.assertNotIn('private-key', raw)
                finally:
                    b.store.close()
                c = SteamClient('other-key', delay=0, cache_dir=folder)
                try:
                    c.get('ISteamUserStats/GetSchemaForGame/v2', appid=1, l='english')
                    self.assertEqual(request.call_count, 3)
                finally:
                    c.store.close()

    def test_player_cache_expires_and_refresh_bypasses_it(self):
        client = SteamClient('key', delay=0, progress_ttl=300)
        data = {'playerstats': {'success': True, 'achievements': []}}
        with patch('steam_exporter.urlopen') as request:
            request.return_value.__enter__.return_value.read.return_value = json.dumps(data).encode()
            try:
                method = 'ISteamUserStats/GetPlayerAchievements/v1'
                client.get(method, steamid='123', appid=1)
                client.get(method, steamid='123', appid=1)
                self.assertEqual(request.call_count, 1)
                client.get(method, steamid='456', appid=1)
                self.assertEqual(request.call_count, 2)
                # Expire the player payload while the longer schema TTL remains.
                key = json.dumps([client.scope, method, {'steamid': '123', 'appid': 1}], sort_keys=True)
                client.store.write(key, {'data': data, 'fetched_at': time.time()-301})
                client.get(method, steamid='123', appid=1)
                self.assertEqual(request.call_count, 3)
                client.refresh = True
                client.get(method, steamid='123', appid=1)
                self.assertEqual(request.call_count, 4)
            finally:
                client.store.close()

    def test_explicit_no_stats_cached_but_server_failures_not_cached(self):
        client = SteamClient('key', delay=0, retries=0)
        try:
            with patch('steam_exporter.urlopen', side_effect=HTTPError('url', 400, '', {}, io.BytesIO(b'{"playerstats":{"error":"Requested app has no stats"}}'))) as request:
                for _ in range(2):
                    with self.assertRaises(SteamError):
                        client.get('GetPlayerAchievements', appid=1)
                self.assertEqual(request.call_count, 1)
            with patch('steam_exporter.urlopen', side_effect=HTTPError('url', 503, '', {}, None)) as request:
                for _ in range(2):
                    with self.assertRaises(SteamError):
                        client.get('GetPlayerAchievements', appid=2)
                self.assertEqual(request.call_count, 2)
        finally:
            client.store.close()

    def test_429_never_uses_community_as_rate_limit_bypass(self):
        client = SteamClient('key', delay=0, retries=0)
        schema = {'game': {'availableGameStats': {'achievements': [{'name': 'FIRST'}]}}}
        try:
            with patch.object(client, 'get', side_effect=[schema, RateLimitError('limited')]), patch.object(client, 'community_achievements') as fallback:
                self.assertEqual(achievement_data(client, '123', 1, 'english')['status'], 'unavailable')
                fallback.assert_not_called()
            with patch('steam_exporter.urlopen', side_effect=HTTPError('url', 429, '', {'Retry-After': '3600'}, None)), patch.object(client.store, 'cooldown') as cooldown:
                with self.assertRaises(RateLimitError):
                    client.get('test')
                cooldown.assert_called_once_with(client.scope, 3600, 0)
        finally:
            client.store.close()

    def test_export_overlaps_games_but_keeps_stable_output_order(self):
        from test_exporter import FakeClient
        client = FakeClient()
        barrier = threading.Barrier(2)
        def progress_for_game(client, steamid, appid, language):
            barrier.wait(timeout=5)
            if appid == 1:
                time.sleep(.02)
            return {'status': 'no_achievements', 'total': 0, 'unlocked': 0, 'items': []}
        completed = []
        with patch('steam_exporter.achievement_data', side_effect=progress_for_game):
            data = export_library(client, 'example', workers=2, progress=lambda i,n,name: completed.append(name))
        self.assertEqual([g['appid'] for g in data['games']], [1, 2])
        self.assertEqual(completed, ['No achievements', 'Example'])
