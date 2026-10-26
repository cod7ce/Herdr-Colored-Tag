#!/usr/bin/env python3
"""Sort Spaces by tag and color Space titles and Agents workspace labels. Python 3.9+."""

import argparse
import contextlib
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unicodedata
import uuid

PLUGIN_ID = "local.herdr-colored-tag"
COLORS = {"blue": "蓝色", "green": "绿色", "orange": "橙色", "purple": "紫色",
          "cyan": "青色", "red": "红色", "yellow": "黄色", "gray": "灰色"}
COLOR_HEX = {"blue": "#7aa2f7", "green": "#9ece6a", "orange": "#ff9e64",
             "purple": "#bb9af7", "cyan": "#7dcfff", "red": "#f7768e",
             "yellow": "#e0af68", "gray": "#a9b1d6"}


class GroupError(Exception):
    pass


class WorkspaceListChanged(GroupError):
    pass


class Client:
    def __init__(self, socket_path=None):
        if os.environ.get("HERDR_ENV") != "1":
            raise GroupError("请在 Herdr 终端中运行。")
        self.socket_path = socket_path or os.environ.get("HERDR_SOCKET_PATH")
        if not self.socket_path:
            raise GroupError("缺少 HERDR_SOCKET_PATH，无法确定当前会话。")

    def call(self, method, **params):
        request_id = "herdr-group:" + uuid.uuid4().hex
        request = {"id": request_id, "method": method, "params": params}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(10)
                connection.connect(self.socket_path)
                connection.sendall((json.dumps(request) + "\n").encode())
                with connection.makefile("rb") as stream:
                    line = stream.readline(8 * 1024 * 1024 + 1)
            if not line.endswith(b"\n") or len(line) > 8 * 1024 * 1024:
                raise GroupError("Herdr 返回了不完整或过大的响应。")
            response = json.loads(line)
            if response.get("id") != request_id:
                raise GroupError("Herdr 响应 ID 不匹配。")
            if "error" in response:
                error = response["error"]
                raise GroupError(f"{error.get('code')}: {error.get('message')}")
            return response["result"]
        except (OSError, ValueError, KeyError) as exc:
            raise GroupError(f"无法调用 Herdr {method}: {exc}") from exc


def state_directory():
    supplied = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if supplied:
        return Path(supplied)
    root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    return root / "herdr" / "plugins" / PLUGIN_ID


