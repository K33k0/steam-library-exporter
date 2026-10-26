import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from steam_exporter import main


class LauncherTests(unittest.TestCase):
    def run_main(self, args):
        result = {'summary': {'game_count': 0, 'games_with_unavailable_achievements': 0}}
        with patch('steam_exporter.SteamClient') as client, patch('steam_exporter.export_library', return_value=result), patch('steam_exporter.save_export'), patch('builtins.print'):
            client.return_value.store.requests = 0
            client.return_value.store.hits = 0
            code = main(args)
        return code, client

    def test_missing_details_can_open_both_lookup_pages(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True), patch('builtins.input', side_effect=['', 'https://steamcommunity.com/id/example/']), patch('steam_exporter.getpass.getpass', side_effect=['', 'private-key']), patch('steam_exporter.webbrowser.open') as browser:
            code, client = self.run_main(['--interactive', '--config', str(Path(folder) / 'absent.json')])
            self.assertEqual(code, 0)
            self.assertEqual([call.args[0] for call in browser.call_args_list], ['https://steamcommunity.com/my/', 'https://steamcommunity.com/dev/apikey'])
            self.assertEqual(client.call_args.args[0], 'private-key')

    def test_local_config_skips_prompts(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            config = Path(folder) / 'local.json'
            config.write_text(json.dumps({'profile': 'example', 'api_key': 'private-key'}), encoding='utf-8')
            with patch('builtins.input') as prompt, patch('steam_exporter.getpass.getpass') as hidden, patch('steam_exporter.webbrowser.open') as browser:
                code, client = self.run_main(['--interactive', '--config', str(config)])
                self.assertEqual(code, 0)
                self.assertEqual(client.call_args.args[0], 'private-key')
                prompt.assert_not_called()
                hidden.assert_not_called()
                browser.assert_not_called()

    def test_invalid_config_fails_without_echoing_contents(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'local.json'
            config.write_text('{"api_key": "private-key", "profile": 123}', encoding='utf-8')
            with patch('builtins.print') as output:
                self.assertEqual(main(['--config', str(config)]), 1)
                self.assertNotIn('private-key', str(output.call_args_list))
