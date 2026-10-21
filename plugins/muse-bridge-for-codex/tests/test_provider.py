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
        self.assertIn('# comment\n', actual)
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

    def test_multiline_instructions_are_opaque_to_setting_edits(self):
        original = ('developer_instructions = """\nKeep this example:\nmodel = "example"\n'
                    '[model_providers.muse]\nname = "example"\n"""\nmodel = "old"\n')
        actual = provider.config_text(original, 'http://127.0.0.1', Path('/catalog'))
        self.assertEqual(tomllib.loads(original)['developer_instructions'],
                         tomllib.loads(actual)['developer_instructions'])

    def test_indented_unrelated_table_survives_provider_update(self):
        original = ('[model_providers.muse]\nname = "Muse Contributor (max only)"\n'
                    'base_url = "old"\n  [mcp_servers.unrelated]\ncommand = "keep"\n')
        actual = provider.config_text(original, 'http://127.0.0.1', Path('/catalog'))
        self.assertEqual({'command': 'keep'}, tomllib.loads(actual)['mcp_servers']['unrelated'])

    def test_inline_provider_maps_preserve_other_providers_and_reinstall(self):
        for key in ('model_providers', '"model_\\u0070roviders"'):
            original = (key + ' = {other = {name = "Keep", wire_api = "responses", '
                        'requires_openai_auth = true, request_max_retries = 2, '
                        'auth = {command = "helper", args = ["get"]}}}\n'
                        '[mcp_servers.keep]\ncommand="keep"\n')
            actual = provider.config_text(original, 'http://127.0.0.1', Path('/catalog'))
            parsed = tomllib.loads(actual)
            self.assertEqual(tomllib.loads(original)['model_providers']['other'], parsed['model_providers']['other'])
            self.assertEqual({'command': 'keep'}, parsed['mcp_servers']['keep'])
            self.assertEqual('responses', parsed['model_providers']['muse']['wire_api'])
            self.assertEqual(actual, provider.config_text(actual, 'http://127.0.0.1', Path('/catalog')))

    def test_non_ascii_paths_and_unrelated_nan_remain_valid(self):
        import math
        actual = provider.config_text('unrelated = nan\n', 'http://127.0.0.1', Path('/runtime/\U0001f431/catalog.json'))
        parsed = tomllib.loads(actual)
        self.assertTrue(math.isnan(parsed['unrelated']))
        self.assertEqual('/runtime/\U0001f431/catalog.json', parsed['model_catalog_json'])
