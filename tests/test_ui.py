import curses
import unittest
from unittest.mock import patch

from group_ui import _UI


class Screen:
    def __init__(self):
        self.lines = {}

    def getmaxyx(self):
        return 16, 80

    def erase(self):
        self.lines.clear()

    def addstr(self, y, x, text, attr=0):
        self.lines[y] = text

    def move(self, *args):
        pass

    def refresh(self):
        pass

    def get_wch(self):
        return '\n'


class Service:
    def snapshot(self):
        return {'current_workspace_id': 'linked', 'tags': {'perf': {'name': '性能', 'color': 'blue'}},
                'workspaces': [
                    dict(workspace_id='main', group_root_id='main', group_size=3,
                         label='主目录', tag_id='perf'),
                    dict(workspace_id='duplicate', group_root_id='main', group_size=3,
                         label='副目录', tag_id='perf'),
                    dict(workspace_id='linked', group_root_id='main', group_size=3,
                         label='分支', tag_id='perf', focused=True),
                    dict(workspace_id='ordinary', group_root_id='ordinary', label='普通'),
                ]}


class UITest(unittest.TestCase):
    def test_linked_caller_and_selection_canonicalize_to_one_native_row(self):
        ui = _UI(Screen(), Service())
        ui.marked = {'linked', 'duplicate'}
        ui._refresh()
        self.assertEqual([row['workspace_id'] for row in ui.rows], ['main', 'ordinary'])
        self.assertEqual(ui.marked, {'main'})
        self.assertEqual(ui.selected, 0)
        self.assertTrue(ui.rows[0]['focused'])
        self.assertEqual(ui._targets(), ['main'])

    def test_tag_sections_show_separate_headers_and_no_manual_tree(self):
        screen = Screen()
        ui = _UI(screen, Service())
        ui._refresh()
        ui._draw_list(ui.rows, 0, 'Space 标签', ('操作', '同标签相邻 · worktree 整组'))
        rendered = '\n'.join(screen.lines.values())
        self.assertIn('标签 性能 · 蓝色', rendered)
        self.assertIn('未标记', rendered)
        self.assertIn('整组 3 Spaces', rendered)
        self.assertNotIn('├─', rendered)
        self.assertNotIn('└─', rendered)

    def test_accepting_unchanged_emoji_preserves_joiner(self):
        name = '👩‍💻 Dev'
        with patch('group_ui.curses.curs_set'):
            self.assertEqual(_UI(Screen(), None)._prompt('名称', name), name)


if __name__ == '__main__':
    unittest.main()
