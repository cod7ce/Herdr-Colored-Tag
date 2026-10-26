"""Managed Space and Agents workspace-label colors in Herdr's shared config.

Named Herdr sessions share the XDG config file, so rules match globally by title.
"""

import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import unicodedata

# Keep legacy markers so existing checksummed config blocks remain readable.
BEGIN = b"# BEGIN local.herdr-group title colors"
END = b"# END local.herdr-group title colors"
_DIGEST = re.compile(rb"(?m)^# local\.herdr-group sha256=([0-9a-f]{64})\r?$")
_OWNER = re.compile(rb"(?m)^# local\.herdr-group owner=([0-9a-f]{64})\r?$")
_SEPARATOR = b"# local.herdr-group separator-newline=true"
_AGENT_BEGIN = b"# BEGIN local.herdr-group agent workspace colors"
_AGENT_END = b"# END local.herdr-group agent workspace colors"
_AGENT_DIGEST = re.compile(rb"(?m)^# local\.herdr-group agent-sha256=([0-9a-f]{64})\r?$")
_AGENT_OWNER = re.compile(rb"(?m)^# local\.herdr-group agent-owner=([0-9a-f]{64})\r?$")
_AGENT_SEPARATOR = b"# local.herdr-group agent-separator-newline=true"
_KEY_PART = rb'''(?:[A-Za-z0-9_-]+|"(?:[^"\\\r\n]|\\.)*"|'[^'\r\n]*')'''
_KEY_PATH = _KEY_PART + rb'(?:[ \t]*\.[ \t]*' + _KEY_PART + rb')*'
_HEADER = re.compile(rb'^[ \t]*(\[\[?)[ \t]*(' + _KEY_PATH + rb')[ \t]*(\]\]?)[ \t]*(?:#.*)?$')
_ASSIGNMENT = re.compile(rb'^[ \t]*(' + _KEY_PATH + rb')[ \t]*=')


def _key_path(raw):
    parts = []
    escapes = {'b': '\b', 't': '\t', 'n': '\n', 'f': '\f', 'r': '\r', '"': '"', '\\': '\\'}

    def unescape(match):
        value = match.group(1)
        return chr(int(value[1:], 16)) if value[0] in 'uU' else escapes[value]

    for match in re.finditer(_KEY_PART, raw):
        key = match.group().decode('utf-8')
        if key.startswith('"'):
            key = re.sub(r'\\(u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|[btnfr"\\])', unescape, key[1:-1])
        elif key.startswith("'"):
            key = key[1:-1]
        parts.append(key)
    return tuple(parts)


def _toml_entries(data):
    """Read table/key paths, skipping values; this is not a full TOML validator."""
    table, quote, depth = (), None, 0
    for number, line in enumerate(data.splitlines(keepends=True)):
        if not quote and not depth:
            header = _HEADER.fullmatch(line.rstrip(b'\r\n'))
            if header and len(header[1]) == len(header[3]):
                table = _key_path(header[2])
                yield number, 'array' if len(header[1]) == 2 else 'table', table
                continue
            assignment = _ASSIGNMENT.match(line)
            if not assignment:
                continue
            yield number, 'key', table + _key_path(assignment[1])
            line = line[assignment.end():]
        # Track only strings and nesting so header-like text in a multiline
        # value cannot be mistaken for configuration structure.
        pos = 0
        while pos < len(line):
            char = line[pos:pos + 1]
            if quote:
                if quote.startswith(b'"') and char == b'\\':
                    pos += 2
                    continue
                if line.startswith(quote, pos):
                    pos += len(quote)
                    if len(quote) == 3:
                        # TOML allows one or two quote characters before the
                        # closing triple quote (four/five quotes in total).
                        for _ in range(2):
                            if line[pos:pos + 1] == char:
                                pos += 1
                    quote = None
                    continue
            elif char == b'#':
                break
            elif char in (b'"', b"'"):
                quote = char * 3 if line.startswith(char * 3, pos) else char
                pos += len(quote)
                continue
            elif char in (b'[', b'{'):
                depth += 1
            elif char in (b']', b'}'):
                depth -= 1
            pos += 1


class ColorConfigError(ValueError):
    """A title-color config could not be safely validated or applied."""


