import copy
import json
import tempfile
import unittest

from herdr_group import COLOR_HEX, GroupError, Service, Store, WorkspaceListChanged, agent_workspace_rules


def workspace(wid, label, **extra):
    return dict(workspace_id=wid, label=label, active_tab_id=wid + ':t1', **extra)


class FakeClient:
    socket_path = '/tmp/test-herdr-colored-tag.sock'

    def __init__(self):
        self.workspaces = [workspace('w1', '项目'), workspace('w2', '开发'),
                           workspace('w3', '测试'), workspace('w4', '其他')]
        self.calls = []
        self.fail_method = None

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == self.fail_method:
            self.fail_method = None
            raise GroupError('injected failure')
        if method == 'workspace.list':
            return {'workspaces': copy.deepcopy(self.workspaces)}
        if method == 'workspace.get':
            return {'workspace': copy.deepcopy(next(w for w in self.workspaces
                                                    if w['workspace_id'] == params['workspace_id']))}
        if method == 'workspace.rename':
            next(w for w in self.workspaces if w['workspace_id'] == params['workspace_id'])['label'] = params['label']
        elif method == 'workspace.move_block':
            self.workspaces.sort(key=lambda w: params['workspace_ids'].index(w['workspace_id']))
        elif method not in ('workspace.focus', 'plugin.pane.open'):
            raise AssertionError(method)
        return {}


class FakeColors:
    def __init__(self):
        self.rules = []
        self.agent_rules = []
        self.changes = 0
        self.fail_next = False

    def validate(self, rules):
        pass

    def validate_agent_workspace(self, rules):
        pass

    def apply_agent_workspace(self, rules):
        if self.agent_rules != rules:
            self.agent_rules = copy.deepcopy(rules)

    def apply(self, rules):
        if self.fail_next:
            self.fail_next = False
            raise GroupError('injected config failure')
        if self.rules != rules:
            self.changes += 1
            self.rules = copy.deepcopy(rules)


class TagsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = FakeClient()
        self.store = Store(self.temp.name, self.client.socket_path)
        self.colors = FakeColors()
        self.service = Service(self.client, self.store, 'w1', self.colors)

    def rows(self):
        return {w['workspace_id']: w for w in self.client.workspaces}

    def order(self):
        return [w['workspace_id'] for w in self.client.workspaces]

    def palette(self):
        return {rule['equals']: rule['fg'] for rule in self.colors.rules}

    def native_group(self):
        tree = dict(repo_key='/repo/.git', is_linked_worktree=False)
        self.rows()['w1']['worktree'] = tree
        self.rows()['w4']['worktree'] = dict(tree, is_linked_worktree=True)
        return tree

    def legacy(self):
        self.native_group()
        self.rows()['w1']['label'] = '├─ 项目'
        self.rows()['w2']['label'] = '└─ 开发'
        state = {'version': 1, 'parents': {'w1': 'w3', 'w2': 'w3'},
                 'labels': {'w1': {'base': '项目', 'applied': '├─ 项目', 'prefix': '├─ '},
                            'w2': {'base': '开发', 'applied': '└─ 开发', 'prefix': '└─ '}}}
        self.store.save(state)
        return state

    def test_tags_contiguous_native_titles_unchanged_no_metadata(self):
        before = {wid: row['label'] for wid, row in self.rows().items()}
        tag = self.service.set_tag(['w3', 'w1'], '工作', 'blue')
        self.assertEqual(self.order(), ['w1', 'w3', 'w2', 'w4'])
        self.assertEqual({wid: row['label'] for wid, row in self.rows().items()}, before)
        self.assertEqual(self.palette(), {'项目': COLOR_HEX['blue'], '测试': COLOR_HEX['blue']})
        self.assertEqual(self.store.load()['assignments'], {'w1': tag, 'w3': tag})
        self.assertFalse(any(method == 'workspace.rename' for method, _ in self.client.calls))

    def test_stable_tag_and_member_order_untagged_at_end(self):
        self.service.set_tag(['w3'], '第一')
        self.service.set_tag(['w4'], '第二')
        self.service.set_tag(['w2'], '第一')
        self.assertEqual(self.order(), ['w3', 'w2', 'w4', 'w1'])
        self.service.sync()
        self.assertEqual(self.order(), ['w3', 'w2', 'w4', 'w1'])

    def test_sync_idempotent_no_config_reload_after_sort(self):
        self.service.set_tag(['w3', 'w1'], '项目')
        self.client.calls.clear()
        changes = self.colors.changes
        for _ in range(5):
            self.service.sync()
        self.assertTrue(all(method == 'workspace.list' for method, _ in self.client.calls))
        self.assertEqual(self.colors.changes, changes)

    def test_linked_selection_tags_native_group_only_main_colored(self):
        self.native_group()
        tag = self.service.set_tag(['w4', 'w1', 'w2'], '项目')
        self.assertEqual(self.order(), ['w1', 'w4', 'w2', 'w3'])
        self.assertEqual(self.store.load()['assignments'], {'w1': tag, 'w2': tag})
        self.assertEqual(self.palette(), {'项目': COLOR_HEX['blue'], '开发': COLOR_HEX['blue']})
        self.assertEqual(self.rows()['w4']['label'], '其他')
        self.service.clear_tag(['w4'])
        self.assertNotIn('w1', self.store.load()['assignments'])
        self.assertNotIn('项目', self.palette())

    def test_new_native_member_remains_with_main(self):
        tree = self.native_group()
        self.service.set_tag(['w1'], '项目')
        self.client.workspaces.append(workspace('w5', '新分支', worktree=dict(tree, is_linked_worktree=True)))
        self.service.sync()
        self.assertEqual(self.order()[:3], ['w1', 'w4', 'w5'])
        self.assertNotIn('新分支', self.palette())

    def test_duplicate_mains_receive_color_and_anchor_close_transfers_tag(self):
        tree = self.native_group()
        self.client.workspaces.append(workspace('w5', '副主目录', worktree=tree))
        tag = self.service.set_tag(['w5'], '项目')
        self.assertEqual(self.palette()['副主目录'], self.palette()['项目'])
        self.client.workspaces = [w for w in self.client.workspaces if w['workspace_id'] != 'w1']
        self.service.sync()
        self.assertEqual(self.store.load()['assignments'], {'w5': tag})
        self.assertEqual(self.order()[:2], ['w5', 'w4'])

    def test_last_linked_closes_main_retains_tag(self):
        self.native_group()
        tag = self.service.set_tag(['w1'], '项目')
        self.client.workspaces = [w for w in self.client.workspaces if w['workspace_id'] != 'w4']
        self.service.sync()
        self.assertEqual(self.store.load()['assignments'], {'w1': tag})

    def test_dissolved_native_group_keeps_tag_on_all_survivors(self):
        tree = self.native_group()
        self.client.workspaces.append(workspace('w5', '副主目录', worktree=tree))
        tag = self.service.set_tag(['w1'], '项目')
        self.client.workspaces = [w for w in self.client.workspaces if w['workspace_id'] != 'w4']
        self.service.sync()
        self.assertEqual(self.store.load()['assignments'], {'w1': tag, 'w5': tag})
        self.assertEqual(self.palette()['副主目录'], COLOR_HEX['blue'])

    def test_main_tag_wins_when_separate_units_merge(self):
        a = self.service.set_tag(['w1'], 'A')
        self.service.set_tag(['w4'], 'B')
        self.native_group()
        self.service.sync()
        self.assertEqual(self.store.load()['assignments'], {'w1': a})
        self.assertNotIn('其他', self.palette())

    def test_orphan_linked_tag_moves_to_new_main(self):
        self.rows()['w4']['worktree'] = dict(repo_key='/repo/.git', is_linked_worktree=True)
        tag = self.service.set_tag(['w4'], '项目')
        self.assertIn('其他', self.palette())
        self.rows()['w1']['worktree'] = dict(repo_key='/repo/.git', is_linked_worktree=False)
        self.service.sync()
        self.assertEqual(self.store.load()['assignments'], {'w1': tag})
        self.assertNotIn('其他', self.palette())
        self.assertEqual(self.palette()['项目'], COLOR_HEX['blue'])

    def test_color_change_updates_all_same_tag_titles(self):
        tag = self.service.set_tag(['w1', 'w2'], '项目')
        self.service.update_tag(tag, color='purple', name='性能')
        self.assertEqual(self.palette(), {'项目': COLOR_HEX['purple'], '开发': COLOR_HEX['purple']})
        self.assertEqual(self.store.load()['tags'][tag]['name'], '性能')

    def test_tag_order_moves_native_units_and_persists_after_restart(self):
        self.native_group()
        first = self.service.set_tag(['w1', 'w2'], '第一', 'blue')
        second = self.service.set_tag(['w3'], '第二', 'purple')
        labels = {wid: row['label'] for wid, row in self.rows().items()}
        colors, changes = copy.deepcopy(self.colors.rules), self.colors.changes
        assignments = self.store.load()['assignments']
        self.assertTrue(self.service.move_tag(second, 'up'))
        self.assertEqual(list(self.store.load()['tags']), [second, first])
        self.assertEqual(self.order(), ['w3', 'w1', 'w4', 'w2'])
        self.assertEqual(self.store.load()['assignments'], assignments)
        self.assertEqual({wid: row['label'] for wid, row in self.rows().items()}, labels)
        self.assertEqual(self.colors.rules, colors)
        self.assertEqual(self.colors.changes, changes)
        restored = Service(self.client, Store(self.temp.name, self.client.socket_path), 'w1', self.colors)
        self.client.workspaces.reverse()
        restored.sync()
        self.assertEqual(self.order(), ['w3', 'w2', 'w1', 'w4'])
        self.assertTrue(restored.move_tag(second, 'down'))
        self.assertEqual(list(self.store.load()['tags']), [first, second])
        self.assertEqual(self.order(), ['w2', 'w1', 'w4', 'w3'])

    def test_empty_tag_order_boundaries_and_untagged_spaces(self):
        first = self.service.set_tag(['w1'], '第一')
        empty = self.service.set_tag(['w2'], '空标签')
        self.service.clear_tag(['w2'])
        self.assertTrue(self.service.move_tag(empty, 'up'))
        self.assertEqual(list(self.store.load()['tags']), [empty, first])
        before = self.store.path.read_bytes()
        self.assertFalse(self.service.move_tag(empty, 'up'))
        self.assertFalse(self.service.move_tag(first, 'down'))
        self.assertEqual(self.store.path.read_bytes(), before)
        self.service.set_tag(['w3'], '空标签')
        self.assertEqual(self.order(), ['w3', 'w1', 'w2', 'w4'])
        third = self.service.set_tag(['w4'], '第三')
        self.assertEqual(list(self.store.load()['tags']), [empty, first, third])
        self.assertEqual(self.order(), ['w3', 'w1', 'w4', 'w2'])

    def test_invalid_tag_move_does_not_mutate(self):
        tag = self.service.set_tag(['w1'], '第一')
        before, order = self.store.path.read_bytes(), self.order()
        for tid, direction in [('missing', 'up'), (tag, 'sideways')]:
            with self.assertRaises(GroupError):
                self.service.move_tag(tid, direction)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.order(), order)

    def test_tag_order_survives_apply_failure_for_retry(self):
        first = self.service.set_tag(['w1'], '第一')
        second = self.service.set_tag(['w2'], '第二')
        self.client.fail_method = 'workspace.move_block'
        with self.assertRaises(GroupError):
            self.service.move_tag(second, 'up')
        self.assertEqual(list(self.store.load()['tags']), [second, first])
        self.service.sync()
        self.assertEqual(self.order(), ['w2', 'w1', 'w3', 'w4'])

    def test_agent_workspace_colors_follow_space_labels_and_native_group(self):
        self.native_group()
        tag = self.service.set_tag(['w1', 'w2'], '开发', 'blue')
        rules = agent_workspace_rules(self.store.load(), self.client.workspaces)
        self.assertEqual(rules, [{'equals': '其他', 'fg': COLOR_HEX['blue']},
                                 {'equals': '开发', 'fg': COLOR_HEX['blue']},
                                 {'equals': '项目', 'fg': COLOR_HEX['blue']}])
        self.assertEqual(self.colors.agent_rules, rules)
        self.service.update_tag(tag, color='yellow')
        self.assertEqual(self.colors.agent_rules, [{'equals': '其他', 'fg': COLOR_HEX['yellow']},
                                                   {'equals': '开发', 'fg': COLOR_HEX['yellow']},
                                                   {'equals': '项目', 'fg': COLOR_HEX['yellow']}])
        self.assertIn('其他', {rule['equals'] for rule in self.colors.agent_rules})
        self.service.clear_tag(['w1', 'w2'])
        self.assertFalse(self.colors.agent_rules)

    def test_agent_workspace_same_name_conflict_is_rejected(self):
        self.rows()['w1']['label'] = '同名'
        self.rows()['w3']['label'] = '同名'
        state = {'tags': {'blue': {'name': '蓝组', 'color': 'blue'},
                          'yellow': {'name': '黄组', 'color': 'yellow'}},
                 'assignments': {'w1': 'blue', 'w3': 'yellow'}, 'native_blocks': {}, 'labels': {}}
        with self.assertRaisesRegex(GroupError, '同名 Space'):
            agent_workspace_rules(state, self.client.workspaces)

    def test_agent_rule_limit_counts_linked_labels_before_agents_start(self):
        tree = self.native_group()
        self.client.workspaces.extend(
            workspace('linked-' + str(i), '分支-' + str(i),
                      worktree=dict(tree, is_linked_worktree=True))
            for i in range(15)
        )
        with self.assertRaisesRegex(GroupError, '16'):
            self.service.set_tag(['w1'], '开发', 'blue')
        self.assertFalse(self.store.path.exists())
        self.assertEqual(self.colors.rules, [])
        self.assertEqual(self.colors.agent_rules, [])

    def test_same_name_reuses_tag_and_color(self):
        a = self.service.set_tag(['w1'], '项目', 'orange')
        b = self.service.set_tag(['w2'], '项目')
        self.assertEqual(a, b)
        self.assertEqual(self.palette()['开发'], COLOR_HEX['orange'])

    def test_external_rename_updates_exact_match_without_name_mutation(self):
        self.service.set_tag(['w1'], '项目')
        self.rows()['w1']['label'] = '新名称'
        self.service.sync()
        self.assertEqual(self.palette(), {'新名称': COLOR_HEX['blue']})
        self.service.clear_tag(['w1'])
        self.assertEqual(self.rows()['w1']['label'], '新名称')
        self.assertFalse(self.palette())

    def test_explicit_rename_keeps_tag(self):
        self.service.set_tag(['w1'], '项目')
        self.service.rename('w1', '新名称')
        self.assertEqual(self.rows()['w1']['label'], '新名称')
        self.assertEqual(self.palette(), {'新名称': COLOR_HEX['blue']})

    def test_reset_preserves_metadata_native_provenance_and_spaces(self):
        tree = self.native_group()
        self.rows()['w1']['tokens'] = {'ticket': 'ABC-1'}
        self.service.set_tag(['w1', 'w2'], '项目')
        self.service.reset()
        self.assertEqual(self.rows()['w1']['tokens'], {'ticket': 'ABC-1'})
        self.assertEqual(self.rows()['w1']['worktree'], tree)
        self.assertFalse(self.store.load()['assignments'])
        self.assertFalse(self.palette())
        self.assertEqual(len(self.client.workspaces), 4)

    def test_restart_replays_config_without_recreating_spaces(self):
        self.service.set_tag(['w2'], '项目')
        config = FakeColors()
        Service(self.client, self.store, 'w1', config).sync()
        self.assertEqual(config.rules, self.colors.rules)

    def test_config_failure_preserves_intent_for_retry(self):
        self.colors.fail_next = True
        with self.assertRaises(GroupError):
            self.service.set_tag(['w2'], '项目')
        self.assertIn('w2', self.store.load()['assignments'])
        self.service.sync()
        self.assertEqual(self.palette(), {'开发': COLOR_HEX['blue']})

    def test_legacy_migration_backs_up_and_restores_names(self):
        original = self.legacy()
        self.service.sync()
        state = self.store.load()
        self.assertEqual(state['version'], 2)
        self.assertEqual(set(state['assignments']), {'w1', 'w2', 'w3'})
        self.assertEqual(self.rows()['w1']['label'], '项目')
        self.assertEqual(self.rows()['w2']['label'], '开发')
        self.assertEqual(set(self.palette()), {'项目', '开发', '测试'})
        self.assertEqual(json.loads(self.store.path.with_suffix('.v1-backup.json').read_text()), original)
        self.assertFalse(state['labels'])

    def test_reset_legacy_state_does_not_recreate_old_groups(self):
        state = self.legacy()
        state['parents'].clear()
        self.store.save(state)
        self.service.sync()
        self.assertFalse(self.store.load()['assignments'])
        self.assertFalse(self.palette())

    def test_snapshot_does_not_migrate_disk_or_apply_colors(self):
        original = self.legacy()
        self.service.snapshot()
        self.assertEqual(self.store.load(), original)
        self.assertEqual(self.client.calls, [('workspace.list', {})])
        self.assertFalse(self.palette())

    def test_legacy_restore_failure_can_retry_after_external_rename(self):
        self.legacy()
        self.client.fail_method = 'workspace.rename'
        with self.assertRaises(GroupError):
            self.service.sync()
        self.rows()['w1']['label'] = '├─ 新项目'
        self.service.sync()
        self.assertEqual(self.rows()['w1']['label'], '新项目')

    def test_invalid_tag_or_id_does_not_mutate(self):
        for ids, name, color in [(['missing'], '项目', None), ([], '项目', None),
                                 (['w1'], '', None), (['w1'], 'bad\nname', None),
                                 (['w1'], 'A\u0085B', None),
                                 (['w1'], 'x' * 33, None), (['w1'], '项目', 'invalid')]:
            with self.assertRaises(GroupError):
                self.service.set_tag(ids, name, color)
        self.assertFalse(self.store.path.exists())

    def test_duplicate_title_different_color_rejected_before_mutation(self):
        self.rows()['w2']['label'] = '项目'
        with self.assertRaises(GroupError):
            self.service.set_tag(['w1'], 'A')
        self.assertFalse(self.store.path.exists())
        self.service.set_tag(['w1', 'w2'], 'A')
        before = self.store.path.read_text()
        with self.assertRaises(GroupError):
            self.service.set_tag(['w2'], 'B', 'purple')
        self.assertEqual(self.store.path.read_text(), before)

    def test_rule_limit_rejected_before_state_write(self):
        self.client.workspaces = [workspace('w' + str(i), '标题' + str(i)) for i in range(17)]
        with self.assertRaises(GroupError):
            self.service.set_tag(self.order(), '项目')
        self.assertFalse(self.store.path.exists())
        self.service.set_tag(self.order()[:16], '项目')
        self.assertEqual(len(self.palette()), 16)

    def test_duplicate_tag_rename_rejected(self):
        a = self.service.set_tag(['w1'], 'A')
        self.service.set_tag(['w2'], 'B')
        with self.assertRaises(GroupError):
            self.service.update_tag(a, name='B')

    def test_closed_space_prunes_color_keeps_reusable_tag(self):
        self.service.set_tag(['w2'], '项目')
        self.client.workspaces = [w for w in self.client.workspaces if w['workspace_id'] != 'w2']
        self.service.sync()
        self.assertFalse(self.store.load()['assignments'])
        self.assertFalse(self.palette())
        self.assertEqual(len(self.store.load()['tags']), 1)

    def test_sync_retries_topology_change_without_replaying_operation(self):
        self.service.set_tag(['w2'], '项目')
        original = self.client.call
        lists = 0
        def concurrent(method, **params):
            nonlocal lists
            if method == 'workspace.list':
                lists += 1
                if lists == 2:
                    self.client.workspaces.append(workspace('w5', '新 Space'))
            return original(method, **params)
        self.client.call = concurrent
        self.service.sync()
        self.assertIn('w5', self.order())
        self.assertEqual(self.order()[0], 'w2')

    def test_corrupt_state_not_overwritten(self):
        self.store.path.write_text('{broken')
        with self.assertRaises(GroupError):
            self.service.sync()
        self.assertEqual(self.store.path.read_text(), '{broken')

    def test_socket_namespaces_do_not_share_ids(self):
        self.service.set_tag(['w2'], '项目')
        other = Store(self.temp.name, '/tmp/another-session.sock')
        self.assertNotEqual(other.path, self.store.path)
        self.assertEqual(other.load()['assignments'], {})


if __name__ == '__main__':
    unittest.main()
