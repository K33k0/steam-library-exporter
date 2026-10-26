import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from steam_exporter import SteamClient, SteamError, achievement_data, export_library, save_export


class FakeClient:
    def __init__(self, private=False):
        self.private = private

    def resolve(self, profile):
        return '76561198000000000'

    def get(self, method, **params):
        if 'GetOwnedGames' in method:
            return {'response': {'game_count': 2, 'games': [
                {'appid': 1, 'name': 'Example', 'playtime_forever': 90},
                {'appid': 2, 'name': 'No achievements', 'playtime_forever': 0}]}}
        if 'GetSchema' in method:
            return {'game': {'gameName': 'Example', 'availableGameStats': {'achievements': [
                {'name': 'FIRST', 'displayName': 'First'}, {'name': 'LAST', 'displayName': 'Last'}] if params['appid'] == 1 else []}}}
        if self.private:
            return {'playerstats': {'success': False}}
        return {'playerstats': {'success': True, 'achievements': [
            {'apiname': 'FIRST', 'achieved': 1, 'unlocktime': 100},
            {'apiname': 'LAST', 'achieved': 0, 'unlocktime': 0}]}}


class ExportTests(unittest.TestCase):
    def test_full_export_counts_and_original_fields(self):
        data = export_library(FakeClient(), 'test')
        game = data['games'][0]
        self.assertEqual(game['hours_played'], 1.5)
        self.assertEqual(game['playtime_forever'], 90)
        self.assertEqual(game['achievements']['total'], 2)
        self.assertEqual(game['achievements']['unlocked'], 1)
        self.assertEqual(game['achievements']['completion_percent'], 50)
        self.assertEqual(data['games'][1]['achievements']['status'], 'no_achievements')
        self.assertTrue(data['summary']['achievement_counts_complete'])

    def test_private_progress_is_not_zero(self):
        data = export_library(FakeClient(private=True), 'test')
        stats = data['games'][0]['achievements']
        self.assertEqual(stats['total'], 2)
        self.assertIsNone(stats['unlocked'])
        self.assertIsNone(stats['items'][0]['achieved'])
        self.assertFalse(data['summary']['achievement_counts_complete'])
        self.assertEqual(data['summary']['games_with_unavailable_achievements'], 1)

    def test_missing_schema_is_not_no_achievements(self):
        client = FakeClient()
        client.get = lambda *a, **kw: {}
        self.assertIsNone(achievement_data(client, 'id', 1, 'english')['total'])
        with self.assertRaises(SteamError):
            export_library(client, 'test')

    def test_incomplete_progress_remains_unknown(self):
        client = FakeClient()
        original = client.get
        client.get = lambda method, **kw: {'playerstats': {'success': True, 'achievements': []}} if 'GetPlayerAchievements' in method else original(method, **kw)
        self.assertIsNone(achievement_data(client, 'id', 1, 'english')['unlocked'])

    def test_empty_library(self):
        client = FakeClient()
        client.get = lambda *a, **kw: {'response': {'game_count': 0}}
        self.assertEqual(export_library(client, 'test')['games'], [])

    def test_profile_resolution(self):
        client = SteamClient('secret')
        self.assertEqual(client.resolve('https://steamcommunity.com/profiles/76561198000000000/'), '76561198000000000')
        with patch.object(client, 'get', return_value={'response': {'success': 1, 'steamid': '123'}}) as request:
            self.assertEqual(client.resolve('https://steamcommunity.com/id/example'), '123')
            request.assert_called_once_with('ISteamUser/ResolveVanityURL/v1', vanityurl='example')
        with self.assertRaises(SteamError):
            client.resolve('https://example.com/id/example')

    def test_http_errors_do_not_expose_key(self):
        client = SteamClient('secret', retries=0, delay=0)
        with patch('steam_exporter.urlopen', side_effect=HTTPError('https://example.com/?key=secret', 403, 'Forbidden', {}, None)):
            with self.assertRaisesRegex(SteamError, '^Steam returned HTTP 403$'):
                client.get('test')

    def test_rate_limit_retry(self):
        client = SteamClient('secret', retries=1, delay=0)
        with patch('steam_exporter.urlopen', side_effect=HTTPError('url', 429, '', {}, None)) as request, patch('steam_exporter.time.sleep'):
            with self.assertRaises(SteamError):
                client.get('test')
            self.assertEqual(request.call_count, 2)

    def test_atomic_output_preserves_old_file_on_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'library.json'
            save_export({'name': 'Café'}, path)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'name': 'Café'})
            with self.assertRaises(TypeError):
                save_export({'bad': object()}, path)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'name': 'Café'})
            self.assertEqual(len(list(Path(folder).iterdir())), 1)


if __name__ == '__main__':
    unittest.main()
