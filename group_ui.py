"""Flat-tag curses frontend; all Herdr operations belong to the supplied service."""

import curses
import locale
import unicodedata
from collections import Counter

from herdr_group import COLORS


_STATUS = {
    "running": "运行中",
    "working": "工作中",
    "blocked": "等待确认",
    "busy": "忙碌",
    "idle": "空闲",
    "waiting": "等待输入",
    "waiting_for_input": "等待输入",
    "done": "已完成",
    "completed": "已完成",
    "error": "错误",
    "failed": "失败",
    "stopped": "已停止",
    "none": "无代理",
    "unknown": "未知",
}
_COLOR_NAMES = ("red", "orange", "yellow", "green", "cyan", "blue", "purple", "gray")
_ENTER = ("\n", "\r", curses.KEY_ENTER)
_CANCEL = ("\x1b", "q")
_GUIDE = (
    "↑/↓ j/k 选择  Space 多选  t 设置标签  d 清除标签  c 标签颜色",
    "r 重命名 Space  e 重命名标签  g 刷新  Enter 聚焦  q 关闭",
    "s 标签排序 · 同标签相邻 · worktree 整组",
)


def _clean(text):
    """Never send terminal control or Unicode formatting characters to curses."""
    return "".join(
        " " if unicodedata.category(char).startswith("C")
        or char in "  " else char
        for char in str(text)
    )


def _width(char):
    if unicodedata.combining(char) or unicodedata.category(char) in ("Mn", "Me"):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def _clip(text, cells):
    result = []
    used = 0
    for char in _clean(text):
        width = _width(char)
        if used + width > cells:
            break
        if width or result:
            result.append(char)
            used += width
    return "".join(result)


def _put(screen, y, text, attr=0, x=0):
    height, width = screen.getmaxyx()
    if not (0 <= y < height and 0 <= x < width):
        return
    text = _clip(text, width - x)
    if text:
        try:
            screen.addstr(y, x, text, attr)
        except curses.error:
            # A resize can race drawing; writing the bottom-right cell also
            # raises ERR on some terminals even though the text was written.
            pass


def _show(screen):
    try:
        screen.refresh()
    except curses.error:
        pass


def _key(screen):
    try:
        return screen.get_wch()
    except curses.error:
        return None


def _name(workspace):
    return workspace.get("base_label") or workspace.get("label") or "未命名"


def _root(workspace):
    return workspace.get("group_root_id") or workspace["workspace_id"]


def _agent_summary(members):
    counts = Counter()
    for member in members:
        status = str(member.get("agent_status") or "none")
        counts[_STATUS.get(status.lower(), status)] += 1
    if len(counts) > 1:
        counts.pop(_STATUS["none"], None)
    return " / ".join(
        f"{status}×{count}" if count > 1 else status
        for status, count in counts.items()
    )