class Store:
    def __init__(self, directory, socket_path):
        self.directory = Path(directory)
        key = hashlib.sha256(os.fsencode(socket_path)).hexdigest()[:24]
        self.path = self.directory / f"groups-{key}.json"
        self.lock_path = self.directory / f"groups-{key}.lock"

    @contextlib.contextmanager
    def locked(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def load(self):
        if not self.path.exists():
            return {"version": 2, "tags": {}, "assignments": {}, "labels": {}, "native_blocks": {}}
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if state.get("version") not in (1, 2):
                raise ValueError("unsupported state version")
            labels = state["labels"]
            if not isinstance(labels, dict):
                raise ValueError("labels must be an object")
            for wid, record in labels.items():
                if not isinstance(wid, str):
                    raise ValueError("invalid label ID")
                for field in ("base", "applied", "prefix"):
                    if not isinstance(record[field], str):
                        raise ValueError("invalid label record")
                if record.get("pending") is not None:
                    if not isinstance(record["pending"], str) or not isinstance(record["pending_prefix"], str):
                        raise ValueError("invalid pending label")
            blocks = state.get("native_blocks", {})
            if not isinstance(blocks, dict):
                raise ValueError("native_blocks must be an object")
            for key, block in blocks.items():
                if not isinstance(key, str) or not isinstance(block.get("anchor"), str):
                    raise ValueError("invalid native group anchor")
                for field in ("members", "linked"):
                    if not isinstance(block[field], list) or any(not isinstance(wid, str) for wid in block[field]):
                        raise ValueError("invalid native group members")
                if block["anchor"] not in block["members"] or not set(block["linked"]).issubset(block["members"]):
                    raise ValueError("inconsistent native group")
            if state["version"] == 1:
                validate_tree(state["parents"])
            else:
                if not isinstance(state["tags"], dict) or not isinstance(state["assignments"], dict):
                    raise ValueError("invalid tags/assignments")
                names = set()
                for tid, tag in state["tags"].items():
                    if not isinstance(tid, str) or clean_label(tag["name"], 32) != tag["name"]:
                        raise ValueError("invalid tag")
                    if tag["color"] not in COLORS or tag["name"] in names:
                        raise ValueError("invalid or duplicate tag")
                    names.add(tag["name"])
                if any(not isinstance(wid, str) or not isinstance(tid, str) or tid not in state["tags"]
                       for wid, tid in state["assignments"].items()):
                    raise ValueError("invalid tag assignment")
            return state
        except (OSError, ValueError, KeyError, TypeError, AttributeError, GroupError) as exc:
            raise GroupError(f"分组状态无法读取，未覆盖原文件 {self.path}: {exc}") from exc

    def save(self, state):
        descriptor, temporary = tempfile.mkstemp(prefix=".groups-", dir=self.directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def backup_legacy(self, state):
        path = self.path.with_suffix(".v1-backup.json")
        try:
            with path.open("x", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            pass


def validate_tree(parents):
    if not isinstance(parents, dict) or any(not isinstance(c, str) or not isinstance(p, str)
                                            for c, p in parents.items()):
        raise GroupError("invalid legacy parents")
    for child in parents:
        seen = {child}
        cursor = child
        while cursor in parents:
            cursor = parents[cursor]
            if cursor in seen or len(seen) > 16:
                raise GroupError("invalid legacy tree")
            seen.add(cursor)


def native_blocks(workspaces, previous=None):
    """Match Herdr's repo grouping: at least one main and one linked checkout."""
    repositories = {}
    for workspace in workspaces:
        tree = workspace.get("worktree")
        if tree:
            mains, linked = repositories.setdefault(tree["repo_key"], ([], []))
            (linked if tree["is_linked_worktree"] else mains).append(workspace["workspace_id"])
    blocks = {}
    for key, (mains, linked) in repositories.items():
        if mains and linked:
            preferred = (previous or {}).get(key, {}).get("anchor")
            anchor = preferred if preferred in mains else mains[0]
            mains = [anchor] + [wid for wid in mains if wid != anchor]
            blocks[key] = {"anchor": anchor, "members": mains + linked, "linked": linked}
    return blocks


def unit_roots(blocks):
    return {wid: block["anchor"] for block in blocks.values() for wid in block["members"]}


def ordered_units(workspaces, state):
    roots = unit_roots(state["native_blocks"])
    units = list(dict.fromkeys(roots.get(w["workspace_id"], w["workspace_id"]) for w in workspaces))
    rank = {tid: index for index, tid in enumerate(state["tags"])}
    return sorted(units, key=lambda wid: rank.get(state["assignments"].get(wid), len(rank)))


def clean_label(label, limit=200):
    if not isinstance(label, str) or not label.strip():
        raise GroupError("名称不能为空。")
    if len(label) > limit or any(unicodedata.category(c) == "Cc" for c in label):
        raise GroupError(f"名称不能含控制字符，且最多 {limit} 个字符。")
    return label.strip()


def adopt_label(record, label):
    if record.get("pending") == label:
        record["applied"] = label
        record["prefix"] = record["pending_prefix"]
    elif label != record["applied"]:
        prefixes = (record.get("pending_prefix", ""), record["prefix"])
        prefix = next((p for p in prefixes if p and label.startswith(p)), "")
        record["base"] = label[len(prefix):]
        record["applied"] = label
        record["prefix"] = prefix
    else:
        return
    record.pop("pending", None)
    record.pop("pending_prefix", None)


def migrate(state, workspaces):
    if state["version"] == 2:
        return
    live = {w["workspace_id"]: w for w in workspaces}
    for wid, record in state["labels"].items():
        if wid in live:
            adopt_label(record, live[wid]["label"])
    tags, assignments = {}, {}
    for child in state["parents"]:
        if child not in live:
            continue
        root = child
        while root in state["parents"] and state["parents"][root] in live:
            root = state["parents"][root]
        if root == child:
            continue
        name = state["labels"].get(root, {}).get("base", live[root]["label"])
        name = "".join(c for c in name if unicodedata.category(c) != "Cc")[:32].strip() or "未命名"
        existing = next((tid for tid, tag in tags.items() if tag["name"] == name), None)
        tid = existing or uuid.uuid5(uuid.NAMESPACE_URL, "herdr-group:" + root).hex
        if tid not in tags:
            tags[tid] = {"name": name, "color": list(COLORS)[len(tags) % len(COLORS)]}
        assignments[root] = assignments[child] = tid
    state.update(version=2, tags=tags, assignments=assignments)
    state.pop("parents")
    state.setdefault("native_blocks", {})


def normalize(state, workspaces):
    live = {w["workspace_id"]: w for w in workspaces}
    previous = state["native_blocks"]
    blocks = native_blocks(workspaces, previous)
    roots = unit_roots(blocks)
    replacements = dict(roots)
    for block in previous.values():
        old = block["anchor"]
        if old not in live:
            survivor = next((wid for wid in block["members"] if wid in live), None)
            if survivor:
                replacements[old] = roots.get(survivor, survivor)
    assignments = {}
    # The main Space wins if separately tagged units join a native group.
    for wid, tid in sorted(state["assignments"].items(), key=lambda item: replacements.get(item[0], item[0]) != item[0]):
        root = replacements.get(wid, wid)
        if root in live:
            assignments.setdefault(root, tid)
    # If a native block dissolves, each surviving Space keeps the group's tag.
    for block in previous.values():
        tid = state["assignments"].get(block["anchor"])
        if tid is not None:
            for wid in block["members"]:
                if wid in live:
                    assignments.setdefault(roots.get(wid, wid), tid)
    state["assignments"] = assignments
    state["native_blocks"] = blocks
    for wid, record in list(state["labels"].items()):
        if wid not in live:
            del state["labels"][wid]
        else:
            adopt_label(record, live[wid]["label"])


def _workspace_colors(state, workspaces, include_linked):
    roots = unit_roots(state["native_blocks"])
    linked = {wid for block in state["native_blocks"].values() for wid in block["linked"]}
    labels = {}
    for workspace in workspaces:
        wid = workspace["workspace_id"]
        tag = state["tags"].get(state["assignments"].get(roots.get(wid, wid)))
        color = COLOR_HEX[tag["color"]] if tag and (include_linked or wid not in linked) else None
        label = state["labels"].get(wid, {}).get("base", workspace["label"])
        if label in labels and labels[label] != color:
            raise GroupError(f"同名 Space 无法分别着色：{label}。请先重命名，或使用相同标签颜色。")
        labels[label] = color
    rules = [{"equals": label, "fg": color} for label, color in labels.items() if color]
    if len(rules) > 16:
        raise GroupError("Herdr 0.9.5 每种 workspace 标题颜色规则最多支持 16 条，请减少已着色的 Space。")
    return sorted(rules, key=lambda rule: rule["equals"])


def title_rules(state, workspaces):
    """Colors native parent titles only; linked child rows keep their defaults."""
    return _workspace_colors(state, workspaces, include_linked=False)


def agent_workspace_rules(state, workspaces):
    """Color the workspace name shown beside agents, including linked Spaces."""
    return _workspace_colors(state, workspaces, include_linked=True)



class Service:
    def __init__(self, client=None, store=None, current_workspace_id=None, color_config=None):
        self.client = client or Client()
        self.store = store or Store(state_directory(), self.client.socket_path)
        if color_config is None:
            from title_colors import ColorConfig
            color_config = ColorConfig(self.client)
        self.color_config = color_config
        context = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON", "{}"))
        self.current_workspace_id = (current_workspace_id
                                     or os.environ.get("HERDR_GROUP_WORKSPACE_ID")
                                     or context.get("workspace_id")
                                     or os.environ.get("HERDR_WORKSPACE_ID"))
        self.start_tagging = os.environ.get("HERDR_GROUP_MODE") == "tag"

    def current(self):
        if not self.current_workspace_id:
            raise GroupError("没有调用方 Space，请显式指定 Space ID。")
        return self.current_workspace_id

    @staticmethod
    def require(workspaces, wid):
        for workspace in workspaces:
            if workspace["workspace_id"] == wid:
                return workspace
        raise GroupError(f"Space 不存在或已关闭: {wid}")

    def _execute(self, operation=None, apply=True):
        with self.store.locked():
            state = self.store.load()
            original = copy.deepcopy(state)
            workspaces = self.client.call("workspace.list")["workspaces"]
            migrate(state, workspaces)
            normalize(state, workspaces)
            result = operation(state, workspaces) if operation else None
            normalize(state, workspaces)
            if apply:
                self.color_config.validate(title_rules(state, workspaces))
                self.color_config.validate_agent_workspace(agent_workspace_rules(state, workspaces))
                if original["version"] == 1:
                    self.store.backup_legacy(original)
                # Dict equality ignores order, but tag insertion order controls sorting.
                if state != original or list(state["tags"]) != list(original.get("tags", {})):
                    self.store.save(state)
                self._apply(state, workspaces)
            live = {w["workspace_id"]: w for w in workspaces}
            by_anchor = {b["anchor"]: b for b in state["native_blocks"].values()}
            rows = []
            for root in ordered_units(workspaces, state):
                block = by_anchor.get(root, {"members": [root], "linked": []})
                tid = state["assignments"].get(root)
                tag = state["tags"].get(tid, {})
                for wid in block["members"]:
                    row = dict(live[wid])
                    row.update(base_label=state["labels"].get(wid, {}).get("base", row["label"]),
                               group_root_id=root, group_size=len(block["members"]),
                               native_child=wid in block["linked"],
                               tag_id=tid, tag_name=tag.get("name"), tag_color=tag.get("color"))
                    rows.append(row)
            return result, {"workspaces": rows, "tags": copy.deepcopy(state["tags"]),
                            "current_workspace_id": self.current_workspace_id}

    def _apply(self, state, workspaces):
        live = {w["workspace_id"]: w for w in workspaces}
        # Only legacy cleanup and explicit rename ever change a Space name.
        for wid, record in list(state["labels"].items()):
            desired = record["base"]
            if desired != live[wid]["label"]:
                fresh = self.client.call("workspace.get", workspace_id=wid)["workspace"]
                if fresh["label"] != live[wid]["label"]:
                    adopt_label(record, fresh["label"])
                    desired = record["base"]
                record.update(pending=desired, pending_prefix="")
                self.store.save(state)
                self.client.call("workspace.rename", workspace_id=wid, label=desired)
                live[wid]["label"] = desired
            del state["labels"][wid]
            self.store.save(state)
        self.color_config.apply(title_rules(state, workspaces))
        self.color_config.apply_agent_workspace(agent_workspace_rules(state, workspaces))
        if state["assignments"]:
            by_anchor = {b["anchor"]: b for b in state["native_blocks"].values()}
            desired = [wid for root in ordered_units(workspaces, state)
                       for wid in by_anchor.get(root, {"members": [root]})["members"]]
            actual = [w["workspace_id"] for w in self.client.call("workspace.list")["workspaces"]]
            if set(desired) != set(actual):
                raise WorkspaceListChanged("Space 列表在同步期间发生变化，请刷新重试。")
            if desired != actual:
                self.client.call("workspace.move_block", workspace_ids=desired)

    def snapshot(self):
        return self._execute(apply=False)[1]

    def sync(self):
        for attempt in range(3):
            try:
                return self._execute()[1]
            except WorkspaceListChanged:
                if attempt == 2:
                    raise

    def set_tag(self, workspace_ids, name, color=None):
        name = clean_label(name, 32)
        if color is not None and color not in COLORS:
            raise GroupError("未知标签颜色。")
        selected = list(dict.fromkeys(workspace_ids))
        if not selected:
            raise GroupError("请先选择至少一个 Space。")

        def operation(state, workspaces):
            for wid in selected:
                self.require(workspaces, wid)
            roots = unit_roots(state["native_blocks"])
            tid = next((tid for tid, tag in state["tags"].items() if tag["name"] == name), None)
            if tid is None:
                tid = uuid.uuid4().hex
                state["tags"][tid] = {"name": name, "color": color or list(COLORS)[len(state["tags"]) % len(COLORS)]}
            elif color:
                state["tags"][tid]["color"] = color
            for wid in selected:
                state["assignments"][roots.get(wid, wid)] = tid
            return tid

        return self._execute(operation)[0]

    def clear_tag(self, workspace_ids=None):
        selected = workspace_ids or [self.current()]

        def operation(state, workspaces):
            roots = unit_roots(state["native_blocks"])
            for wid in selected:
                self.require(workspaces, wid)
                state["assignments"].pop(roots.get(wid, wid), None)

        self._execute(operation)

    def update_tag(self, tag_id, name=None, color=None):
        if name is not None:
            name = clean_label(name, 32)
        if color is not None and color not in COLORS:
            raise GroupError("未知标签颜色。")

        def operation(state, workspaces):
            if tag_id not in state["tags"]:
                raise GroupError("标签不存在，请刷新。")
            if name is not None:
                if any(tid != tag_id and tag["name"] == name for tid, tag in state["tags"].items()):
                    raise GroupError("标签名称已存在，请使用设置标签合并成员。")
                state["tags"][tag_id]["name"] = name
            if color is not None:
                state["tags"][tag_id]["color"] = color

        self._execute(operation)

    def move_tag(self, tag_id, direction):
        if direction not in ("up", "down"):
            raise GroupError("标签移动方向必须是 up 或 down。")

        def operation(state, workspaces):
            if tag_id not in state["tags"]:
                raise GroupError("标签不存在，请刷新。")
            order = list(state["tags"])
            index = order.index(tag_id)
            target = index + (-1 if direction == "up" else 1)
            if not 0 <= target < len(order):
                return False
            order[index], order[target] = order[target], order[index]
            state["tags"] = {tid: state["tags"][tid] for tid in order}
            return True

        return self._execute(operation)[0]

    def rename(self, workspace_id, label):
        label = clean_label(label)

        def operation(state, workspaces):
            workspace = self.require(workspaces, workspace_id)
            state["labels"][workspace_id] = {"base": label, "applied": workspace["label"], "prefix": ""}

        self._execute(operation)

    def reset(self):
        def operation(state, workspaces):
            state["assignments"].clear()
            state["tags"].clear()
        self._execute(operation)

    def focus(self, workspace_id):
        self.client.call("workspace.focus", workspace_id=workspace_id)

    def open(self, tagging=False):
        env = {"HERDR_GROUP_WORKSPACE_ID": self.current()}
        if tagging:
            env["HERDR_GROUP_MODE"] = "tag"
        self.client.call("plugin.pane.open", plugin_id=PLUGIN_ID, entrypoint="manager",
                         placement="popup", focus=True, env=env)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("ui", "list", "sync", "reset"):
        sub.add_parser(name)
    open_parser = sub.add_parser("open")
    open_parser.add_argument("--tag", action="store_true")
    tag = sub.add_parser("tag", help="为 Space / worktree 整组设置标签")
    tag.add_argument("name")
    tag.add_argument("workspaces", nargs="+")
    tag.add_argument("--color", choices=COLORS)
    untag = sub.add_parser("untag", aliases=["detach"], help="移除标签；默认当前 Space")
    untag.add_argument("workspaces", nargs="*")
    edit_tag = sub.add_parser("tag-edit", help="修改标签名称或颜色")
    edit_tag.add_argument("tag_id")
    edit_tag.add_argument("--name")
    edit_tag.add_argument("--color", choices=COLORS)
    rename = sub.add_parser("rename")
    rename.add_argument("workspace")
    rename.add_argument("label")
    args = parser.parse_args(argv)
    try:
        service = Service()
        if args.command == "ui":
            from group_ui import run
            run(service)
        elif args.command == "open":
            service.open(args.tag)
        elif args.command == "tag":
            print(service.set_tag(args.workspaces, args.name, args.color))
        elif args.command in ("untag", "detach"):
            service.clear_tag(args.workspaces)
        elif args.command == "tag-edit":
            service.update_tag(args.tag_id, args.name, args.color)
        elif args.command == "rename":
            service.rename(args.workspace, args.label)
        elif args.command == "reset":
            service.reset()
        elif args.command == "sync":
            service.sync()
        elif args.command == "list":
            print(json.dumps(service.snapshot(), ensure_ascii=False, indent=2))
    except (GroupError, OSError, ValueError) as exc:
        print(f"Herdr Colored Tag: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
