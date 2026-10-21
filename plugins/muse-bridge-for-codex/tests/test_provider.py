"""Provider configuration must preserve unrelated user settings."""
import importlib.util
from pathlib import Path
import tomllib
import unittest

SPEC = importlib.util.spec_from_file_location('muse_provider', Path(__file__).resolve().parents[1] / 'provider.py')
provider = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provider)


class ConfigTests(unittest.TestCase):
    def test_preserves_unrelated_settings_and_is_idempotent(self):
        original = '# comment\nmodel = "old"\nweb_search = "disabled"\n[mcp_servers.test]\ncommand = "test"\n'
        actual = provider.config_text(original, 'http://127.0.0.1:1/secret', Path('/catalog.json'))
        parsed = tomllib.loads(actual)
        self.assertEqual('max', parsed['model_reasoning_effort'])
        self.assertEqual('muse', parsed['model_provider'])
        self.assertEqual('disabled', parsed['web_search'])
        self.assertEqual({'command': 'test'}, parsed['mcp_servers']['test'])
        self.assertTrue(actual.startswith('# comment\n'))
        self.assertEqual(actual, provider.config_text(actual, 'http://127.0.0.1:1/secret', Path('/catalog.json')))

    def test_unrelated_provider_name_is_not_overwritten(self):
        with self.assertRaisesRegex(ValueError, 'unrelated'):
            provider.config_text('[model_providers.muse]\nname="Other"\n', 'http://127.0.0.1', Path('/catalog'))

    def test_missing_final_newline_is_valid(self):
        actual = provider.config_text('model="old"', 'http://127.0.0.1', Path('/catalog'))
        self.assertEqual('max', tomllib.loads(actual)['model_reasoning_effort'])

    def test_isolated_and_user_configurations_cannot_share_a_backup(self):
        state = Path('/state')
        user = Path('/user/.codex/config.toml')
        isolated = Path('/smoke/config.toml')
        self.assertNotEqual(provider.backup_path(user, state), provider.backup_path(isolated, state))
        self.assertEqual(provider.backup_path(user, state), provider.backup_path(user, state))