class _UI:
    def __init__(self, screen, service):
        self.screen = screen
        self.service = service
        self.rows = []
        self.tags = {}
        self.selected = 0
        self.message = ""
        self.marked = set()
        self.color_pairs = {}

    def _call(self, method, *args, **kwargs):
        # Catch service errors without depending on the executable's class identity.
        try:
            return True, getattr(self.service, method)(*args, **kwargs)
        except Exception as error:
            self.message = "错误：" + (str(error) or type(error).__name__)
            return False, None

    def _refresh(self, preferred=None):
        if preferred is None and self.rows:
            preferred = self.rows[self.selected]["workspace_id"]
        ok, snapshot = self._call("snapshot")
        if not ok:
            return
        workspaces = snapshot["workspaces"]
        roots = {row["workspace_id"]: _root(row) for row in workspaces}
        members = {}
        for row in workspaces:
            members.setdefault(_root(row), {})[row["workspace_id"]] = row
        self.tags = snapshot.get("tags", {})
        sections = {}
        seen = set()
        for original in workspaces:
            wid = original["workspace_id"]
            if wid != _root(original) or wid in seen:
                continue
            seen.add(wid)
            row = dict(original)
            group = list(members[wid].values())
            row["focused"] = any(member.get("focused") for member in group)
            row["group_size"] = max(row.get("group_size") or 1, len(group))
            row["agent_summary"] = _agent_summary(group)
            row["tag_id"] = row.get("tag_id") or None
            sections.setdefault(row["tag_id"], []).append(row)
        self.rows = [row for tag, rows in sections.items() if tag is not None for row in rows]
        self.rows.extend(sections.get(None, []))
        self.marked = {roots[wid] for wid in self.marked if wid in roots and roots[wid] in seen}
        if preferred is None:
            preferred = snapshot.get("current_workspace_id")
        if preferred is None:
            preferred = next((row["workspace_id"] for row in workspaces if row.get("focused")), None)
        preferred = roots.get(preferred, preferred)
        self.selected = next(
            (index for index, row in enumerate(self.rows) if row["workspace_id"] == preferred),
            min(self.selected, max(0, len(self.rows) - 1)),
        )

    def _init_colors(self):
        self.color_pairs = {}
        try:
            if not curses.has_colors():
                return
            curses.start_color()
            background = -1
            try:
                curses.use_default_colors()
            except curses.error:
                background = curses.COLOR_BLACK
            basic = (curses.COLOR_RED, curses.COLOR_YELLOW, curses.COLOR_YELLOW,
                     curses.COLOR_GREEN, curses.COLOR_CYAN, curses.COLOR_BLUE,
                     curses.COLOR_MAGENTA, curses.COLOR_WHITE)
            palette = (196, 208, 220, 82, 51, 75, 141, 245) if curses.COLORS >= 256 else basic
            for pair, (name, foreground) in enumerate(zip(_COLOR_NAMES, palette), 1):
                if pair >= curses.COLOR_PAIRS:
                    break
                try:
                    curses.init_pair(pair, foreground, background)
                    self.color_pairs[name] = curses.color_pair(pair)
                except curses.error:
                    continue
        except curses.error:
            self.color_pairs = {}

    def _color(self, name):
        return self.color_pairs.get(name, 0)

    def _tag(self, row):
        tag = self.tags.get(row.get("tag_id"), {})
        return (tag.get("name") or row.get("tag_name") or "未命名标签",
                tag.get("color") or row.get("tag_color") or "gray")

    def _draw_list(self, rows, selected, title, guide):
        self.screen.erase()
        height, _ = self.screen.getmaxyx()
        header = 1 if height >= 6 else 0
        guides = min(len(guide), max(0, height - 3))
        status = 1 if height >= 2 else 0
        visible = max(1, height - header - guides - status)
        if header:
            _put(self.screen, 0, title, curses.A_BOLD)
        counts = Counter(row.get("tag_id") for row in rows)
        entries = []
        positions = []
        for index, row in enumerate(rows):
            tag_id = row.get("tag_id")
            if index == 0 or tag_id != rows[index - 1].get("tag_id"):
                if tag_id is None:
                    text, attr = "未标记", curses.A_DIM
                else:
                    name, color = self._tag(row)
                    text = f"标签 {name} · {COLORS.get(color, color)}"
                    attr = curses.A_BOLD | self._color(color)
                entries.append((None, f"{text} · {counts[tag_id]} 个 Space / 整组", attr))
            positions.append(len(entries))
            marker = "+" if row["workspace_id"] in self.marked else " "
            badge = f" · 整组 {row['group_size']} Spaces" if row.get("group_size", 1) > 1 else ""
            text = "{}{}{} {}  [{}]{}".format(
                ">" if index == selected else " ",
                "*" if row.get("focused") else " ", marker,
                _name(row), row.get("agent_summary") or _agent_summary([row]), badge,
            )
            color = self._color(self._tag(row)[1]) if tag_id else 0
            entries.append((index, text, color | (curses.A_REVERSE if index == selected else 0)))
        if rows:
            selected_line = positions[selected]
            top = max(0, min(selected_line - visible // 2, len(entries) - visible))
            if visible == 1:
                shown = [entries[selected_line]]
            else:
                # Repeat the section header when scrolling into a long section.
                if entries[top][0] is not None:
                    top = max(top, selected_line - visible + 2)
                shown = entries[top:top + visible]
                if entries[top][0] is not None:
                    section = next(entry for entry in reversed(entries[:top]) if entry[0] is None)
                    shown = [section] + shown[:visible - 1]
            for offset, (_, text, attr) in enumerate(shown):
                _put(self.screen, header + offset, text, attr)
        else:
            _put(self.screen, header, "没有 Space；按 g 刷新，q 关闭")
        if status:
            detail = self.message or "{}/{} · 已选 {} · * 当前 Space".format(
                selected + 1 if rows else 0, len(rows), len(self.marked)
            )
            _put(self.screen, height - guides - 1, detail)
        elif self.message:
            _put(self.screen, 0, self.message)
        if guides:
            for offset, line in enumerate(guide[-guides:]):
                _put(self.screen, height - guides + offset, line, curses.A_DIM)
        _show(self.screen)

    def _prompt(self, title, initial="", allow_empty=False):
        value = initial
        warning = ""
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        try:
            while True:
                self.screen.erase()
                height, width = self.screen.getmaxyx()
                input_y = 1 if height >= 2 else 0
                if height >= 2:
                    _put(self.screen, 0, title, curses.A_BOLD)
                # Keep the end and cursor visible without splitting wide cells.
                budget = max(0, width - 1)
                start = len(value)
                used = 0
                while start:
                    size = _width(value[start - 1])
                    if used + size > budget:
                        break
                    used += size
                    start -= 1
                shown = _clip(value[start:], budget)
                _put(self.screen, input_y, shown)
                if height >= 3:
                    _put(self.screen, height - 1,
                         warning or "Enter 确认 · Esc 取消 · 退格删除")
                try:
                    self.screen.move(input_y, min(sum(map(_width, shown)), width - 1))
                except curses.error:
                    pass
                _show(self.screen)
                key = _key(self.screen)
                if key == "\x1b":
                    return None
                if key in _ENTER:
                    if value.strip() or allow_empty:
                        return value.strip()
                    warning = "名称不能为空；Esc 取消"
                elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    value = value[:-1]
                    warning = ""
                elif isinstance(key, str) and (key.isprintable() or key in ("‌", "‍")):
                    value += key
                    warning = ""
                # KEY_RESIZE and other function keys simply redraw the prompt.
        finally:
            try:
                curses.curs_set(0)
            except curses.error:
                pass

    def _pick(self, title, choices, selected=0, numbered=False):
        """Return a choice index, keeping picker headings outside navigation."""
        while True:
            self.screen.erase()
            height, _ = self.screen.getmaxyx()
            header = 1 if height >= 3 else 0
            footer = 1 if height >= 2 else 0
            visible = max(1, height - header - footer)
            if header:
                _put(self.screen, 0, title, curses.A_BOLD)
            top = max(0, min(selected - visible // 2, len(choices) - visible))
            for offset, (label, color) in enumerate(choices[top:top + visible]):
                index = top + offset
                text = f"{index + 1}. {label}" if numbered else label
                attr = self._color(color) | (curses.A_REVERSE if index == selected else 0)
                _put(self.screen, header + offset, ("> " if index == selected else "  ") + text, attr)
            if footer:
                hint = "1–8 选择 · " if numbered else ""
                _put(self.screen, height - 1, hint + "↑/↓ j/k 选择 · Enter 确认 · q/Esc 取消", curses.A_DIM)
            _show(self.screen)
            key = _key(self.screen)
            if key in _CANCEL:
                return None
            if key in _ENTER:
                return selected
            if numbered and isinstance(key, str) and key in "12345678":
                return int(key) - 1
            if key in (curses.KEY_UP, "k"):
                selected = max(0, selected - 1)
            elif key in (curses.KEY_DOWN, "j"):
                selected = min(len(choices) - 1, selected + 1)

    def _pick_color(self, initial="blue"):
        choices = [(f"{COLORS[name]} ({name})", name) for name in _COLOR_NAMES]
        selected = _COLOR_NAMES.index(initial) if initial in _COLOR_NAMES else 0
        choice = self._pick("选择标签颜色", choices, selected, numbered=True)
        return _COLOR_NAMES[choice] if choice is not None else None

    def _targets(self):
        if self.marked:
            return [row["workspace_id"] for row in self.rows if row["workspace_id"] in self.marked]
        return [self.rows[self.selected]["workspace_id"]] if self.rows else []

    def _assign_tag(self, row):
        tags = list(self.tags.items())
        choices = [(f"{tag['name']} · {COLORS.get(tag['color'], tag['color'])}", tag["color"])
                   for _, tag in tags]
        choices.append(("+ 新建 / 输入标签名称…", None))
        selected = next((index for index, (tag_id, _) in enumerate(tags) if tag_id == row.get("tag_id")), 0)
        choice = self._pick("给所选 Space / 整组设置标签", choices, selected)
        if choice is None:
            return
        if choice == len(tags):
            name = self._prompt("标签名称（同名将使用已有标签）")
            if name is None:
                return
            initial = next((tag["color"] for _, tag in tags if tag["name"] == name), "blue")
            color = self._pick_color(initial)
            if color is None:
                return
            self._change("set_tag", "已设置标签：" + name, self._targets(), name, color=color)
        else:
            name = tags[choice][1]["name"]
            self._change("set_tag", "已设置标签：" + name, self._targets(), name)

    def _sort_tags(self):
        self._refresh()
        tags = list(self.tags.items())
        if not tags:
            self.message = "还没有标签；按 t 创建标签后再排序"
            return
        current = self.rows[self.selected].get("tag_id") if self.rows else None
        selected = next((i for i, (tid, _) in enumerate(tags) if tid == current), 0)
        notice = "调整后立即保存并同步侧栏；未标记的 Space 固定在最后"
        while True:
            self.screen.erase()
            height, _ = self.screen.getmaxyx()
            header = 1 if height >= 5 else 0
            guides = min(2, max(0, height - 3))
            status = 1 if height >= 2 else 0
            visible = max(1, height - header - guides - status)
            if header:
                _put(self.screen, 0, "标签排序", curses.A_BOLD)
            top = max(0, min(selected - visible // 2, len(tags) - visible))
            counts = Counter(row.get("tag_id") for row in self.rows)
            for offset, (tid, tag) in enumerate(tags[top:top + visible]):
                index = top + offset
                text = (f"{'>' if index == selected else ' '} {index + 1}. {tag['name']}"
                        f" · {COLORS.get(tag['color'], tag['color'])}"
                        f" · {counts[tid]} 个 Space / 整组")
                attr = self._color(tag["color"]) | (curses.A_REVERSE if index == selected else 0)
                _put(self.screen, header + offset, text, attr)
            if status:
                _put(self.screen, height - guides - 1, notice)
            hints = ("↑/↓ j/k 选择 · K 上移 · J 下移（大写 / Shift）",
                     "Enter / q / Esc 返回 · 调整立即保存")
            if guides:
                for offset, hint in enumerate(hints[-guides:]):
                    _put(self.screen, height - guides + offset, hint, curses.A_DIM)
            _show(self.screen)
            key = _key(self.screen)
            if key in _CANCEL or key in _ENTER:
                return
            if key in (curses.KEY_UP, "k"):
                selected = max(0, selected - 1)
            elif key in (curses.KEY_DOWN, "j"):
                selected = min(len(tags) - 1, selected + 1)
            elif key in ("K", "J"):
                tid, tag = tags[selected]
                ok, moved = self._call("move_tag", tid, "up" if key == "K" else "down")
                if ok:
                    notice = (f"已{'上移' if key == 'K' else '下移'}标签：{tag['name']}" if moved
                              else "已在最前面" if key == "K" else "已在最后面")
                    self.message = notice
                else:
                    notice = self.message
                self._refresh()
                tags = list(self.tags.items())
                if not tags:
                    return
                selected = next((i for i, (item, _) in enumerate(tags) if item == tid),
                                min(selected, len(tags) - 1))

    def _change(self, method, message, *args, **kwargs):
        ok, _ = self._call(method, *args, **kwargs)
        if ok:
            self.message = message
            self._refresh()

    def loop(self):
        self.screen.keypad(True)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if hasattr(curses, "set_escdelay"):
            curses.set_escdelay(25)
        self._init_colors()
        self._refresh()
        if getattr(self.service, "start_tagging", False):
            self.message = "用 Space 键多选，按 t 选择或创建标签（worktree 整组）"
        while True:
            self._draw_list(self.rows, self.selected, "Space 标签", _GUIDE)
            key = _key(self.screen)
            if key in _CANCEL:
                return
            if key in (curses.KEY_UP, "k"):
                self.selected = max(0, self.selected - 1)
            elif key in (curses.KEY_DOWN, "j"):
                self.selected = min(max(0, len(self.rows) - 1), self.selected + 1)
            elif key == "g":
                self._change("sync", "已同步并刷新")
            elif key == "s":
                self._sort_tags()
            elif self.rows:
                row = self.rows[self.selected]
                workspace_id = row["workspace_id"]
                if key == " ":
                    if workspace_id in self.marked:
                        self.marked.remove(workspace_id)
                    else:
                        self.marked.add(workspace_id)
                    self.message = f"已选择 {len(self.marked)} 个 Space / 整组"
                elif key in _ENTER:
                    if self._call("focus", workspace_id)[0]:
                        return
                elif key == "t":
                    self._assign_tag(row)
                elif key == "d":
                    self._change("clear_tag", "已清除所选标签", self._targets())
                elif key == "r":
                    name = self._prompt("重命名 Space：基础名称", _name(row))
                    if name is not None:
                        self._change("rename", "已重命名 Space", workspace_id, name)
                elif key in ("c", "e"):
                    tag_id = row.get("tag_id")
                    if tag_id is None:
                        self.message = "当前 Space 没有标签；按 t 设置标签"
                        continue
                    name, color = self._tag(row)
                    if key == "c":
                        color = self._pick_color(color)
                        if color is not None:
                            self._change("update_tag", "已更新标签颜色", tag_id, color=color)
                    else:
                        name = self._prompt("重命名标签（应用于所有同标签 Space）", name)
                        if name is not None:
                            self._change("update_tag", "已重命名标签", tag_id, name=name)


def run(service):
    """Open the Space list until focus, cancellation, or keyboard interrupt."""
    try:
        locale.setlocale(locale.LC_CTYPE, "")
    except locale.Error:
        pass
    try:
        curses.wrapper(lambda screen: _UI(screen, service).loop())
    except KeyboardInterrupt:
        pass