class ColorConfig:
    def __init__(self, client, path=None):
        self.client = client
        if path is None:
            config_home = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
            path = config_home / "herdr" / "config.toml"
        self.path = Path(path).expanduser()
        self.lock_path = self.path.with_name(self.path.name + ".herdr-title-colors.lock")
        socket_path = getattr(client, "socket_path", None)
        self.owner = hashlib.sha256(os.fsencode(socket_path or "default-herdr-session")).hexdigest().encode("ascii")

    def validate(self, rules):
        """Validate serialization and config ownership without changing files."""
        rendered = self._render(rules, separator_newline=False)
        data, _ = self._read()
        block = self._locate(data, check_unmanaged=bool(rules))
        if rendered and block and block[3] != self.owner:
            raise ColorConfigError("the shared Herdr config already has title colors owned by another session")
        return rendered

    def apply(self, rules):
        rendered_rules = self._render(rules, separator_newline=False)
        # Clearing a missing block is a true no-op, including no lock-file creation.
        if not rendered_rules:
            initial, exists = self._read()
            if not exists:
                return False
            located = self._locate(initial, check_unmanaged=False)
            if not located or located[3] != self.owner:
                return False

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked():
            original, exists = self._read()
            block = self._locate(original, check_unmanaged=bool(rules))
            if not rendered_rules and not block:
                return False
            if block and block[3] != self.owner:
                if not rendered_rules:
                    return False
                raise ColorConfigError("the shared Herdr config already has title colors owned by another session")
            if rendered_rules and not block:
                self._check_unmanaged_spaces(original)
            if rendered_rules:
                # Own a separate newline even when a preceding managed block
                # already ends with LF; either block can then be removed first.
                separator = bool(original) if not block else block[2]
                rendered = self._render(rules, separator_newline=separator, owner=self.owner)
                if block:
                    start, end = block[:2]
                    outer_separator = b"\n" if block[2] else b""
                    updated = original[:start] + outer_separator + rendered + original[end:]
                else:
                    updated = original + (b"\n" if separator else b"") + rendered
            else:
                updated = self._remove_block(original, block, agent=False)
            if updated == original:
                return False

            mode = stat.S_IMODE(self.path.stat().st_mode) if exists else 0o600
            backup = self._backup(original, mode) if exists else None
            self._replace(updated, mode)
            try:
                self._reload()
            except Exception as exc:
                rollback = self._restore_if_unchanged(updated, original, mode)
                try:
                    self._reload()
                except Exception as restore_exc:
                    restore_note = f"; restoring reload also failed: {restore_exc}"
                else:
                    restore_note = "; original config reloaded"
                backup_note = f"; backup: {backup}" if backup else ""
                raise ColorConfigError(
                    f"Herdr rejected title colors ({exc}); "
                    f"config {'restored' if rollback else 'left untouched because it changed concurrently'}"
                    f"{restore_note}{backup_note}"
                ) from exc
            return True

    def _render(self, rules, separator_newline, owner=None):
        if not isinstance(rules, list) or len(rules) > 16:
            raise ColorConfigError("title color rules must be a list of at most 16 entries")
        seen = set()
        values = []
        for index, rule in enumerate(rules):
            if not isinstance(rule, dict) or set(rule) != {"equals", "fg"}:
                raise ColorConfigError(f"title color rule {index + 1} must contain equals and fg")
            title, color = rule["equals"], rule["fg"]
            if not isinstance(title, str) or not title or any(unicodedata.category(ch) == "Cc" for ch in title):
                raise ColorConfigError(f"title color rule {index + 1} has an invalid title")
            if title in seen:
                raise ColorConfigError(f"duplicate title in title color rules: {title!r}")
            if not isinstance(color, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
                raise ColorConfigError(f"title color rule {index + 1} has an invalid foreground color")
            seen.add(title)
            values.append(f"{{equals = {json.dumps(title, ensure_ascii=False)}, fg = {json.dumps(color)}}}")
        if not values:
            return b""
        rows = ", ".join(values)
        body = (
            '[ui.sidebar.spaces]\n'
            f'rows = [["state_icon", {{token = "workspace", rules = [{rows}]}}], '
            '["branch", "git_status"]]\n'
        ).encode("utf-8")
        separator_line = _SEPARATOR + b"\n" if separator_newline else b""
        owner_line = b"# local.herdr-group owner=" + owner + b"\n" if owner else b""
        digest = hashlib.sha256(separator_line + owner_line + body).hexdigest().encode("ascii")
        return (BEGIN + b"\n" + separator_line + owner_line + body + b"# local.herdr-group sha256=" + digest
                + b"\n" + END + b"\n")

    def _read(self):
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return b"", False
        except OSError as exc:
            raise ColorConfigError(f"cannot inspect Herdr config {self.path}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise ColorConfigError(f"refusing to replace symlinked Herdr config: {self.path}")
        if not stat.S_ISREG(info.st_mode):
            raise ColorConfigError(f"Herdr config is not a regular file: {self.path}")
        try:
            return self.path.read_bytes(), True
        except OSError as exc:
            raise ColorConfigError(f"cannot read Herdr config {self.path}: {exc}") from exc

    def _locate(self, data, check_unmanaged=True):
        lines = data.splitlines(keepends=True)
        starts = [i for i, line in enumerate(lines) if self._line(line) == BEGIN]
        ends = [i for i, line in enumerate(lines) if self._line(line) == END]
        if not starts and not ends:
            if check_unmanaged:
                self._check_unmanaged_spaces(data)
            return None
        if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
            raise ColorConfigError("managed title-color markers are malformed or duplicated")
        first, last = starts[0], ends[0]
        body = b"".join(lines[first + 1:last])
        match = _DIGEST.search(body)
        owner_match = _OWNER.search(body)
        if not match or not owner_match:
            raise ColorConfigError("managed title-color block has no ownership or integrity marker")
        digest_line_start = match.start()
        config_body = body[:digest_line_start]
        if hashlib.sha256(config_body).hexdigest().encode("ascii") != match.group(1):
            raise ColorConfigError("managed title-color block was edited outside this tool")
        separator = _SEPARATOR in body
        offsets = []
        offset = 0
        for line in lines:
            offsets.append(offset)
            offset += len(line)
        start = offsets[first]
        end = offsets[last] + len(lines[last])
        if separator and start:
            previous = data[:start]
            if previous.endswith(b"\r\n"):
                start -= 2
            elif previous.endswith(b"\n"):
                start -= 1
            else:
                raise ColorConfigError("managed title-color separator is missing")
        unowned = b"".join(lines[:first] + lines[last + 1:])
        # Another spaces table outside this block would make ownership ambiguous.
        self._check_unmanaged_spaces(unowned)
        # Bare keys after END still belong to this TOML table; avoid consuming them.
        following = lines[last + 1:]
        for line in following:
            stripped = line.strip()
            if stripped.startswith(b"["):
                break
            if not stripped or stripped.startswith(b"#"):
                continue
            raise ColorConfigError("bare TOML keys follow the managed block before the next table")
        return start, end, separator, owner_match.group(1)

    @staticmethod
    def _line(line):
        return line[:-2] if line.endswith(b"\r\n") else line[:-1] if line.endswith(b"\n") else line

    @classmethod
    def _check_unmanaged_spaces(cls, data):
        cls._check_layout(data, 'spaces', allow_empty=False)

    @staticmethod
    def _check_layout(data, section, allow_empty):
        target = ('ui', 'sidebar', section)
        bases = []
        for number, kind, path in _toml_entries(data):
            if path[:len(target)] == target or (kind == 'key' and target[:len(path)] == path):
                if allow_empty and kind == 'table' and path == target and not bases:
                    bases.append(number)
                else:
                    raise ColorConfigError(
                        f"[ui.sidebar.{section}] already exists or contains custom layout settings; "
                        "refusing to overwrite it"
                    )
        return bases

    def _remove_block(self, data, block, agent):
        start, end, separator = block[:3]
        prefix, suffix = data[:start], data[end:]
        if separator and prefix and suffix and not prefix.endswith(b'\n') and not suffix.startswith((b'\n', b'\r\n')):
            # Older releases let adjacent blocks share an added newline. Move
            # its ownership to the surviving block, preserving legacy markers.
            other_begin = BEGIN if agent else _AGENT_BEGIN
            if self._line(suffix.splitlines(keepends=True)[0]) == other_begin:
                other = self._locate(data) if agent else self._locate_agent(data)
                if other[3] != self.owner:
                    raise ColorConfigError("cannot transfer a managed separator to colors owned by another session")
                if not other[2]:
                    marker = _SEPARATOR if agent else _AGENT_SEPARATOR
                    digest_pattern = _DIGEST if agent else _AGENT_DIGEST
                    first_line = suffix.index(b'\n') + 1
                    match = digest_pattern.search(suffix)
                    body = marker + b'\n' + suffix[first_line:match.start()]
                    digest = hashlib.sha256(body).hexdigest().encode('ascii')
                    suffix = (suffix[:first_line] + body + suffix[match.start():match.start(1)]
                              + digest + suffix[match.end(1):])
            return prefix + b'\n' + suffix
        return prefix + suffix

    @contextlib.contextmanager
    def _locked(self):
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise ColorConfigError(f"cannot open config lock {self.lock_path}: {exc}") from exc
        with os.fdopen(descriptor, "a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _backup(self, original, mode):
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        base = self.path.with_name(self.path.name + "." + stamp + ".bak")
        for suffix in range(10000):
            backup = base if suffix == 0 else base.with_name(base.name + f".{suffix}")
            try:
                descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            except FileExistsError:
                continue
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(original)
                    stream.flush()
                    os.fsync(stream.fileno())
            except Exception:
                with contextlib.suppress(OSError):
                    backup.unlink()
                raise
            return backup
        raise ColorConfigError(f"cannot create a unique dated backup beside {self.path}")

    def _replace(self, data, mode):
        descriptor, temporary = tempfile.mkstemp(prefix="." + self.path.name + ".", dir=self.path.parent)
        try:
            os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self._sync_directory()
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _restore_if_unchanged(self, expected, original, mode):
        try:
            current, exists = self._read()
            if not exists or current != expected:
                return False
            self._replace(original, mode)
            return True
        except (OSError, ColorConfigError):
            return False

    def _reload(self):
        result = self.client.call("server.reload_config")
        status = result.get("status") if isinstance(result, dict) else None
        diagnostics = result.get("diagnostics", []) if isinstance(result, dict) else []
        if status != "applied":
            details = "; ".join(str(item) for item in diagnostics) if diagnostics else "no diagnostics"
            raise ColorConfigError(f"reload status was {status!r}: {details}")

    def validate_agent_workspace(self, rules):
        self._render_agent_workspace(rules, separator_newline=False)
        data, exists = self._read()
        block = self._locate_agent(data, check_unmanaged=bool(rules)) if exists else None
        if rules and block and block[3] != self.owner:
            raise ColorConfigError("the shared Herdr config already has agent workspace colors owned by another session")
        return rules

    def apply_agent_workspace(self, rules):
        rendered_rules = self._render_agent_workspace(rules, separator_newline=False)
        initial, exists = self._read()
        block = self._locate_agent(initial, check_unmanaged=bool(rules)) if exists else None
        if not rendered_rules and (not block or block[3] != self.owner):
            return False
        if rendered_rules and block and block[3] != self.owner:
            raise ColorConfigError("the shared Herdr config already has agent workspace colors owned by another session")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked():
            original, exists = self._read()
            block = self._locate_agent(original, check_unmanaged=bool(rules)) if exists else None
            if not rendered_rules and (not block or block[3] != self.owner):
                return False
            if rendered_rules and block and block[3] != self.owner:
                raise ColorConfigError("the shared Herdr config already has agent workspace colors owned by another session")
            if block:
                start, end, separator, _, _ = block
                insertion = None
            else:
                insertion, existing_table = self._agent_insertion(original)
                created_table = not existing_table
                separator = bool(insertion)
                start = end = None
            include_table = created_table if not block else block[4]
            rendered = self._render_agent_workspace(
                rules, separator_newline=separator, owner=self.owner, include_table=include_table,
            ) if rendered_rules else b""
            if block:
                if rendered:
                    updated = original[:start] + (b"\n" if separator else b"") + rendered + original[end:]
                else:
                    updated = self._remove_block(original, block, agent=True)
            elif rendered:
                updated = original[:insertion] + (b"\n" if separator else b"") + rendered + original[insertion:]
            else:
                return False
            if updated == original:
                return False
            mode = stat.S_IMODE(self.path.stat().st_mode) if exists else 0o600
            backup = self._backup(original, mode) if exists else None
            self._replace(updated, mode)
            try:
                self._reload()
            except Exception as exc:
                rollback = self._restore_if_unchanged(updated, original, mode)
                try:
                    self._reload()
                    restore_note = "; original config reloaded"
                except Exception as restore_exc:
                    restore_note = f"; restoring reload also failed: {restore_exc}"
                backup_note = f"; backup: {backup}" if backup else ""
                raise ColorConfigError(
                    f"Herdr rejected agent workspace colors ({exc}); "
                    f"config {'restored' if rollback else 'left untouched because it changed concurrently'}"
                    f"{restore_note}{backup_note}"
                ) from exc
            return True

    def _agent_insertion(self, data):
        """Insert the managed agent rows inside an existing empty agents table."""
        lines = data.splitlines(keepends=True)
        starts = self._check_unmanaged_agents(data)
        if not starts:
            return len(data), False
        start = starts[0]
        offset = sum(map(len, lines[:start + 1]))
        for line in lines[start + 1:]:
            stripped = line.strip()
            if stripped.startswith(b"[") or stripped in (BEGIN, _AGENT_BEGIN):
                break
            if stripped and not stripped.startswith(b"#"):
                raise ColorConfigError("[ui.sidebar.agents] already contains settings; refusing to overwrite them")
            offset += len(line)
        # Do not take a newline owned by an adjacent Spaces block. Keep it
        # with that block so clearing either section preserves exact bytes.
        spaces = self._locate(data, check_unmanaged=False)
        if spaces and spaces[2] and spaces[0] < offset <= spaces[0] + 2:
            offset = spaces[0]
        return offset, True

    def _render_agent_workspace(self, rules, separator_newline, owner=None, include_table=True):
        if not isinstance(rules, list) or len(rules) > 16:
            raise ColorConfigError("agent workspace color rules must be a list of at most 16 entries")
        seen = set()
        values = []
        for index, rule in enumerate(rules):
            if not isinstance(rule, dict) or set(rule) != {"equals", "fg"}:
                raise ColorConfigError(f"agent workspace rule {index + 1} must contain equals and fg")
            title, color = rule["equals"], rule["fg"]
            if not isinstance(title, str) or not title or any(unicodedata.category(ch) == "Cc" for ch in title):
                raise ColorConfigError(f"agent workspace rule {index + 1} has an invalid title")
            if title in seen:
                raise ColorConfigError(f"duplicate agent workspace title: {title!r}")
            if not isinstance(color, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
                raise ColorConfigError(f"agent workspace rule {index + 1} has an invalid foreground color")
            seen.add(title)
            values.append(f"{{equals = {json.dumps(title, ensure_ascii=False)}, fg = {json.dumps(color)}}}")
        if not values:
            return b""
        rules_text = ", ".join(values)
        table = '[ui.sidebar.agents]\n' if include_table else ""
        body = (
            table + 'rows = [["state_icon", "machine", {token = "workspace", rules = ['
            + rules_text + ']}, "tab"], ["agent"]]\n'
        ).encode("utf-8")
        separator_line = _AGENT_SEPARATOR + b"\n" if separator_newline else b""
        owner_line = b"# local.herdr-group agent-owner=" + owner + b"\n" if owner else b""
        digest = hashlib.sha256(separator_line + owner_line + body).hexdigest().encode("ascii")
        return (_AGENT_BEGIN + b"\n" + separator_line + owner_line + body
                + b"# local.herdr-group agent-sha256=" + digest + b"\n" + _AGENT_END + b"\n")

    def _locate_agent(self, data, check_unmanaged=True):
        lines = data.splitlines(keepends=True)
        starts = [i for i, line in enumerate(lines) if self._line(line) == _AGENT_BEGIN]
        ends = [i for i, line in enumerate(lines) if self._line(line) == _AGENT_END]
        if not starts and not ends:
            if check_unmanaged:
                self._check_unmanaged_agents(data)
            return None
        if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
            raise ColorConfigError("managed agent workspace markers are malformed or duplicated")
        first, last = starts[0], ends[0]
        body = b"".join(lines[first + 1:last])
        match, owner_match = _AGENT_DIGEST.search(body), _AGENT_OWNER.search(body)
        if not match or not owner_match:
            raise ColorConfigError("managed agent workspace block has no ownership or integrity marker")
        if hashlib.sha256(body[:match.start()]).hexdigest().encode("ascii") != match.group(1):
            raise ColorConfigError("managed agent workspace block was edited outside this tool")
        start = sum(map(len, lines[:first]))
        end = start + sum(map(len, lines[first:last + 1]))
        separator = _AGENT_SEPARATOR in body
        if separator and start:
            previous = data[:start]
            if previous.endswith(b"\r\n"):
                start -= 2
            elif previous.endswith(b"\n"):
                start -= 1
            else:
                raise ColorConfigError("managed agent workspace separator is missing")
        created_table = any(kind == 'table' and path == ('ui', 'sidebar', 'agents')
                            for _, kind, path in _toml_entries(body))
        unowned = b"".join(lines[:first] + lines[last + 1:])
        bases = self._check_layout(unowned, 'agents', allow_empty=not created_table)
        if not created_table:
            enclosing = None
            for _, kind, path in _toml_entries(b"".join(lines[:first])):
                if kind in ('table', 'array'):
                    enclosing = (kind, path)
            if not bases or enclosing != ('table', ('ui', 'sidebar', 'agents')):
                raise ColorConfigError("managed agent rows require an enclosing [ui.sidebar.agents] table")
        for line in lines[last + 1:]:
            stripped = line.strip()
            if stripped.startswith(b"["):
                break
            if stripped and not stripped.startswith(b"#"):
                raise ColorConfigError("bare TOML keys follow the managed agent block before the next table")
        return start, end, separator, owner_match.group(1), created_table

    @classmethod
    def _check_unmanaged_agents(cls, data):
        return cls._check_layout(data, 'agents', allow_empty=True)

    def _sync_directory(self):
        try:
            descriptor = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            # Some filesystems do not support syncing directory handles.
            pass
