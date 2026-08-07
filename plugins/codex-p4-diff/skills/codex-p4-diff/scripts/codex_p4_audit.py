#!/usr/bin/env python3
"""Codex lifecycle hook that records one local Git commit per changed turn."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any, Iterable

import codex_p4_diff as bridge


MAX_CAPTURE_BYTES = int(os.environ.get("CODEX_P4_DIFF_MAX_BLOB_BYTES", 8 * 1024 * 1024))
LOCK_STALE_SECONDS = 24 * 60 * 60
OWNERSHIP_SCHEMA_VERSION = 6
MUTATING_TOOLS = {"apply_patch", "Edit", "Write", "Bash", "shell_command"}
IGNORED_TOP_LEVEL = {
    ".git",
    ".svn",
    "Binaries",
    "DerivedDataCache",
    "Intermediate",
    "Saved",
    "node_modules",
}


class AuditError(RuntimeError):
    """Raised when audit integrity would be lost."""


class HookSkip(RuntimeError):
    """Raised when the hook does not apply to the current task."""


class OwnershipConflict(AuditError):
    """Raised when another active Codex session owns a local path."""


@dataclass(frozen=True)
class AuditContext:
    p4: dict[str, Any]
    project_root: Path
    session_id: str
    branch: str
    history_root: Path
    state_root: Path
    session_root: Path
    git_exe: str


@dataclass(frozen=True)
class ShellToken:
    value: str
    quoted: bool = False
    operator: bool = False


def utc_now() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_json_if_exists(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


@contextmanager
def project_lock(state_root: Path):
    state_root.mkdir(parents=True, exist_ok=True)
    lock = state_root / ".project.lock"
    deadline = time.monotonic() + 20
    token = f"{os.getpid()}:{time.time_ns()}"
    while True:
        try:
            descriptor = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                stream.write(token)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 120:
                    lock.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise AuditError(f"等待本地历史锁超时：{lock}")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            if lock.read_text(encoding="ascii", errors="ignore") == token:
                lock.unlink()
        except FileNotFoundError:
            pass


def redact_summary(text: str, limit: int = 180) -> str:
    value = re.sub(r"\s+", " ", text).strip()
    patterns = [
        (r"(?i)\b(token|password|secret)\s*[=:]\s*[^\s,;]+", r"\1=<redacted>"),
        (r"(?i)Bearer\s+[^\s,;]+", "Bearer <redacted>"),
    ]
    for pattern, replacement in patterns:
        value = re.sub(pattern, replacement, value)
    if not value:
        value = "Codex file changes"
    return value[:limit]


GENERIC_ASSISTANT_SUMMARY_LINES = {
    '已完成',
    '完成',
    '处理完成',
    '已处理',
    '完成情况',
    '完成内容',
    '更新内容',
    '修改内容',
    '改动内容',
    '主要改动',
    '结果',
    '验证',
    '验证通过',
    '测试通过',
    'done',
    'completed',
    'summary',
    'changes',
    'results',
    'verification',
    'testspassed',
}


def strip_basic_markdown(line: str) -> str:
    value = re.sub(r'^\s{0,3}(?:#{1,6}\s+|>\s*)', '', line)
    value = re.sub(r'^\s*(?:[-+*]\s+|\d+[.)]\s+)', '', value)
    value = re.sub(r'^\s*\[[ xX]\]\s+', '', value)
    value = re.sub(r'!\[([^\]]*)\]\([^)]+\)', r'\1', value)
    value = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', value)
    value = value.replace('**', '').replace('__', '').replace('~~', '').replace(chr(96), '')
    return re.sub(r'\s+', ' ', value).strip()


def extract_assistant_change_summary(message: Any) -> str | None:
    text = str(message or '')
    in_fence = False
    fence = chr(96) * 3
    for raw_line in text.splitlines():
        if raw_line.strip().startswith(fence) or raw_line.strip().startswith('~~~'):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        line = strip_basic_markdown(raw_line)
        if not line or line.startswith('::') or line.startswith('<oai-'):
            continue
        normalized = re.sub(r'[\s。.!！,:：;；]+', '', line).casefold()
        if normalized in GENERIC_ASSISTANT_SUMMARY_LINES:
            continue
        return redact_summary(line)
    return None


def fallback_change_summary(changes: list[dict[str, Any]]) -> str:
    if not changes:
        return '更新文件'
    names = [
        Path(str(change.get('path') or '')).name or str(change.get('path') or '')
        for change in changes
    ]
    if len(changes) == 1:
        verb = {'add': '新增', 'delete': '删除', 'edit': '修改'}.get(
            str(changes[0].get('action') or ''), '更新'
        )
        return redact_summary(f'{verb} {names[0]}')
    preview = '、'.join(names[:2])
    suffix = ' 等' if len(names) > 2 else ''
    return redact_summary(f'更新 {len(changes)} 个文件：{preview}{suffix}')


def build_change_summary(
    event: dict[str, Any], changes: list[dict[str, Any]]
) -> tuple[str, str, str | None]:
    assistant_message = str(event.get('last_assistant_message') or '')
    assistant_sha256 = (
        sha256_bytes(assistant_message.encode('utf-8', errors='surrogatepass'))
        if assistant_message
        else None
    )
    summary = extract_assistant_change_summary(assistant_message)
    if summary:
        return summary, 'assistant', assistant_sha256
    return fallback_change_summary(changes), 'files', assistant_sha256


def normalize_session_id(value: Any) -> str:
    session_id = str(value or "").strip()
    if not session_id:
        raise HookSkip("Hook 输入缺少 Codex session_id")
    return session_id


def resolve_context(event: dict[str, Any]) -> AuditContext:
    cwd_text = str(event.get("cwd") or os.getcwd()).strip()
    project_root = Path(cwd_text).resolve()
    if not project_root.is_dir():
        raise HookSkip(f"Codex cwd 不存在：{project_root}")
    session_id = normalize_session_id(event.get("session_id"))
    try:
        p4_context = bridge.discover_p4_context(project_root)
    except bridge.CodexP4DiffError as exc:
        raise HookSkip(str(exc)) from exc
    paths = bridge.storage_paths(p4_context, project_root)
    paths["state"].mkdir(parents=True, exist_ok=True)
    with project_lock(paths["state"]):
        history = bridge.ensure_history_repo(p4_context, project_root)
    session_key = bridge.sanitize_component(session_id, 80)
    return AuditContext(
        p4=p4_context,
        project_root=project_root,
        session_id=session_id,
        branch=bridge.HISTORY_BRANCH,
        history_root=Path(history["historyRoot"]),
        state_root=Path(history["stateRoot"]),
        session_root=Path(history["stateRoot"]) / "sessions" / session_key,
        git_exe=str(history["gitExe"]),
    )


def session_path(ctx: AuditContext) -> Path:
    return ctx.session_root / "session.json"


def base_session(ctx: AuditContext) -> dict[str, Any]:
    return {
        "schemaVersion": bridge.SCHEMA_VERSION,
        "generatedBy": bridge.TOOL_NAME,
        "sessionId": ctx.session_id,
        "branch": ctx.branch,
        "projectRoot": str(ctx.project_root),
        "historyRoot": str(ctx.history_root),
        "serverAddress": ctx.p4.get("serverAddress"),
        "client": ctx.p4.get("client"),
        "status": "active",
        "createdAt": utc_now(),
        "updatedAt": utc_now(),
        "nextSequence": 1,
        "activeTurnId": None,
        "turns": {},
        "ownedFiles": [],
    }


def load_session(ctx: AuditContext) -> dict[str, Any]:
    session = read_json_if_exists(session_path(ctx), {})
    if not session:
        return base_session(ctx)
    expected = {
        "sessionId": ctx.session_id,
        "projectRoot": str(ctx.project_root),
        "branch": ctx.branch,
    }
    for key, value in expected.items():
        if session.get(key) != value:
            raise AuditError(f"会话账本上下文不一致：{key}")
    return session


def save_session(ctx: AuditContext, session: dict[str, Any]) -> None:
    session["updatedAt"] = utc_now()
    atomic_write_json(session_path(ctx), session)


def normalize_turn_id(ctx: AuditContext, event: dict[str, Any], session: dict[str, Any]) -> str:
    explicit = str(event.get("turn_id") or "").strip()
    if explicit:
        return explicit
    active = str(session.get("activeTurnId") or "").strip()
    if active:
        return active
    return f"turn-{int(time.time() * 1000)}"


def turn_root(ctx: AuditContext, turn_id: str) -> Path:
    return ctx.session_root / "turns" / bridge.sanitize_component(turn_id, 96)


def prompt_record_path(ctx: AuditContext, turn_id: str) -> Path:
    return turn_root(ctx, turn_id) / "prompt.json"


def before_manifest_path(ctx: AuditContext, turn_id: str) -> Path:
    return turn_root(ctx, turn_id) / "before.json"


def is_ignored_path(ctx: AuditContext, path: Path) -> bool:
    try:
        relative = path.resolve().relative_to(ctx.project_root).parts
    except ValueError:
        return True
    return bool(relative and relative[0] in IGNORED_TOP_LEVEL)


def relative_path(ctx: AuditContext, path: Path) -> str:
    try:
        return path.resolve().relative_to(ctx.project_root).as_posix()
    except ValueError as exc:
        raise AuditError(f"工具目标不在项目目录内：{path}") from exc


def resolve_candidate_path(ctx: AuditContext, value: str, cwd: Path) -> Path | None:
    cleaned = value.strip().strip("'\"` ,;()[]{}")
    if not cleaned or cleaned.startswith("//") or cleaned.startswith("-"):
        return None
    cleaned = cleaned.replace("/", os.sep)
    candidate = Path(cleaned)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(ctx.project_root)
    except (OSError, ValueError):
        return None
    if is_ignored_path(ctx, resolved) or resolved.is_dir():
        return None
    return resolved


def parse_apply_patch_paths(command: str, cwd: Path, project_root: Path) -> list[Path]:
    paths: list[Path] = []
    for line in command.splitlines():
        match = re.match(r"^\*\*\* (?:Update|Add|Delete) File:\s*(.+?)\s*$", line)
        if not match:
            match = re.match(r"^\*\*\* Move to:\s*(.+?)\s*$", line)
        if not match:
            continue
        candidate = Path(match.group(1).strip())
        if not candidate.is_absolute():
            candidate = cwd / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(project_root.resolve())
        except ValueError as exc:
            raise bridge.CodexP4DiffError(f"apply_patch 路径越出项目目录：{candidate}") from exc
        if resolved not in paths:
            paths.append(resolved)
    return paths


SHELL_MUTATING_COMMANDS = {
    "add-content",
    "apply_patch",
    "clear-content",
    "copy-item",
    "cp",
    "clang-format",
    "move-item",
    "mv",
    "new-item",
    "out-file",
    "prettier",
    "remove-item",
    "rename-item",
    "rm",
    "sed",
    "set-content",
    "tee",
    "touch",
}
SHELL_COMMAND_SEPARATORS = {";", "|", "\n", "\r", "{", "}"}


def shell_tokens(command: str) -> list[ShellToken]:
    """Tokenize enough PowerShell/POSIX syntax to keep strings out of path detection."""
    tokens: list[ShellToken] = []
    value: list[str] = []
    quote = ""
    token_quoted = False
    index = 0

    def flush() -> None:
        nonlocal token_quoted
        if value:
            tokens.append(ShellToken("".join(value), quoted=token_quoted))
            value.clear()
        token_quoted = False

    while index < len(command):
        char = command[index]
        if quote:
            if char == quote:
                if quote == "'" and index + 1 < len(command) and command[index + 1] == "'":
                    value.append("'")
                    index += 2
                    continue
                quote = ""
                token_quoted = True
                index += 1
                continue
            if quote == '"' and char == "`" and index + 1 < len(command):
                value.append(command[index + 1])
                index += 2
                continue
            value.append(char)
            index += 1
            continue

        if char in {"'", '"'}:
            quote = char
            token_quoted = True
            index += 1
            continue
        if char == "`" and index + 1 < len(command):
            value.append(command[index + 1])
            index += 2
            continue
        if char.isspace():
            flush()
            if char in {"\n", "\r"}:
                tokens.append(ShellToken(char, operator=True))
            index += 1
            continue
        if char in {";", "|", "{", "}", "="}:
            flush()
            tokens.append(ShellToken(char, operator=True))
            index += 1
            continue
        if char == ">":
            flush()
            operator = ">>" if index + 1 < len(command) and command[index + 1] == ">" else ">"
            tokens.append(ShellToken(operator, operator=True))
            index += len(operator)
            continue
        value.append(char)
        index += 1
    flush()
    return tokens


def shell_command_segments(tokens: list[ShellToken]) -> list[list[ShellToken]]:
    segments: list[list[ShellToken]] = []
    current: list[ShellToken] = []
    for token in tokens:
        if token.operator and token.value in SHELL_COMMAND_SEPARATORS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def normalized_shell_command(token: ShellToken) -> str:
    return token.value.strip("&() ").casefold()


def shell_redirection_targets(tokens: list[ShellToken]) -> list[ShellToken]:
    targets: list[ShellToken] = []
    conditional_depth = 0
    for index, token in enumerate(tokens[:-1]):
        if token.value in {"[[", "(("}:
            conditional_depth += 1
            continue
        if token.value in {"]]", "))"}:
            conditional_depth = max(0, conditional_depth - 1)
            continue
        if not token.operator or token.value not in {">", ">>"}:
            continue
        if conditional_depth:
            continue
        candidate = tokens[index + 1]
        if candidate.value.startswith("&"):
            continue
        targets.append(candidate)
    return targets


def mutating_command_indexes(segment: list[ShellToken]) -> list[int]:
    starts = [0]
    starts.extend(index + 1 for index, token in enumerate(segment[:-1]) if token.value == "=")
    indexes: list[int] = []
    for start in starts:
        while start < len(segment) and segment[start].value == "&":
            start += 1
        if start < len(segment) and normalized_shell_command(segment[start]) in {"command", "sudo"}:
            start += 1
        if (
            start < len(segment)
            and not segment[start].quoted
            and normalized_shell_command(segment[start]) in SHELL_MUTATING_COMMANDS
        ):
            indexes.append(start)
    return indexes


def likely_mutating_shell(command: str) -> bool:
    tokens = shell_tokens(command)
    if shell_redirection_targets(tokens):
        return True
    return any(mutating_command_indexes(segment) for segment in shell_command_segments(tokens))


def shell_argument_value(
    tokens: list[ShellToken], index: int, variables: dict[str, str]
) -> tuple[str | None, int]:
    if index >= len(tokens):
        return None, index
    token = tokens[index]
    cleaned = token.value.strip("(),")
    if normalized_shell_command(token) == "join-path" and index + 2 < len(tokens):
        base, base_end = shell_argument_value(tokens, index + 1, variables)
        child, child_end = shell_argument_value(tokens, base_end + 1, variables)
        if base is not None and child is not None:
            return str(Path(base) / child), child_end
        return None, child_end
    if cleaned.startswith("$"):
        return variables.get(cleaned.casefold()), index
    if not cleaned or (token.quoted and "$" in cleaned):
        return None, index
    return cleaned, index


def shell_variable_values(tokens: list[ShellToken]) -> dict[str, str]:
    variables: dict[str, str] = {}
    for segment in shell_command_segments(tokens):
        if len(segment) < 3 or not segment[0].value.startswith("$") or segment[1].value != "=":
            continue
        resolved, _ = shell_argument_value(segment, 2, variables)
        if resolved is not None:
            variables[segment[0].value.casefold()] = resolved
    return variables


def named_shell_arguments(
    segment: list[ShellToken], flags: set[str], variables: dict[str, str]
) -> list[str]:
    values: list[str] = []
    index = 0
    while index < len(segment) - 1:
        if segment[index].value.casefold() in flags:
            resolved, end = shell_argument_value(segment, index + 1, variables)
            if resolved is not None:
                values.append(resolved)
            index = max(index + 1, end)
        index += 1
    return values


def positional_shell_arguments(
    segment: list[ShellToken], command_index: int, variables: dict[str, str]
) -> list[str]:
    values: list[str] = []
    for token in segment[command_index + 1 :]:
        if token.operator or token.value.startswith("-"):
            continue
        resolved, _ = shell_argument_value([token], 0, variables)
        if resolved is not None:
            values.append(resolved)
    return values


def mutating_segment_paths(
    segment: list[ShellToken], variables: dict[str, str]
) -> list[str]:
    values: list[str] = []
    for command_index in mutating_command_indexes(segment):
        token = segment[command_index]
        command = normalized_shell_command(token)
        tail = segment[command_index + 1 :]
        path_flags = {"-path", "-literalpath", "-filepath"}
        destination_flags = {"-destination"}
        if command in {"set-content", "add-content", "clear-content", "out-file"}:
            values.extend(named_shell_arguments(tail, path_flags, variables))
        elif command == "new-item":
            item_types = named_shell_arguments(tail, {"-itemtype", "-type"}, variables)
            if not item_types or item_types[-1].casefold() not in {
                "directory",
                "junction",
                "symboliclink",
            }:
                values.extend(named_shell_arguments(tail, path_flags, variables))
        elif command in {"remove-item"}:
            values.extend(named_shell_arguments(tail, path_flags, variables))
        elif command == "copy-item":
            values.extend(named_shell_arguments(tail, destination_flags, variables))
        elif command == "move-item":
            values.extend(named_shell_arguments(tail, path_flags | destination_flags, variables))
        elif command == "rename-item":
            sources = named_shell_arguments(tail, path_flags, variables)
            values.extend(sources)
            new_names = named_shell_arguments(tail, {"-newname"}, variables)
            if sources and new_names:
                new_path = Path(new_names[-1])
                values.append(
                    str(new_path if new_path.is_absolute() else Path(sources[-1]).parent / new_path)
                )
        else:
            positional = positional_shell_arguments(segment, command_index, variables)
            if command in {"cp"} and positional:
                values.append(positional[-1])
            elif command in {"mv"}:
                values.extend(positional)
            elif command in {"rm", "touch", "tee"}:
                values.extend(positional)
            elif command == "sed" and "-i" in {item.value.casefold() for item in tail} and positional:
                values.append(positional[-1])
            elif command == "clang-format" and "-i" in {item.value.casefold() for item in tail}:
                values.extend(positional)
            elif command == "prettier" and "--write" in {item.value.casefold() for item in tail}:
                values.extend(positional)
    return values


def extract_shell_paths(ctx: AuditContext, command: str, cwd: Path) -> list[Path]:
    tokens = shell_tokens(command)
    variables = shell_variable_values(tokens)
    values: list[str] = []
    for segment in shell_command_segments(tokens):
        values.extend(mutating_segment_paths(segment, variables))
    for token in shell_redirection_targets(tokens):
        resolved, _ = shell_argument_value([token], 0, variables)
        if resolved is not None:
            values.append(resolved)

    paths: list[Path] = []
    for value in values:
        path = resolve_candidate_path(ctx, value, cwd)
        if path is not None and path not in paths:
            paths.append(path)
    return paths


def walk_path_values(value: Any, key: str = "") -> Iterable[str]:
    if isinstance(value, dict):
        for child_key, child in value.items():
            if child_key.casefold() in {
                "file",
                "file_path",
                "filepath",
                "path",
                "target",
                "destination",
                "source",
            } and isinstance(child, str):
                yield child
            yield from walk_path_values(child, child_key)
    elif isinstance(value, list):
        for child in value:
            yield from walk_path_values(child, key)


def extract_tool_paths(ctx: AuditContext, event: dict[str, Any]) -> list[Path]:
    tool_name = str(event.get("tool_name") or "")
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    cwd = Path(str(tool_input.get("workdir") or event.get("cwd") or ctx.project_root)).resolve()
    paths: list[Path] = []
    if tool_name == "apply_patch":
        patch_text = str(
            tool_input.get("patch") or tool_input.get("input") or tool_input.get("command") or ""
        )
        paths.extend(parse_apply_patch_paths(patch_text, cwd, ctx.project_root))
    for value in walk_path_values(tool_input):
        path = resolve_candidate_path(ctx, value, cwd)
        if path is not None and path not in paths:
            paths.append(path)
    if tool_name in {"Bash", "shell_command"}:
        command = str(tool_input.get("command") or tool_input.get("cmd") or "")
        if likely_mutating_shell(command):
            for path in extract_shell_paths(ctx, command, cwd):
                if path not in paths:
                    paths.append(path)
    return [path for path in paths if not is_ignored_path(ctx, path)]


def is_probably_binary(data: bytes) -> bool:
    if b"\0" in data[:8192]:
        return True
    sample = data[:8192]
    if not sample:
        return False
    controls = sum(byte < 9 or (13 < byte < 32) for byte in sample)
    return controls / len(sample) > 0.08


def snapshot_metadata(path: Path, capture_path: Path | None = None) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {
            "exists": False,
            "size": 0,
            "sha256": None,
            "binary": False,
            "captured": True,
            "snapshotPath": None,
        }
    size = path.stat().st_size
    if size <= MAX_CAPTURE_BYTES:
        data = path.read_bytes()
        binary = is_probably_binary(data)
        if capture_path is not None:
            capture_path.parent.mkdir(parents=True, exist_ok=True)
            capture_path.write_bytes(data)
        return {
            "exists": True,
            "size": len(data),
            "sha256": sha256_bytes(data),
            "binary": binary,
            "captured": True,
            "snapshotPath": str(capture_path) if capture_path is not None else None,
        }
    return {
        "exists": True,
        "size": size,
        "sha256": sha256_file(path),
        "binary": True,
        "captured": False,
        "snapshotPath": None,
    }


def snapshot_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return bool(before.get("exists") != after.get("exists") or before.get("sha256") != after.get("sha256"))


def record_before_path(ctx: AuditContext, turn_id: str, path: Path) -> dict[str, Any]:
    relative = relative_path(ctx, path)
    manifest_path = before_manifest_path(ctx, turn_id)
    manifest = read_json_if_exists(manifest_path, {"files": {}})
    files = manifest.setdefault("files", {})
    if relative in files:
        return files[relative]
    blob_name = hashlib.sha256(relative.encode("utf-8")).hexdigest() + ".bin"
    snapshot_path = turn_root(ctx, turn_id) / "before-blobs" / blob_name
    metadata = snapshot_metadata(path, snapshot_path)
    metadata["path"] = relative
    metadata["recordedAt"] = utc_now()
    files[relative] = metadata
    atomic_write_json(manifest_path, manifest)
    return metadata


def ownership_path(ctx: AuditContext) -> Path:
    return ctx.state_root / "ownership.json"


def legacy_session_root(ctx: AuditContext, session_id: str) -> Path:
    return ctx.state_root / "sessions" / bridge.sanitize_component(session_id, 80)


def normalize_ownership_registry(
    ctx: AuditContext, registry: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    if int(registry.get("schemaVersion") or 0) >= OWNERSHIP_SCHEMA_VERSION:
        return registry, False
    records = registry.setdefault("files", {})
    for relative in list(records):
        current = records.get(relative)
        if not isinstance(current, dict):
            del records[relative]
            continue
        session_id = str(current.get("sessionId") or "")
        session_root = legacy_session_root(ctx, session_id)
        legacy_session = read_json_if_exists(session_root / "session.json", {})
        turn_id = str(legacy_session.get("activeTurnId") or "")
        before = read_json_if_exists(session_root / "turns" / turn_id / "before.json", {"files": {}})
        before_file = before.get("files", {}).get(relative)
        project_file = ctx.project_root / Path(relative)
        try:
            project_file.resolve().relative_to(ctx.project_root.resolve())
            inside_project = True
        except (OSError, ValueError):
            inside_project = False
        material_path = inside_project and (
            project_file.is_file()
            or (isinstance(before_file, dict) and before_file.get("exists") is True)
        )
        if not turn_id or not material_path:
            del records[relative]
            continue
        current["turnId"] = turn_id
        current["source"] = "legacy-v5"
    registry["schemaVersion"] = OWNERSHIP_SCHEMA_VERSION
    registry["updatedAt"] = utc_now()
    return registry, True


def read_ownership_registry(ctx: AuditContext) -> tuple[dict[str, Any], bool]:
    value = read_json_if_exists(ownership_path(ctx), {"files": {}})
    if not isinstance(value, dict):
        value = {"files": {}}
    return normalize_ownership_registry(ctx, value)


def refresh_owned_files(
    session: dict[str, Any], records: dict[str, Any], session_id: str
) -> None:
    session["ownedFiles"] = sorted(
        relative
        for relative, current in records.items()
        if isinstance(current, dict) and current.get("sessionId") == session_id
    )


def claim_paths(
    ctx: AuditContext, session: dict[str, Any], turn_id: str, paths: list[Path]
) -> None:
    registry, _ = read_ownership_registry(ctx)
    records = registry.setdefault("files", {})
    now_epoch = time.time()
    for path in paths:
        relative = relative_path(ctx, path)
        current = records.get(relative)
        if isinstance(current, dict):
            age = now_epoch - float(current.get("updatedEpoch") or 0)
            current_session = current.get("sessionId")
            current_turn = current.get("turnId")
            if age < LOCK_STALE_SECONDS and current_session != ctx.session_id:
                raise OwnershipConflict(
                    f"文件正由另一个 Codex 会话记录：{relative} "
                    f"(session {current_session}, turn {current_turn or '-'})"
                )
            if (
                age < LOCK_STALE_SECONDS
                and current_session == ctx.session_id
                and current_turn
                and current_turn != turn_id
            ):
                raise OwnershipConflict(
                    f"文件仍由本 Codex 会话的上一回合记录：{relative} "
                    f"(turn {current_turn})"
                )
        records[relative] = {
            "sessionId": ctx.session_id,
            "turnId": turn_id,
            "branch": ctx.branch,
            "source": "tool-hook",
            "updatedAt": utc_now(),
            "updatedEpoch": now_epoch,
        }
    refresh_owned_files(session, records, ctx.session_id)
    registry["schemaVersion"] = OWNERSHIP_SCHEMA_VERSION
    registry["updatedAt"] = utc_now()
    atomic_write_json(ownership_path(ctx), registry)


def release_turn_paths(ctx: AuditContext, session: dict[str, Any], turn_id: str) -> None:
    registry, _ = read_ownership_registry(ctx)
    records = registry.setdefault("files", {})
    for relative in list(records):
        current = records.get(relative)
        if (
            isinstance(current, dict)
            and current.get("sessionId") == ctx.session_id
            and current.get("turnId") == turn_id
        ):
            del records[relative]
    refresh_owned_files(session, records, ctx.session_id)
    registry["updatedAt"] = utc_now()
    atomic_write_json(ownership_path(ctx), registry)


def release_paths(ctx: AuditContext, session: dict[str, Any] | None = None) -> None:
    registry, _ = read_ownership_registry(ctx)
    records = registry.setdefault("files", {})
    for relative in list(records):
        current = records.get(relative)
        if isinstance(current, dict) and current.get("sessionId") == ctx.session_id:
            del records[relative]
    if session is not None:
        refresh_owned_files(session, records, ctx.session_id)
    registry["updatedAt"] = utc_now()
    atomic_write_json(ownership_path(ctx), registry)


def current_history_head(ctx: AuditContext) -> str:
    branch = bridge.git_output(
        ctx.git_exe,
        ctx.history_root,
        ["symbolic-ref", "--short", "HEAD"],
        check=False,
    )
    if branch != ctx.branch:
        raise AuditError(
            f"本地历史分支被切换为 {branch or '<detached>'}；请切回 {ctx.branch} 后重试。"
        )
    return bridge.git_output(ctx.git_exe, ctx.history_root, ["rev-parse", "HEAD"])


def git_blob_at(ctx: AuditContext, commit: str, path: str) -> bytes | None:
    completed = bridge.run_command(
        [ctx.git_exe, "-C", str(ctx.history_root), "show", f"{commit}:{path}"], check=False
    )
    return completed.stdout if completed.returncode == 0 else None


def decode_text(data: bytes) -> tuple[str, str] | None:
    if is_probably_binary(data):
        return None
    for encoding in ("utf-8-sig", "utf-8", "utf-16"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return None


def build_file_diff(relative: str, before_data: bytes | None, after_data: bytes | None) -> str:
    before_decoded = decode_text(before_data or b"") if before_data is not None else ("", "utf-8")
    after_decoded = decode_text(after_data or b"") if after_data is not None else ("", "utf-8")
    if before_decoded is None or after_decoded is None:
        return ""
    before_lines = before_decoded[0].splitlines(keepends=True)
    after_lines = after_decoded[0].splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"a/{relative}" if before_data is not None else "/dev/null",
            tofile=f"b/{relative}" if after_data is not None else "/dev/null",
        )
    )


def after_snapshot(path: Path) -> tuple[dict[str, Any], bytes | None]:
    metadata = snapshot_metadata(path)
    if metadata.get("exists") and metadata.get("captured"):
        data = path.read_bytes()
        if sha256_bytes(data) != metadata.get("sha256"):
            raise AuditError(f"文件在回合收尾期间仍在变化：{path}")
        return metadata, data
    return metadata, None


def action_for(before: dict[str, Any], after: dict[str, Any]) -> str:
    if not before.get("exists") and after.get("exists"):
        return "add"
    if before.get("exists") and not after.get("exists"):
        return "delete"
    return "edit"


def commit_tree(
    ctx: AuditContext,
    parent: str,
    updates: dict[str, bytes | None],
    message: str,
) -> tuple[str, dict[str, str | None]]:
    ctx.session_root.mkdir(parents=True, exist_ok=True)
    handle, index_name = tempfile.mkstemp(prefix="git-index-", dir=str(ctx.session_root))
    os.close(handle)
    os.unlink(index_name)
    env = {
        **os.environ,
        "GIT_INDEX_FILE": index_name,
        "GIT_AUTHOR_NAME": "Codex P4 Diff",
        "GIT_AUTHOR_EMAIL": "codex-p4-diff@local.invalid",
        "GIT_COMMITTER_NAME": "Codex P4 Diff",
        "GIT_COMMITTER_EMAIL": "codex-p4-diff@local.invalid",
    }
    try:
        bridge.run_command(
            [ctx.git_exe, "-C", str(ctx.history_root), "read-tree", parent], env=env
        )
        index_entries: dict[str, str | None] = {}
        for path, data in sorted(updates.items()):
            if data is None:
                bridge.run_command(
                    [
                        ctx.git_exe,
                        "-C",
                        str(ctx.history_root),
                        "update-index",
                        "--force-remove",
                        "--",
                        path,
                    ],
                    env=env,
                    check=False,
                )
                index_entries[path] = None
                continue
            blob = bridge.run_command(
                [ctx.git_exe, "-C", str(ctx.history_root), "hash-object", "-w", "--stdin"],
                input_bytes=data,
            ).stdout.decode("ascii").strip()
            bridge.run_command(
                [
                    ctx.git_exe,
                    "-C",
                    str(ctx.history_root),
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    "100644",
                    blob,
                    path,
                ],
                env=env,
            )
            index_entries[path] = blob
        tree = bridge.run_command(
            [ctx.git_exe, "-C", str(ctx.history_root), "write-tree"], env=env
        ).stdout.decode("ascii").strip()
        commit = bridge.run_command(
            [ctx.git_exe, "-C", str(ctx.history_root), "commit-tree", tree, "-p", parent],
            input_bytes=message.encode("utf-8"),
            env=env,
        ).stdout.decode("ascii").strip()
        return commit, index_entries
    finally:
        try:
            os.unlink(index_name)
        except FileNotFoundError:
            pass


def ensure_turn_paths_not_staged(ctx: AuditContext, parent: str, paths: list[str]) -> None:
    completed = bridge.run_command(
        [
            ctx.git_exe,
            "-C",
            str(ctx.history_root),
            "diff",
            "--cached",
            "--quiet",
            parent,
            "--",
            *paths,
        ],
        check=False,
    )
    if completed.returncode == 1:
        raise AuditError(
            "本回合目标路径包含用户或其他工具已暂存的 Git 改动；"
            "为避免覆盖暂存状态，本地回合 commit 已停止。"
        )
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise AuditError(f"无法核对本回合 Git 暂存状态：{message}")


def sync_main_index(ctx: AuditContext, entries: dict[str, str | None]) -> None:
    for path, blob in sorted(entries.items()):
        if blob is None:
            bridge.run_command(
                [
                    ctx.git_exe,
                    "-C",
                    str(ctx.history_root),
                    "update-index",
                    "--force-remove",
                    "--",
                    path,
                ],
                check=False,
            )
            continue
        bridge.run_command(
            [
                ctx.git_exe,
                "-C",
                str(ctx.history_root),
                "update-index",
                "--add",
                "--cacheinfo",
                "100644",
                blob,
                path,
            ]
        )


def attach_audit_note(ctx: AuditContext, commit: str, record: dict[str, Any]) -> None:
    payload = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    bridge.run_command(
        [
            ctx.git_exe,
            "-C",
            str(ctx.history_root),
            "notes",
            "--ref=codex-p4-diff",
            "add",
            "-f",
            "-F",
            "-",
            commit,
        ],
        input_bytes=payload,
    )


def entries_at_commit(ctx: AuditContext, commit: str, paths: Iterable[str]) -> dict[str, str | None]:
    entries: dict[str, str | None] = {}
    for path in paths:
        completed = bridge.run_command(
            [ctx.git_exe, "-C", str(ctx.history_root), "rev-parse", f"{commit}:{path}"],
            check=False,
        )
        entries[path] = (
            completed.stdout.decode("ascii", errors="replace").strip()
            if completed.returncode == 0
            else None
        )
    return entries


def create_turn_commit(
    ctx: AuditContext,
    session: dict[str, Any],
    turn_id: str,
    prompt: dict[str, Any],
    changes: list[dict[str, Any]],
    patches: list[str],
    stop_event: dict[str, Any],
) -> tuple[str, str, dict[str, str | None]]:
    parent = current_history_head(ctx)
    sequence = int(session.get("nextSequence") or 1)
    updates: dict[str, bytes | None] = {}
    turn_paths = sorted(str(change["path"]) for change in changes)
    ensure_turn_paths_not_staged(ctx, parent, turn_paths)

    for change in changes:
        relative = str(change["path"])
        before = change["before"]
        after = change["after"]
        before_data = change.pop("_beforeData", None)
        after_data = change.pop("_afterData", None)
        parent_data = git_blob_at(ctx, parent, relative)
        history_state = "receipt-only"
        if after.get("captured") and after_data is not None:
            if parent_data is None:
                updates[relative] = after_data
                history_state = "first-touch"
            elif before.get("captured") and before_data is not None and parent_data == before_data:
                updates[relative] = after_data
                history_state = "exact"
            else:
                history_state = "drifted-receipt-only"
        elif not after.get("exists"):
            if parent_data is not None and before.get("captured") and parent_data == before_data:
                updates[relative] = None
                history_state = "exact-delete"
            else:
                history_state = "delete-receipt-only"
        change["historyState"] = history_state

    change_summary, change_summary_source, assistant_message_sha256 = build_change_summary(
        stop_event, changes
    )
    summary_record = {
        'changeSummary': change_summary,
        'changeSummarySource': change_summary_source,
        'assistantMessageSha256': assistant_message_sha256,
    }
    event_record = {
        **summary_record,
        "schemaVersion": bridge.SCHEMA_VERSION,
        "generatedBy": bridge.TOOL_NAME,
        "sessionId": ctx.session_id,
        "turnId": turn_id,
        "sessionSequence": sequence,
        "branch": ctx.branch,
        "projectRoot": str(ctx.project_root),
        "promptSummary": prompt.get("summary"),
        "promptSha256": prompt.get("sha256"),
        "recordedAt": utc_now(),
        "p4Mode": "read-only",
        "files": changes,
        "textPatch": "\n".join(patches) if patches else None,
    }

    subject = f"[Codex] {change_summary}"[:120]
    body = [
        subject,
        "",
        f"Change: {change_summary}",
        f"Change-Summary-Source: {change_summary_source}",
        f"Request: {prompt.get('summary') or 'Codex file changes'}",
        f"Session: {ctx.session_id}",
        f"Turn: {turn_id}",
        f"Recorded-At: {event_record['recordedAt']}",
        f"Prompt-Recorded-At: {prompt.get('recordedAt') or '-'}",
        f"Project: {ctx.project_root}",
        "P4 mode: read-only",
        "Files:",
    ]
    for item in changes:
        before = item["before"]
        after = item["after"]
        p4 = item.get("p4") or {}
        body.append(
            f"- {item['action']} {item['path']} "
            f"before={before.get('sha256') or '-'}:{before.get('size')} "
            f"after={after.get('sha256') or '-'}:{after.get('size')} "
            f"history={item['historyState']} "
            f"p4={p4.get('depotFile') or '-'}@{p4.get('haveRev') or '-'}/{p4.get('headRev') or '-'}"
        )
    body.extend(["", "Audit-Notes-Ref: refs/notes/codex-p4-diff", ""])
    commit, entries = commit_tree(ctx, parent, updates, "\n".join(body))
    attach_audit_note(ctx, commit, event_record)
    parent_entries = entries_at_commit(ctx, parent, entries)
    try:
        sync_main_index(ctx, entries)
        bridge.run_command(
            [
                ctx.git_exe,
                "-C",
                str(ctx.history_root),
                "update-ref",
                f"refs/heads/{ctx.branch}",
                commit,
                parent,
            ]
        )
    except (bridge.CodexP4DiffError, OSError):
        sync_main_index(ctx, parent_entries)
        raise
    return commit, "refs/notes/codex-p4-diff", summary_record


def record_prompt(ctx: AuditContext, event: dict[str, Any]) -> str:
    with project_lock(ctx.state_root):
        session = load_session(ctx)
        turn_id = normalize_turn_id(ctx, event, session)
        prompt = str(event.get("prompt") or event.get("user_prompt") or "")
        record = {
            "turnId": turn_id,
            "summary": redact_summary(prompt),
            "sha256": sha256_bytes(prompt.encode("utf-8", errors="surrogatepass")),
            "recordedAt": utc_now(),
        }
        atomic_write_json(prompt_record_path(ctx, turn_id), record)
        session["activeTurnId"] = turn_id
        turns = session.setdefault("turns", {})
        turns.setdefault(turn_id, {"status": "active", "startedAt": utc_now()})
        session["status"] = "active"
        save_session(ctx, session)
    return turn_id


def prepare_tool(ctx: AuditContext, event: dict[str, Any]) -> int:
    tool_name = str(event.get("tool_name") or "")
    if tool_name not in MUTATING_TOOLS:
        return 0
    paths = extract_tool_paths(ctx, event)
    if not paths:
        return 0
    with project_lock(ctx.state_root):
        session = load_session(ctx)
        turn_id = normalize_turn_id(ctx, event, session)
        claim_paths(ctx, session, turn_id, paths)
        for path in paths:
            record_before_path(ctx, turn_id, path)
        save_session(ctx, session)
    return len(paths)


def finish_tool(ctx: AuditContext, event: dict[str, Any]) -> int:
    paths = extract_tool_paths(ctx, event)
    if not paths:
        return 0
    with project_lock(ctx.state_root):
        session = load_session(ctx)
        turn_id = normalize_turn_id(ctx, event, session)
        manifest = read_json_if_exists(before_manifest_path(ctx, turn_id), {"files": {}})
        known = manifest.setdefault("files", {})
        claim_paths(ctx, session, turn_id, paths)
        for path in paths:
            relative = relative_path(ctx, path)
            if relative not in known:
                known[relative] = {
                    "path": relative,
                    "exists": None,
                    "size": None,
                    "sha256": None,
                    "binary": None,
                    "captured": False,
                    "snapshotPath": None,
                    "recordedAt": utc_now(),
                    "warning": "path discovered after tool execution; exact before content unavailable",
                }
        atomic_write_json(before_manifest_path(ctx, turn_id), manifest)
        save_session(ctx, session)
    return len(paths)


def finalize_turn(ctx: AuditContext, event: dict[str, Any]) -> dict[str, Any]:
    with project_lock(ctx.state_root):
        session = load_session(ctx)
        turn_id = normalize_turn_id(ctx, event, session)
        turn_state = session.setdefault("turns", {}).setdefault(turn_id, {})
        if turn_state.get("status") == "committed":
            release_turn_paths(ctx, session, turn_id)
            save_session(ctx, session)
            return {
                "status": "already-committed",
                "commit": turn_state.get("commit"),
                "branch": ctx.branch,
            }
        prompt = read_json_if_exists(
            prompt_record_path(ctx, turn_id),
            {"summary": "Codex file changes", "sha256": None},
        )
        manifest = read_json_if_exists(before_manifest_path(ctx, turn_id), {"files": {}})
        changes: list[dict[str, Any]] = []
        patches: list[str] = []
        for relative, before in sorted(manifest.get("files", {}).items()):
            path = ctx.project_root / Path(relative)
            after, after_data = after_snapshot(path)
            if before.get("exists") is None:
                changed = bool(after.get("exists"))
            else:
                changed = snapshot_changed(before, after)
            if not changed:
                continue
            before_data: bytes | None = None
            snapshot_path = before.get("snapshotPath")
            if before.get("captured") and before.get("exists") and snapshot_path:
                candidate = Path(str(snapshot_path))
                if candidate.is_file():
                    before_data = candidate.read_bytes()
            patch = build_file_diff(
                relative,
                before_data if before.get("exists") else None,
                after_data if after.get("exists") else None,
            )
            if patch:
                patches.append(patch)
            p4_record = bridge.readonly_fstat(ctx.p4, path)
            change = {
                "path": relative,
                "action": action_for(before, after),
                "before": {
                    key: before.get(key)
                    for key in ("exists", "size", "sha256", "binary", "captured")
                },
                "after": {
                    key: after.get(key)
                    for key in ("exists", "size", "sha256", "binary", "captured")
                },
                "p4": p4_record,
                "textPatch": bool(patch),
                "_beforeData": before_data,
                "_afterData": after_data,
            }
            changes.append(change)

        if not changes:
            turn_state.update({"status": "no-changes", "finishedAt": utc_now()})
            session["activeTurnId"] = None
            release_turn_paths(ctx, session, turn_id)
            save_session(ctx, session)
            return {"status": "no-changes", "branch": ctx.branch, "historyRoot": str(ctx.history_root)}

        try:
            commit, audit_path, summary_record = create_turn_commit(
                ctx, session, turn_id, prompt, changes, patches, event
            )
        except (bridge.CodexP4DiffError, OSError) as exc:
            turn_state.update({"status": "commit-failed", "error": str(exc), "finishedAt": utc_now()})
            save_session(ctx, session)
            raise AuditError(
                f"本地 Git 回合提交失败；P4 工作区文件保持原样，可重试收尾：{exc}"
            ) from exc

        turn_state.update(
            {
                "status": "committed",
                "commit": commit,
                "auditNotesRef": audit_path,
                "fileCount": len(changes),
                "summary": summary_record["changeSummary"],
                "summarySource": summary_record["changeSummarySource"],
                "requestSummary": prompt.get("summary"),
                "finishedAt": utc_now(),
            }
        )
        session["nextSequence"] = int(session.get("nextSequence") or 1) + 1
        session["activeTurnId"] = None
        release_turn_paths(ctx, session, turn_id)
        save_session(ctx, session)
        return {
            "status": "committed",
            "commit": commit,
            "branch": ctx.branch,
            "files": len(changes),
            "summary": summary_record["changeSummary"],
            "summarySource": summary_record["changeSummarySource"],
            "historyRoot": str(ctx.history_root),
        }


def mark_session(ctx: AuditContext, status: str) -> None:
    with project_lock(ctx.state_root):
        session = load_session(ctx)
        registry, migrated = read_ownership_registry(ctx)
        if migrated:
            atomic_write_json(ownership_path(ctx), registry)
        session["status"] = status
        if status == "ended":
            session["endedAt"] = utc_now()
            release_paths(ctx, session)
        save_session(ctx, session)


def additional_context(text: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"additionalContext": text}}


def deny_pretool(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def post_feedback(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": reason,
        }
    }


def handle_event(event: dict[str, Any]) -> dict[str, Any] | None:
    event_name = str(event.get("hook_event_name") or "")
    ctx = resolve_context(event)
    if event_name == "SessionStart":
        mark_session(ctx, "active")
        return additional_context(
            f"Codex 本地历史已启用：{ctx.branch}；P4 仅做只读查询。历史仓库：{ctx.history_root}"
        )
    if event_name == "UserPromptSubmit":
        turn_id = record_prompt(ctx, event)
        return additional_context(f"本回合将在有文件变化时自动提交本地 Git：{turn_id}")
    if event_name == "PreToolUse":
        count = prepare_tool(ctx, event)
        return additional_context(f"已为本回合记录 {count} 个文件的写入前状态") if count else None
    if event_name == "PostToolUse":
        finish_tool(ctx, event)
        return None
    if event_name == "Stop":
        result = finalize_turn(ctx, event)
        if result["status"] == "committed":
            return additional_context(
                f"本回合已提交本地 Git {result['commit'][:12]} 到 {result['branch']}，"
                f"共 {result['files']} 个文件；P4 未被修改。"
            )
        return additional_context("本回合没有文件变化，因此未创建空 Git commit。")
    if event_name == "SessionEnd":
        mark_session(ctx, "ended")
        return None
    return None


def hook_main() -> int:
    raw = sys.stdin.buffer.read()
    try:
        event = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        result = handle_event(event)
        if result is not None:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    except HookSkip:
        return 0
    except OwnershipConflict as exc:
        event_name = ""
        try:
            event_name = str(json.loads(raw.decode("utf-8")).get("hook_event_name") or "")
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        result = deny_pretool(str(exc)) if event_name == "PreToolUse" else additional_context(str(exc))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (AuditError, bridge.CodexP4DiffError, OSError, json.JSONDecodeError) as exc:
        event_name = ""
        try:
            event_name = str(json.loads(raw.decode("utf-8")).get("hook_event_name") or "")
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        if event_name == "PreToolUse":
            result = deny_pretool(f"codex-p4-diff 无法保证本回合审计完整性：{exc}")
        elif event_name == "PostToolUse":
            result = post_feedback(f"codex-p4-diff 审计警告：{exc}")
        else:
            result = additional_context(f"codex-p4-diff 审计警告：{exc}")
        print(json.dumps(result, ensure_ascii=False))
        return 0


if __name__ == "__main__":
    raise SystemExit(hook_main())
