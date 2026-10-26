"""Exercise Space/Agents colors and tag sorting in a disposable Herdr/tmux session."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import tomllib
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ID = 'local.herdr-colored-tag'
BLUE, PURPLE = (122, 162, 247), (187, 154, 247)


def run(args, env, **kwargs):
    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=15, **kwargs)
    if result.returncode:
        raise RuntimeError(f'{args}: {result.stderr or result.stdout}')
    return result.stdout


def width(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in text)


def columns(text, start=0, end=32):
    result, used = '', 0
    for char in text:
        size = width(char)
        if used >= start and used + size <= end:
            result += char
        used += size
    return result


def foreground_at(line, text, occurrence=0):
    foreground, plain, colors = None, '', []
    for part in re.split(r'(\x1b\[[0-9;]*m)', line):
        if not part.startswith('\x1b['):
            plain += part
            colors.extend([foreground] * len(part))
            continue
        codes, index = [int(value or 0) for value in part[2:-1].split(';')], 0
        while index < len(codes):
            code = codes[index]
            if code in (0, 39):
                foreground = None
            elif code == 38 and codes[index + 1:index + 2] == [2] and len(codes) >= index + 5:
                foreground = tuple(codes[index + 2:index + 5])
                index += 4
            elif code == 38 and codes[index + 1:index + 2] == [5] and len(codes) >= index + 3:
                foreground = ('indexed', codes[index + 2])
                index += 2
            elif 30 <= code <= 37 or 90 <= code <= 97:
                foreground = ('basic', code)
            index += 1
    start = -1
    for _ in range(occurrence + 1):
        start = plain.index(text, start + 1)
    values = colors[start:start + len(text)]
    assert len(set(values)) == 1, (text, values, repr(line))
    return values[0]


def main():
    assert os.environ.get('HERDR_ENV') == '1', 'run inside a Herdr-managed pane'
    assert (ROOT / 'title_colors.py').is_file(), 'wait for staged title_colors.py before running'
    with tempfile.TemporaryDirectory(prefix='hg-title-', dir='/tmp') as temp:
        root, session = Path(temp), 'title-test'
        env = {key: value for key, value in os.environ.items() if not key.startswith('HERDR_')}
        env.update(XDG_CONFIG_HOME=str(root / 'config'), XDG_STATE_HOME=str(root / 'state'),
                   HERDR_ENV='1', HERDR_SOCKET_PATH=str(root / f'config/herdr/sessions/{session}/herdr.sock'),
                   COLORTERM='truecolor', PYTHONDONTWRITEBYTECODE='1')
        config = root / 'config/herdr/config.toml'
        config.parent.mkdir(parents=True)
        config.write_text('onboarding = false\n[terminal]\ndefault_shell = "/bin/sh"\n'
                          '[ui]\nsidebar_width = 32\nsidebar_min_width = 32\nsidebar_max_width = 32\n'
                          '[ui.sidebar.agents]\n# Keep this user-owned empty table.')
        tmux_name, server = 'herdr-title-' + str(os.getpid()), None
        tmux_base = ['tmux', '-L', tmux_name, '-f', '/dev/null']

        def cli(*args):
            return json.loads(run(['herdr', *args], env))

        def plugin(*args):
            return run([sys.executable, str(ROOT / 'herdr_group.py'), *args], env)

        def tmux(*args):
            return run([*tmux_base, *args], env)

        def screen(ansi=False):
            return tmux('capture-pane', '-t', tmux_name, '-p', *(['-e'] if ansi else []))

        def sidebar():
            return [columns(line) for line in screen().splitlines()]

        def popup():
            return '\n'.join(columns(line, 33, 120) for line in screen().splitlines())

        def records():
            return cli('workspace', 'list')['result']['workspaces']

        def labels():
            return {row['workspace_id']: row['label'] for row in records()}

        def snapshot():
            return {row['workspace_id']: row for row in json.loads(plugin('list'))['workspaces']}

        def until(predicate, description='event synchronization'):
            deadline = time.monotonic() + 15
            while True:
                value = predicate()
                if value:
                    return value
                if server is not None and server.poll() is not None:
                    raise RuntimeError(server.stderr.read())
                if time.monotonic() > deadline:
                    raise AssertionError(description + ' timed out')
                time.sleep(0.05)

        def contains(text):
            until(lambda: text in popup(), f'popup contains {text!r}')

        def settle():
            def quiet():
                logs = cli('plugin', 'log', 'list', '--plugin', PLUGIN_ID, '--limit', '500')['result']['logs']
                return logs if logs and all(row['status'] != 'running' for row in logs) else None
            until(quiet, 'plugin commands settle')
            time.sleep(0.25)
            return until(quiet, 'queued plugin commands settle')

        def keys(*values):
            tmux('send-keys', '-t', tmux_name, *values)

        def literal(value):
            tmux('send-keys', '-l', '-t', tmux_name, value)

        def open_popup(wid, action='manage'):
            cli('workspace', 'focus', wid)
            cli('plugin', 'action', 'invoke', f'{PLUGIN_ID}.{action}')
            contains('Space 标签')

        def close_popup(focus=None):
            keys('q')
            until(lambda: 'Space 标签' not in popup(), 'popup closes')
            if focus:
                cli('workspace', 'focus', focus)
            settle()

        def start_server():
            return subprocess.Popen(['herdr', '--session', session, 'server'], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

        def start_client():
            values = [f'{key}={env[key]}' for key in ('XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'HERDR_SOCKET_PATH', 'COLORTERM')]
            tmux('new-session', '-d', '-s', tmux_name, '-x', '120', '-y', '40',
                 'env', '-u', 'HERDR_ENV', '-u', 'HERDR_PANE_ID', '-u', 'HERDR_TAB_ID',
                 '-u', 'HERDR_WORKSPACE_ID', *values, 'TERM=tmux-256color', 'herdr', '--session', session)

        def agent_evidence(workspace_title, expected):
            def capture():
                ansi = screen(True).splitlines()
                lines = [columns(re.sub(r'\x1b\[[0-9;]*m', '', line)) for line in ansi]
                header = next((i for i, line in enumerate(lines)
                               if line.strip(' │').lower().split()[:1] == ['agents']), None)
                if header is None:
                    return None
                row = next((i for i in range(header + 1, len(lines))
                            if workspace_title in lines[i]), None)
                if row is None:
                    return None
                found = foreground_at(ansi[row], workspace_title)
                if found != expected:
                    return None
                assert 'claude' in lines[row + 1].lower(), lines[row:row + 2]
                return ansi[row]
            title_only = until(capture, f'Agents workspace title {workspace_title!r} has color {expected}')
            print(f'AGENT WORKSPACE {workspace_title!r}: fg={expected}, ANSI={title_only!r}', flush=True)

        def report_agent(wid):
            panes = cli('pane', 'list', '--workspace', wid)['result']['panes']
            assert len(panes) == 1, panes
            run(['herdr', 'pane', 'report-agent', panes[0]['pane_id'], '--source', 'colored-tag-test',
                 '--agent', 'claude', '--state', 'working'], env)

        def evidence(title, expected=None, x=None):
            until(lambda: any(title in row for row in sidebar()), f'sidebar title {title}')
            plain, ansi = sidebar(), screen(True).splitlines()
            row = next(i for i, line in enumerate(plain) if title in line)
            captured = ansi[row].split('│', 1)[0]
            fg, xpos = foreground_at(captured, title), width(plain[row].split(title, 1)[0])
            print('SIDEBAR:', ' | '.join(line.rstrip() for line in plain if line.strip(' │')), flush=True)
            print(f'TITLE {title!r}: x={xpos}, fg={fg}, ANSI={captured!r}', flush=True)
            if expected is not None:
                assert fg == expected, (title, fg, expected)
            if x is not None:
                assert xpos == x, (title, xpos, x)
            assert not any('Perf' in line or '【' in line or '── Native ──' in line for line in plain), plain
            assert all(not any(k.startswith('hg_') and v for k, v in (r.get('tokens') or {}).items()) for r in records())
            return fg, xpos

        def create(label, cwd=temp):
            wid = cli('workspace', 'create', '--cwd', cwd, '--label', label, '--no-focus')['result']['workspace']['workspace_id']
            settle()
            return wid

        try:
            server = start_server()
            until(lambda: Path(env['HERDR_SOCKET_PATH']).exists(), 'isolated server startup')
            linked = cli('plugin', 'link', str(ROOT))
            assert not linked['result']['plugin'].get('warnings'), linked
            parent, spacer, child = create('接口性能优化'), create('未标记空间'), create('数据库性能分析')
            env['HERDR_WORKSPACE_ID'] = parent
            original_config_bytes = config.read_bytes()
            before, config_before = labels(), tomllib.loads(config.read_text())
            start_client()
            cli('workspace', 'focus', spacer)
            defaults = {name: evidence(name) for name in before.values()}
            open_popup(parent, 'tag')
            contains('用 Space 键多选')
            literal(' ')
            keys('j', 'j')
            literal(' ')
            contains('已选择 2 个 Space')
            keys('t')
            contains('新建 / 输入标签名称')
            keys('Enter')
            contains('标签名称')
            literal('Perf')
            keys('Enter')
            contains('选择标签颜色')
            keys('6')
            contains('标签 Perf · 蓝色')
            print('POPUP BLUE:', popup(), flush=True)
            close_popup(spacer)
            until(lambda: snapshot()[parent]['tag_color'] == snapshot()[child]['tag_color'] == 'blue', 'blue tag')
            assert labels() == before and snapshot()[spacer]['tag_id'] is None
            assert abs(list(labels()).index(parent) - list(labels()).index(child)) == 1
            for wid in (parent, child):
                evidence(before[wid], BLUE, defaults[before[wid]][1])
            evidence(before[spacer], *defaults[before[spacer]])
            open_popup(parent)
            keys('c')
            contains('选择标签颜色')
            keys('7')
            contains('标签 Perf · 紫色')
            close_popup(spacer)
            for wid in (parent, child):
                evidence(before[wid], PURPLE, defaults[before[wid]][1])
            open_popup(parent)
            literal(' ')
            keys('j')
            literal(' ')
            contains('已选择 2 个 Space')
            keys('d')
            contains('已清除所选标签')
            close_popup(spacer)
            assert all(snapshot()[wid]['tag_id'] is None for wid in (parent, child)) and labels() == before
            for name in before.values():
                evidence(name, *defaults[name])
            plugin('tag', 'Perf', parent, child, '--color', 'blue')
            cli('workspace', 'rename', parent, '接口性能改进')
            until(lambda: '接口性能改进' in config.read_text() and '接口性能优化' not in config.read_text(), 'rename updates exact-label rule')
            evidence('接口性能改进', BLUE, defaults[before[parent]][1])
            print('POPUP t/c/d, unchanged labels/count, title positions, default color, adjacency and rename: passed', flush=True)

            repo = root / 'repo'
            run(['git', 'init', str(repo)], env)
            tree = run(['git', '-C', str(repo), 'hash-object', '-t', 'tree', '-w', '--stdin'], env, input='').strip()
            commit = run(['git', '-C', str(repo), 'hash-object', '-t', 'commit', '-w', '--stdin'], env,
                         input=f'tree {tree}\nauthor Fixture <fixture@example.invalid> 1 +0000\ncommitter Fixture <fixture@example.invalid> 1 +0000\n\nfixture\n').strip()
            run(['git', '-C', str(repo), 'update-ref', 'HEAD', commit], env)
            main_space = create('NativeRepo', str(repo))

            def add_link(name, branch):
                path = root / branch
                run(['git', '-C', str(repo), 'worktree', 'add', '-b', branch, str(path)], env)
                wid = cli('worktree', 'open', '--workspace', main_space, '--path', str(path), '--label', name,
                          '--no-focus', '--trust-repository')['result']['workspace']['workspace_id']
                until(lambda: snapshot()[wid]['group_root_id'] == main_space, 'native member canonicalized')
                settle()
                return wid, path

            linked_space, linked_path = add_link('NativeLinked', 'fixture-linked')
            cli('workspace', 'focus', main_space)
            native_defaults = {name: evidence(name) for name in ('NativeRepo', 'NativeLinked')}
            native_before = labels()
            plugin('tag', 'Native', linked_space, spacer, '--color', 'purple')
            evidence('NativeRepo', PURPLE, native_defaults['NativeRepo'][1])
            evidence('NativeLinked', *native_defaults['NativeLinked'])
            assert labels() == native_before
            assert snapshot()[main_space]['tag_id'] == snapshot()[spacer]['tag_id']
            ids = list(labels())
            assert ids[ids.index(main_space) + 1] == linked_space
            assert abs(ids.index(main_space) - ids.index(spacer)) in (1, 2)
            open_popup(linked_space)
            contains('整组 2 Spaces')
            rendered = popup()
            print('NATIVE POPUP:', rendered, flush=True)
            assert any('>' in line and 'NativeRepo' in line for line in rendered.splitlines()), rendered
            assert 'NativeLinked' not in rendered, rendered
            keys('Enter')
            until(lambda: 'Space 标签' not in popup() and next(r for r in records() if r['workspace_id'] == main_space)['focused'], 'focus native representative')
            row = next(i for i, line in enumerate(sidebar()) if 'NativeRepo' in line)
            line = sidebar()[row]
            assert '▾' in line and '▾' not in next(line for line in sidebar() if 'NativeLinked' in line), sidebar()
            x = width(line.split('▾', 1)[0]) + 1
            click = f'\x1b[<0;{x};{row + 1}M\x1b[<0;{x};{row + 1}m'
            literal(click)
            until(lambda: not any('NativeLinked' in line for line in sidebar()), 'mouse collapses native group')
            print('COLLAPSED:', ' | '.join(line.rstrip() for line in sidebar() if line.strip(' │')), flush=True)
            literal(click)
            until(lambda: any('NativeLinked' in line for line in sidebar()), 'mouse expands native group')
            evidence('NativeLinked', *native_defaults['NativeLinked'])
            second, second_path = add_link('NativeSecond', 'fixture-second')
            evidence('NativeSecond', native_defaults['NativeLinked'][0], native_defaults['NativeLinked'][1])
            cli('workspace', 'close', second)
            until(lambda: second not in labels(), 'closed member removed')
            assert second_path.is_dir() and labels() == native_before
            assert run(['git', '-C', str(repo), 'status', '--porcelain'], env) == run(['git', '-C', str(linked_path), 'status', '--porcelain'], env) == ''
            print('NATIVE: linked selection, ordinary adjacency, default children, mouse collapse/expand and add/close passed', flush=True)

            order_before = list(labels())
            perf_ids = [wid for wid in order_before if wid in (parent, child)]
            native_ids = [wid for wid in order_before if wid in (spacer, main_space, linked_space)]
            tracked = set(perf_ids + native_ids)
            trees_before = {r['workspace_id']: r.get('worktree') for r in records()}

            def tag_order(expected):
                api_ids = [wid for wid in labels() if wid in tracked]
                lines = sidebar()
                agent_start = next((i for i, line in enumerate(lines)
                                    if line.strip(' │').lower().split()[:1] == ['agents']), len(lines))
                screen_ids = [wid for line in lines[:agent_start] for wid in tracked if native_before[wid] in line]
                return api_ids == screen_ids == expected

            assert tag_order(perf_ids + native_ids)
            open_popup(parent)
            keys('s')
            contains('标签排序')
            contains('K 上移')
            for key, expected in [('J', native_ids + perf_ids),
                                  ('K', perf_ids + native_ids),
                                  ('J', native_ids + perf_ids)]:
                keys(key)
                until(lambda: tag_order(expected), 'tag order updates sidebar and workspace list')
                assert labels() == native_before
                assert {r['workspace_id']: r.get('worktree') for r in records()} == trees_before
                state = snapshot()
                assert state[main_space]['tag_id'] == state[spacer]['tag_id']
                assert state[linked_space]['group_root_id'] == main_space
                assert state[linked_space]['native_child']
                ids = list(labels())
                assert ids[ids.index(main_space) + 1] == linked_space
                until(lambda: '>' in next(line for line in popup().splitlines() if 'Perf' in line),
                      'selection follows moved tag')
            keys('q')
            contains('Space 标签')
            close_popup(spacer)
            print('TAG ORDER: s panel, J/K moves, sidebar adjacency, unchanged titles/colors/native groups passed', flush=True)

            report_agent(parent)
            report_agent(linked_space)
            agent_evidence('接口性能改进', BLUE)
            agent_evidence('NativeLinked', PURPLE)
            plugin('tag', 'Perf', parent, '--color', 'purple')
            agent_evidence('接口性能改进', PURPLE)
            plugin('tag', 'Perf', parent, '--color', 'blue')
            agent_evidence('接口性能改进', BLUE)
            cli('workspace', 'rename', linked_space, 'RenamedLinked')
            agent_evidence('RenamedLinked', PURPLE)
            cli('workspace', 'rename', linked_space, 'NativeLinked')
            agent_evidence('NativeLinked', PURPLE)
            print('AGENTS: same native agent name, distinct tag colors, inherited linked color, recolor and rename passed', flush=True)

            before_restart_logs = settle()
            assert all(row['status'] == 'succeeded' for row in before_restart_logs), before_restart_logs
            print(f'PLUGIN LOGS BEFORE RESTART: all {len(before_restart_logs)} commands succeeded', flush=True)
            saved_config = tomllib.loads(config.read_text())
            tmux('kill-session', '-t', tmux_name)
            run(['herdr', '--session', session, 'server', 'stop'], env)
            server.wait(timeout=10)
            server = start_server()
            until(lambda: Path(env['HERDR_SOCKET_PATH']).exists(), 'isolated restart')
            settle()
            assert labels() == native_before and tomllib.loads(config.read_text()) == saved_config
            restored = snapshot()
            assert restored[parent]['tag_color'] == 'blue' and restored[main_space]['tag_color'] == 'purple'
            assert restored[linked_space]['group_root_id'] == main_space and restored[linked_space]['native_child']
            assert restored[linked_space]['worktree']['is_linked_worktree']
            start_client()
            cli('workspace', 'focus', main_space)
            evidence('接口性能改进', BLUE, defaults[before[parent]][1])
            evidence('NativeRepo', PURPLE, native_defaults['NativeRepo'][1])
            evidence('NativeLinked', *native_defaults['NativeLinked'])
            assert tag_order(native_ids + perf_ids)
            open_popup(parent)
            keys('s')
            contains('标签排序')
            contains('1. Native')
            contains('2. Perf')
            keys('q')
            contains('标签 Perf · 蓝色')
            close_popup(main_space)
            print('TAG ORDER RESTART: saved order restored in sorting panel and sidebar', flush=True)
            plugin('reset')
            settle()
            assert all(row['tag_id'] is None for row in snapshot().values()) and labels() == native_before
            assert tomllib.loads(config.read_text()) == config_before, config.read_text()
            assert config.read_bytes() == original_config_bytes, 'reset must restore original bytes, including missing final LF'
            evidence('NativeRepo', *native_defaults['NativeRepo'])
            evidence('NativeLinked', *native_defaults['NativeLinked'])
            logs = settle()
            assert all(row['status'] == 'succeeded' for row in logs), logs
            assert any(row.get('event') == 'startup' for row in logs), logs
            print(f'RESTART/RESET: tags and config persist; reset restores original config; all {len(before_restart_logs) + len(logs)} commands succeeded', flush=True)
        except Exception:
            for args in ([*tmux_base, 'capture-pane', '-t', tmux_name, '-p', '-e'],
                         ['herdr', 'plugin', 'log', 'list', '--plugin', PLUGIN_ID, '--limit', '30']):
                debug = subprocess.run(args, env=env, capture_output=True, text=True, timeout=15)
                print('FAILURE EVIDENCE:', repr(debug.stdout or debug.stderr), flush=True)
            raise
        finally:
            subprocess.run([*tmux_base, 'kill-server'], env=env, capture_output=True, timeout=15)
            if server is not None and server.poll() is None:
                subprocess.run(['herdr', '--session', session, 'server', 'stop'], env=env, capture_output=True, timeout=15)
                server.wait(timeout=10)


if __name__ == '__main__':
    main()
