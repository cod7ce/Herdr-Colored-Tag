import glob
import hashlib
import itertools
import os
from pathlib import Path
import sys
import tempfile
import unittest

try:
    import tomllib
except ImportError:  # Python 3.9-3.10
    tomllib = None

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from title_colors import ColorConfig, ColorConfigError


class FakeClient:
    socket_path = "/tmp/color-test.sock"

    def __init__(self, statuses=None):
        self.statuses = list(statuses or ["applied"])
        self.calls = []

    def call(self, method, **params):
        self.calls.append((method, params))
        status = self.statuses.pop(0) if self.statuses else "applied"
        return {"type": "config_reload", "status": status,
                "diagnostics": [] if status == "applied" else ["invalid sidebar config"]}


class ColorConfigTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.toml"
        self.client = FakeClient()
        self.colors = ColorConfig(self.client, self.path)

    def test_preserves_existing_config_and_emits_valid_toml(self):
        original = b"# user settings\n[ui]\npane_borders = 'always'\n"
        self.path.write_bytes(original)
        os.chmod(self.path, 0o640)
        rules = [
            {"equals": 'Dev "北" \\ tree', "fg": "#12aBcD"},
            {"equals": "测试", "fg": "#000000"},
        ]

        self.assertTrue(self.colors.apply(rules))
        data = self.path.read_bytes()
        self.assertTrue(data.startswith(original))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(len(glob.glob(str(self.path) + ".*.bak")), 1)
        self.assertEqual(self.client.calls, [("server.reload_config", {})])
        if tomllib:
            parsed = tomllib.loads(data.decode("utf-8"))
            rows = parsed["ui"]["sidebar"]["spaces"]["rows"]
            token = rows[0][1]
            self.assertEqual(token["token"], "workspace")
            self.assertEqual(token["rules"], [
                {"equals": 'Dev "北" \\ tree', "fg": "#12aBcD"},
                {"equals": "测试", "fg": "#000000"},
            ])
            self.assertEqual(rows[1], ["branch", "git_status"])

    def test_apply_is_idempotent_and_reset_restores_original_bytes(self):
        original = b"[ui]\npane_borders = 'always'"  # no final newline
        self.path.write_bytes(original)
        rules = [{"equals": "workspace", "fg": "#abcdef"}]

        self.assertTrue(self.colors.apply(rules))
        managed = self.path.read_bytes()
        self.assertTrue(managed.startswith(original + b"\n"))
        self.assertFalse(self.colors.apply(rules))
        self.assertEqual(self.path.read_bytes(), managed)
        self.assertEqual(len(self.client.calls), 1)

        self.assertTrue(self.colors.apply([]))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(len(self.client.calls), 2)

    def test_reset_preserves_sections_appended_after_managed_block(self):
        self.path.write_bytes(b"[ui]\npane_borders = 'always'\n")
        self.colors.apply([{"equals": "workspace", "fg": "#abcdef"}])
        later = b"\n[terminal]\ncopy_on_select = true\n"
        with self.path.open("ab") as stream:
            stream.write(later)
        self.colors.apply([{"equals": "other", "fg": "#112233"}])
        self.assertTrue(self.path.read_bytes().endswith(later))
        self.colors.apply([])
        self.assertTrue(self.path.read_bytes().endswith(later))
        if tomllib:
            self.assertTrue(tomllib.loads(self.path.read_text())["terminal"]["copy_on_select"])

    def test_unmanaged_spaces_table_and_dotted_key_are_refused(self):
        for existing in (b"[ui.sidebar.spaces]\nrows = []\n",
                         b"ui.sidebar.spaces = { rows = [] }\n",
                         b"ui.sidebar.spaces.rows = []\n"):
            with self.subTest(existing=existing):
                self.path.write_bytes(existing)
                before = self.path.read_bytes()
                with self.assertRaisesRegex(ColorConfigError, "already exists"):
                    self.colors.validate([{"equals": "work", "fg": "#abcdef"}])
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(self.client.calls, [])

    def test_invalid_rules_fail_before_any_file_or_client_effect(self):
        invalid = [
            [{"equals": "bad\nname", "fg": "#abcdef"}],
            [{"equals": "work", "fg": "red"}],
            [{"equals": "work", "fg": "#abcdef"}, {"equals": "work", "fg": "#000000"}],
            [{"equals": "work", "fg": "#abcdef", "bold": True}],
            [{"equals": f"work-{index}", "fg": "#abcdef"} for index in range(17)],
        ]
        for rules in invalid:
            with self.subTest(rules=rules), self.assertRaises(ColorConfigError):
                self.colors.validate(rules)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.client.calls, [])

    def test_reload_partial_or_failed_restores_original_and_reloads_it(self):
        original = b"[ui]\npane_borders = 'always'\n"
        self.path.write_bytes(original)
        self.client.statuses = ["partial", "applied"]

        with self.assertRaisesRegex(ColorConfigError, "config restored"):
            self.colors.apply([{"equals": "work", "fg": "#abcdef"}])

        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual([method for method, _ in self.client.calls],
                         ["server.reload_config", "server.reload_config"])
        self.assertEqual(len(glob.glob(str(self.path) + ".*.bak")), 1)

    def test_reload_exception_restores_original_file(self):
        original = b"[ui]\npane_borders = 'always'\n"
        self.path.write_bytes(original)

        class RaisingClient:
            def __init__(self):
                self.calls = 0

            def call(inner, method, **params):
                inner.calls += 1
                if inner.calls == 1:
                    raise RuntimeError("offline")
                return {"status": "applied", "diagnostics": []}

        client = RaisingClient()
        self.colors = ColorConfig(client, self.path)
        with self.assertRaisesRegex(ColorConfigError, "offline"):
            self.colors.apply([{"equals": "work", "fg": "#abcdef"}])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(client.calls, 2)

    def test_edited_managed_block_is_refused(self):
        self.colors.apply([{"equals": "work", "fg": "#abcdef"}])
        data = self.path.read_bytes().replace(b"#abcdef", b"#fedcba")
        self.path.write_bytes(data)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ColorConfigError, "was edited"):
            self.colors.apply([])
        self.assertEqual(self.path.read_bytes(), before)

    def test_foreign_session_cannot_overwrite_or_clear_managed_colors(self):
        self.client.socket_path = '/tmp/one.sock'
        first = ColorConfig(self.client, self.path)
        first.apply([{"equals": "work", "fg": "#abcdef"}])
        before = self.path.read_bytes()
        other = FakeClient()
        other.socket_path = '/tmp/two.sock'
        second = ColorConfig(other, self.path)
        self.assertFalse(second.apply([]))
        with self.assertRaisesRegex(ColorConfigError, "another session"):
            second.validate([{"equals": "work", "fg": "#123456"}])
        with self.assertRaisesRegex(ColorConfigError, "another session"):
            second.apply([{"equals": "work", "fg": "#123456"}])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(other.calls, [])

    def test_agent_workspace_rules_are_added_to_existing_empty_agents_table(self):
        original = b"[ui.sidebar.agents]\n# rows = [[\"state_icon\", \"workspace\"], [\"agent\"]]\n\n[ui]\ntheme = \"tokyo-night\"\n"
        self.path.write_bytes(original)
        rules = [{"equals": "🚧 数据构造", "fg": "#9ece6a"},
                 {"equals": "💽 造数能力", "fg": "#9ece6a"}]
        self.assertTrue(self.colors.validate_agent_workspace(rules) is not None)
        self.assertTrue(self.colors.apply_agent_workspace(rules))
        data = self.path.read_bytes()
        self.assertTrue(data.startswith(original[:original.index(b"\n\n[ui]\n")]))
        self.assertEqual(self.client.calls, [("server.reload_config", {})])
        if tomllib:
            parsed = tomllib.loads(data.decode("utf-8"))
            rows = parsed["ui"]["sidebar"]["agents"]["rows"]
            self.assertEqual(rows[0][:2], ["state_icon", "machine"])
            self.assertEqual(rows[0][2]["token"], "workspace")
            self.assertEqual(rows[0][2]["rules"], rules)
            self.assertEqual(rows[1], ["agent"])
            self.assertEqual(parsed["ui"]["theme"], "tokyo-night")
        self.assertFalse(self.colors.apply_agent_workspace(rules))
        self.assertEqual(self.client.calls, [("server.reload_config", {})])
        self.assertTrue(self.colors.apply_agent_workspace([]))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(len(self.client.calls), 2)

    def test_agent_workspace_layout_without_existing_table_is_removed_cleanly(self):
        original = b"[ui]\ntheme = \"tokyo-night\"\n"
        self.path.write_bytes(original)
        rules = [{"equals": "开发", "fg": "#7aa2f7"}]
        self.assertTrue(self.colors.apply_agent_workspace(rules))
        if tomllib:
            parsed = tomllib.loads(self.path.read_text())
            self.assertEqual(parsed["ui"]["sidebar"]["agents"]["rows"][0][2]["rules"], rules)
        self.assertTrue(self.colors.apply_agent_workspace([]))
        self.assertEqual(self.path.read_bytes(), original)

    def test_agent_workspace_layout_conflict_is_refused(self):
        for existing in (b"[ui.sidebar.agents]\nrows = [[\"agent\"]]\n",
                         b"ui.sidebar.agents.rows = []\n",
                         b"[ui.sidebar.agents.claude]\nrows = [[\"agent\"]]\n"):
            with self.subTest(existing=existing):
                self.path.write_bytes(existing)
                before = self.path.read_bytes()
                with self.assertRaisesRegex(ColorConfigError, "agents"):
                    self.colors.validate_agent_workspace([{"equals": "dev", "fg": "#abcdef"}])
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(self.client.calls, [])

    def test_edited_or_foreign_agent_color_block_is_protected(self):
        rules = [{"equals": "dev", "fg": "#abcdef"}]
        self.colors.apply_agent_workspace(rules)
        before = self.path.read_bytes()
        other_client = FakeClient()
        other_client.socket_path = "/tmp/another-agent-session.sock"
        self.assertFalse(ColorConfig(other_client, self.path).apply_agent_workspace([]))
        other_client.socket_path = "/tmp/third-agent-session.sock"
        with self.assertRaisesRegex(ColorConfigError, "another session"):
            ColorConfig(other_client, self.path).validate_agent_workspace(rules)
        edited = before.replace(b"#abcdef", b"#fedcba")
        self.path.write_bytes(edited)
        with self.assertRaisesRegex(ColorConfigError, "was edited"):
            self.colors.apply_agent_workspace([])
        self.assertEqual(self.path.read_bytes(), edited)

    def test_quoted_spaced_and_relative_custom_layouts_are_refused(self):
        templates = (
            b'  [ui.sidebar.SECTION.rows_by_agent]\n',
            b'[ui . sidebar . SECTION . rows_by_agent] # custom\n',
            b'["ui"."sidebar"."SECTION"."rows_by_agent"]\n',
            b"['ui' . 'sidebar' . 'SECTION' . 'rows_by_agent']\n",
            b'[ui.sidebar]\nSECTION.rows_by_agent = {}\n',
            b'[ui]\nsidebar."SECTION".rows = []\n',
            b'"ui" . "sidebar" . "SECTION" . rows = []\n',
            b'["\\u0075i" . sidebar . SECTION . rows_by_agent]\n',
            b'  [[ui . sidebar . SECTION]]\n',
            b'[ui.sidebar.SECTION] # custom\nrows = []\n',
            b'[ui.sidebar]\nSECTION = {rows = []}\n',
            b'ui = {sidebar = {SECTION = {rows = []}}}\n',
        )
        rules = [{"equals": "dev", "fg": "#abcdef"}]
        for section in (b'agents', b'spaces'):
            validate = self.colors.validate_agent_workspace if section == b'agents' else self.colors.validate
            apply = self.colors.apply_agent_workspace if section == b'agents' else self.colors.apply
            for template in templates:
                existing = template.replace(b'SECTION', section)
                with self.subTest(existing=existing):
                    if tomllib:
                        tomllib.loads(existing.decode())
                    self.path.write_bytes(existing)
                    for operation in (validate, apply):
                        with self.assertRaisesRegex(ColorConfigError, section.decode()):
                            operation(rules)
                    self.assertEqual(self.path.read_bytes(), existing)
                    self.assertEqual(self.client.calls, [])
        self.assertEqual(glob.glob(str(self.path) + '.*.bak'), [])

    def test_empty_agent_header_variants_are_reused(self):
        rules = [{"equals": "dev", "fg": "#abcdef"}]
        for header in (b'[ui.sidebar.agents] # keep', b'  [ui . sidebar . agents] # keep',
                       b'["ui"."sidebar"."agents"]', b"['ui'.'sidebar'.'agents']"):
            for ending in (b'', b'\n', b'\r\n'):
                original = b'# existing\n' + header + ending
                with self.subTest(original=original):
                    self.path.write_bytes(original)
                    self.colors.validate_agent_workspace(rules)
                    self.assertTrue(self.colors.apply_agent_workspace(rules))
                    data = self.path.read_bytes()
                    self.assertNotIn(b'\n[ui.sidebar.agents]\n', data)
                    if tomllib:
                        self.assertEqual(tomllib.loads(data.decode())['ui']['sidebar']['agents']['rows'][0][2]['rules'], rules)
                    self.assertFalse(self.colors.apply_agent_workspace(rules))
                    self.assertTrue(self.colors.apply_agent_workspace([]))
                    self.assertEqual(self.path.read_bytes(), original)

    def test_unrelated_tables_and_header_like_string_values_are_preserved(self):
        original = (
            b'[terminal]\n'
            b'note = """text\n[ui.sidebar.agents]\nui.sidebar.spaces.rows = []\n"""\n'
            b"literal = '''\n[ui.sidebar.spaces]\n'''\n"
            b'items = [\n"[ui.sidebar.agents.rows_by_agent]",\n]\n'
            b'[terminal.ui.sidebar.agents]\nrows = []\n'
            b'["ui.sidebar.agents"]\nrows = []\n'
            b'[ui.sidebar.agents_extra]\nrows = []\n'
            b'[ui.sidebar.spaces_extra]\nrows = []\n'
        )
        self.path.write_bytes(original)
        rules = [{"equals": "dev", "fg": "#abcdef"}]
        self.colors.apply_agent_workspace(rules)
        self.colors.apply(rules)
        if tomllib:
            tomllib.loads(self.path.read_text())
        self.colors.apply_agent_workspace([])
        self.colors.apply([])
        self.assertEqual(self.path.read_bytes(), original)

    def test_rows_only_agent_block_requires_original_enclosing_table(self):
        rules = [{"equals": "dev", "fg": "#abcdef"}]
        self.path.write_bytes(b'[ui.sidebar.agents]\n')
        self.colors.apply_agent_workspace(rules)
        managed = self.path.read_bytes()
        for replacement in (b'[terminal]', b'[ui.sidebar.agents.child]', b''):
            with self.subTest(replacement=replacement):
                edited = managed.replace(b'[ui.sidebar.agents]', replacement, 1)
                self.path.write_bytes(edited)
                calls = len(self.client.calls)
                for operation, values in ((self.colors.validate_agent_workspace, rules),
                                          (self.colors.apply_agent_workspace, rules),
                                          (self.colors.apply_agent_workspace, [])):
                    with self.assertRaisesRegex(ColorConfigError, 'agents'):
                        operation(values)
                self.assertEqual(self.path.read_bytes(), edited)
                self.assertEqual(len(self.client.calls), calls)

    def test_owned_agent_header_rejects_external_duplicate_base(self):
        rules = [{"equals": "dev", "fg": "#abcdef"}]
        self.colors.apply_agent_workspace(rules)
        managed = self.path.read_bytes()
        for extra in (b'[ui.sidebar.agents]\n', b'  [ui . sidebar . agents] # duplicate\n',
                      b'["ui"."sidebar"."agents"]\n'):
            for edited in (extra + managed, managed + extra):
                with self.subTest(edited=edited):
                    self.path.write_bytes(edited)
                    calls = len(self.client.calls)
                    for operation, values in ((self.colors.validate_agent_workspace, rules),
                                              (self.colors.apply_agent_workspace, rules),
                                              (self.colors.apply_agent_workspace, [])):
                        with self.assertRaisesRegex(ColorConfigError, 'agents'):
                            operation(values)
                    self.assertEqual(self.path.read_bytes(), edited)
                    self.assertEqual(len(self.client.calls), calls)

    def test_both_blocks_restore_exact_bytes_in_every_apply_and_clear_order(self):
        rules = [{"equals": "dev", "fg": "#abcdef"}]
        operations = (self.colors.apply, self.colors.apply_agent_workspace)
        originals = (b'', b'# user settings', b'[ui]\ntheme = "night"',
                     b'[ui]\ntheme = "night"\n', b'[ui.sidebar.agents]',
                     b'[ui.sidebar.agents]\n# keep this comment',
                     b'  [ui . sidebar . agents] # keep',
                     b'["ui"."sidebar"."agents"]\r\n# keep\r\n')
        for original, apply_order, clear_order in itertools.product(
                originals, itertools.permutations(operations), itertools.permutations(operations)):
            with self.subTest(original=original, apply_order=apply_order, clear_order=clear_order):
                self.path.write_bytes(original)
                for apply in apply_order:
                    self.assertTrue(apply(rules))
                self.colors.validate(rules)
                self.colors.validate_agent_workspace(rules)
                if tomllib:
                    parsed = tomllib.loads(self.path.read_text())
                    self.assertIn('rows', parsed['ui']['sidebar']['agents'])
                    self.assertIn('rows', parsed['ui']['sidebar']['spaces'])
                for apply in operations:
                    self.assertFalse(apply(rules))
                for clear in clear_order:
                    self.assertTrue(clear([]))
                self.assertEqual(self.path.read_bytes(), original)

    def test_agents_insert_before_foreign_spaces_without_changing_its_block(self):
        other_client = FakeClient()
        other_client.socket_path = '/tmp/foreign-spaces.sock'
        foreign = ColorConfig(other_client, self.path)
        rules = [{"equals": "dev", "fg": "#abcdef"}]
        for original in (b'[ui.sidebar.agents]', b'[ui.sidebar.agents]\n# keep',
                         b'[ui.sidebar.agents]\n'):
            for clear_spaces_first in (False, True):
                with self.subTest(original=original, clear_spaces_first=clear_spaces_first):
                    self.path.write_bytes(original)
                    foreign.apply(rules)
                    before = self.path.read_bytes()
                    start, end = foreign._locate(before)[:2]
                    foreign_bytes = before[start:end]
                    self.colors.apply_agent_workspace(rules)
                    data = self.path.read_bytes()
                    self.assertIn(foreign_bytes, data)
                    foreign.validate(rules)
                    self.colors.validate_agent_workspace(rules)
                    if clear_spaces_first:
                        foreign.apply([])
                        self.colors.apply_agent_workspace([])
                    else:
                        self.colors.apply_agent_workspace([])
                        self.assertEqual(self.path.read_bytes(), before)
                        foreign.apply([])
                    self.assertEqual(self.path.read_bytes(), original)

    def test_new_blocks_from_different_sessions_clear_in_every_order(self):
        foreign_client = FakeClient()
        foreign_client.socket_path = '/tmp/foreign-agent.sock'
        foreign = ColorConfig(foreign_client, self.path)
        rules = [{"equals": "dev", "fg": "#abcdef"}]
        operations = (self.colors.apply, foreign.apply_agent_workspace)
        for original, apply_order, clear_order in itertools.product(
                (b'[ui]\ntheme = "night"', b'[ui.sidebar.agents] # keep'),
                itertools.permutations(operations), itertools.permutations(operations)):
            with self.subTest(original=original, apply_order=apply_order, clear_order=clear_order):
                self.path.write_bytes(original)
                for apply in apply_order:
                    apply(rules)
                managed = self.path.read_bytes()
                self.assertFalse(self.colors.apply_agent_workspace([]))
                self.assertFalse(foreign.apply([]))
                self.assertEqual(self.path.read_bytes(), managed)
                for clear in clear_order:
                    clear([])
                self.assertEqual(self.path.read_bytes(), original)

    def test_legacy_spaces_after_empty_agent_header_keeps_integrity(self):
        rules = [{"equals": "dev", "fg": "#abcdef"}]
        for original in (b'[ui.sidebar.agents]', b'[ui.sidebar.agents]\n# keep'):
            for spaces_first in (True, False):
                with self.subTest(original=original, spaces_first=spaces_first):
                    legacy = original + b'\n' + self.colors._render(
                        rules, separator_newline=True, owner=self.colors.owner)
                    self.path.write_bytes(legacy)
                    self.colors.apply_agent_workspace(rules)
                    self.colors.validate(rules)
                    self.colors.validate_agent_workspace(rules)
                    if spaces_first:
                        self.colors.apply([])
                        self.colors.apply_agent_workspace([])
                    else:
                        self.colors.apply_agent_workspace([])
                        self.assertEqual(self.path.read_bytes(), legacy)
                        self.colors.apply([])
                    self.assertEqual(self.path.read_bytes(), original)

    def test_clear_retains_later_user_sections_with_missing_original_newline(self):
        rules = [{"equals": "dev", "fg": "#abcdef"}]
        original = b'[ui]\ntheme = "night"'
        later = b'[terminal]\ncopy_on_select = true\n'
        for apply in (self.colors.apply, self.colors.apply_agent_workspace):
            with self.subTest(apply=apply):
                self.path.write_bytes(original)
                apply(rules)
                with self.path.open('ab') as stream:
                    stream.write(later)
                apply([])
                self.assertEqual(self.path.read_bytes(), original + b'\n' + later)
                if tomllib:
                    tomllib.loads(self.path.read_text())

    def test_legacy_adjacent_markers_restore_missing_final_newline(self):
        rules = [{"equals": "dev", "fg": "#abcdef"}]
        renderers = (self.colors._render, self.colors._render_agent_workspace)
        operations = (self.colors.apply, self.colors.apply_agent_workspace)
        original = b'[ui]\ntheme = "night"'
        for order, clear_order in itertools.product(itertools.permutations((0, 1)),
                                                    itertools.permutations(operations)):
            with self.subTest(order=order, clear_order=clear_order):
                data = original + b'\n'
                data += renderers[order[0]](rules, separator_newline=True, owner=self.colors.owner)
                data += renderers[order[1]](rules, separator_newline=False, owner=self.colors.owner)
                self.path.write_bytes(data)
                self.assertFalse(self.colors.apply(rules))
                self.assertFalse(self.colors.apply_agent_workspace(rules))
                for clear in clear_order:
                    self.assertTrue(clear([]))
                self.assertEqual(self.path.read_bytes(), original)

    def test_legacy_shared_separator_cannot_rewrite_a_foreign_block(self):
        rules = [{"equals": "dev", "fg": "#abcdef"}]
        foreign_owner = hashlib.sha256(b'foreign').hexdigest().encode('ascii')
        renderers = (self.colors._render, self.colors._render_agent_workspace)
        operations = (self.colors.apply, self.colors.apply_agent_workspace)
        for first, second in itertools.permutations((0, 1)):
            with self.subTest(first=first):
                original = b'# no final newline\n'
                original += renderers[first](rules, separator_newline=True, owner=self.colors.owner)
                original += renderers[second](rules, separator_newline=False, owner=foreign_owner)
                self.path.write_bytes(original)
                with self.assertRaisesRegex(ColorConfigError, 'another session'):
                    operations[first]([])
                self.assertEqual(self.path.read_bytes(), original)
                self.assertEqual(self.client.calls, [])

    def test_empty_reset_without_managed_block_is_a_noop(self):
        self.path.write_bytes(b"[ui]\n")
        before = self.path.read_bytes()
        self.assertFalse(self.colors.apply([]))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
