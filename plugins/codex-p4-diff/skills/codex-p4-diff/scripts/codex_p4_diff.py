#!/usr/bin/env python3
"""把 P4 #have 基线接入当前工程的原生 Git 审阅。"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import locale
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from typing import Any, Iterable
from urllib.parse import quote


TOOL_NAME = "codex-p4-diff"
MARKER_NAME = "codex-p4-diff.json"
SCHEMA_VERSION = 2
AUDIT_SCHEMA_VERSION = 1
BASELINE_REF = "refs/heads/codex-p4-have"
REPOSITORY_URL = "https://github.com/BillyCSZhao/codex-p4-diff"
TROUBLESHOOTING_URL = f"{REPOSITORY_URL}#troubleshooting"
DELETE_ACTIONS = {"delete", "move/delete"}
NO_BASE_ACTIONS = {"add", "move/add"}
TEXT_FILE_TYPES = {"text", "unicode", "utf8", "utf8-bom", "utf16", "symlink"}


class CodexP4DiffError(RuntimeError):
    pass


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def windows_tool_candidates(tool: str) -> list[Path]:
    if os.name != "nt":
        return []
    program_roots: list[Path] = []
    for variable in ("ProgramW6432", "PROGRAMFILES", "PROGRAMFILES(X86"):
        value = os.environ.get(variable)
        if value:
            candidate = Path(value)
            if candidate not in program_roots:
                program_roots.append(candidate)
    candidates: list[Path] = []
    if tool == "p4":
        candidates.extend(root / "Perforce" / "p4.exe" for root in program_roots)
    elif tool == "p4vc":
        for root in program_roots:
            candidates.extend((root / "Perforce" / "p4vc.bat", root / "Perforce" / "p4v.exe"))
    elif tool == "git":
        for root in program_roots:
            candidates.extend((root / "Git" / "cmd" / "git.exe", root / "Git" / "bin" / "git.exe"))
        candidates.append(
            Path.home()
            / ".cache"
            / "codex-runtimes"
            / "codex-primary-runtime"
            / "dependencies"
            / "native"
            / "git"
            / "cmd"
            / "git.exe"
        )
    return candidates


def resolve_executable(requested: str | None, tool: str, install_hint: str) -> str:
    value = requested or tool
    direct = Path(value).expanduser()
    if direct.is_file():
        return str(direct.resolve())
    located = shutil.which(value)
    if located:
        return str(Path(located).resolve())
    if requested is None and tool == "p4vc":
        p4v = shutil.which("p4v")
        if p4v:
            return str(Path(p4v).resolve())
    if requested is None:
        for candidate in windows_tool_candidates(tool):
            if candidate.is_file():
                return str(candidate.resolve())
    raise CodexP4DiffError(
        f"找不到 {tool} 可执行文件（请求值：{value!r}）。{install_hint}"
        f"安装后重新打开终端/Codex，或使用 --{tool} <完整路径>。"
    )


def decode_output(raw: bytes) -> str:
    encodings = ["utf-8-sig", locale.getpreferredencoding(False)]
    if os.name == "nt":
        encodings.append("mbcs")
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def run_command(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    allowed_codes: Iterable[int] = (0,),
) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CodexP4DiffError(f"找不到命令：{argv[0]}。请安装该工具或传入完整路径。") from exc
    except PermissionError as exc:
        raise CodexP4DiffError(f"没有权限执行命令：{argv[0]}。请检查文件权限或安全软件策略。") from exc
    except OSError as exc:
        raise CodexP4DiffError(f"无法启动命令 {argv[0]}：{exc}") from exc
    if completed.returncode not in set(allowed_codes):
        rendered = subprocess.list2cmdline(argv)
        stdout = decode_output(completed.stdout).strip()
        stderr = decode_output(completed.stderr).strip()
        detail = "\n".join(part for part in (stdout, stderr) if part)
        raise CodexP4DiffError(
            f"命令执行失败，退出码为 {completed.returncode}: {rendered}"
            + (f"\n{detail}" if detail else "")
        )
    return completed


def parse_ztag(raw: bytes, record_key: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in decode_output(raw).splitlines():
        if not line.startswith("... "):
            continue
        key_value = line[4:]
        key, separator, value = key_value.partition(" ")
        if not separator:
            value = ""
        if key == record_key and key in current:
            records.append(current)
            current = {}
        if key in current:
            suffix = 2
            candidate = f"{key}_{suffix}"
            while candidate in current:
                suffix += 1
                candidate = f"{key}_{suffix}"
            key = candidate
        current[key] = value
    if current:
        records.append(current)
    return records


def p4_tagged(
    p4_exe: str,
    command: list[str],
    *,
    client: str | None = None,
    record_key: str,
    allow_empty_error: bool = False,
    cwd: Path | None = None,
) -> list[dict[str, str]]:
    argv = [p4_exe]
    if client:
        argv.extend(["-c", client])
    argv.extend(["-ztag", *command])
    completed = run_command(argv, cwd=cwd, allowed_codes=(0, 1) if allow_empty_error else (0,))
    records = parse_ztag(completed.stdout, record_key)
    if completed.returncode != 0:
        combined = (decode_output(completed.stdout) + "\n" + decode_output(completed.stderr)).lower()
        if allow_empty_error and not any(record.get("depotFile") for record in records):
            if "not opened" in combined or "no such file" in combined or "file(s) not opened" in combined:
                return []
        raise CodexP4DiffError(combined.strip() or "Perforce 命令执行失败")
    return records


def canonical(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def is_inside(path: Path, parent: Path) -> bool:
    path_text = canonical(path)
    parent_text = canonical(parent)
    try:
        return os.path.commonpath([path_text, parent_text]) == parent_text
    except ValueError:
        return False


def require_inside(path: Path, parent: Path, label: str) -> None:
    if not is_inside(path, parent):
        raise CodexP4DiffError(f"{label} 超出预期根目录：{path}（根目录：{parent}）")


def sanitize_component(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return sanitized[:80] or "unknown"


def p4_escape_filespec_path(path: Path | str) -> str:
    value = str(path)
    for source, replacement in (("%", "%25"), ("@", "%40"), ("#", "%23"), ("*", "%2A")):
        value = value.replace(source, replacement)
    return value


def info_record(p4_exe: str, client: str | None = None, cwd: Path | None = None) -> dict[str, str]:
    records = p4_tagged(p4_exe, ["info"], client=client, record_key="userName", cwd=cwd)
    if not records:
        raise CodexP4DiffError("p4 info 未返回标签数据")
    return records[0]


@contextmanager
def temporary_environment(overrides: dict[str, str]):
    previous = {key: os.environ.get(key) for key in overrides}
    try:
        for key, value in overrides.items():
            os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def load_p4v_connections(connection_map: Path | None = None) -> tuple[Path, list[dict[str, str]]]:
    path = (connection_map or (Path.home() / ".p4qt" / "connectionmap.xml")).expanduser().resolve()
    if not path.is_file():
        return path, []
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise CodexP4DiffError(f"无法读取 P4V 连接映射 {path}：{exc}") from exc

    connections: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entry in root.findall(".//ConnectionMap"):
        port = (entry.findtext("P4Port") or "").strip()
        user = (entry.findtext("User") or "").strip()
        name = (entry.findtext("ServerName") or "").strip()
        if not port:
            continue
        key = (port.casefold(), user.casefold())
        if key in seen:
            continue
        seen.add(key)
        connections.append({"name": name, "port": port, "user": user})
    return path, connections


def connection_summary(info: dict[str, str], source: str) -> dict[str, str]:
    return {
        "source": source,
        "p4Port": os.environ.get("P4PORT") or info.get("serverAddress") or "",
        "p4User": os.environ.get("P4USER") or info.get("userName") or "",
    }


def resolve_connection_and_client(
    p4_exe: str,
    workspace: Path,
    requested_client: str | None,
    requested_port: str | None,
    requested_user: str | None,
    connection_map: Path | None,
) -> tuple[str, dict[str, str], dict[str, str]]:
    if requested_port:
        os.environ["P4PORT"] = requested_port
    if requested_user:
        os.environ["P4USER"] = requested_user

    try:
        client, info = resolve_client(p4_exe, workspace, requested_client)
        source = "explicit" if requested_port or requested_user else "environment"
        return client, info, connection_summary(info, source)
    except CodexP4DiffError as exc:
        if requested_port:
            raise
        initial_error_text = str(exc)

    map_path, saved_connections = load_p4v_connections(connection_map)
    matches: list[tuple[str, str, str, dict[str, str]]] = []
    failures: list[str] = []
    for saved in saved_connections:
        port = saved["port"]
        user = requested_user or saved["user"]
        overrides = {"P4PORT": port}
        if user:
            overrides["P4USER"] = user
        try:
            with temporary_environment(overrides):
                client, info = resolve_client(p4_exe, workspace, requested_client)
        except CodexP4DiffError as exc:
            failures.append(f"{port}: {str(exc).splitlines()[-1]}")
            continue
        key = (port.casefold(), client.casefold())
        if any((item[0].casefold(), item[2].casefold()) == key for item in matches):
            continue
        matches.append((port, user, client, info))

    if len(matches) == 1:
        port, user, client, info = matches[0]
        os.environ["P4PORT"] = port
        if user:
            os.environ["P4USER"] = user
        summary = connection_summary(info, "p4v-connection-map")
        summary["connectionMap"] = str(map_path)
        return client, info, summary

    if len(matches) > 1:
        choices = ", ".join(f"{port} / {client}" for port, _user, client, _info in matches)
        raise CodexP4DiffError(
            f"多个 P4V 连接都能映射 {workspace}：{choices}。请显式传入 --port 和 --client。"
        )

    failure_hint = f" 已检查 {len(saved_connections)} 个 P4V 保存连接。" if saved_connections else ""
    if failures:
        failure_hint += " " + "；".join(failures[:3])
    raise CodexP4DiffError(
        f"{initial_error_text}\n无法为 {workspace} 自动找到可用的 P4 连接。{failure_hint} "
        "请确认 P4V 已保存该连接，或显式传入 --port、--user 和 --client。"
    )


def resolve_client(p4_exe: str, workspace: Path, requested: str | None) -> tuple[str, dict[str, str]]:
    workspace_cwd = workspace.parent if workspace.is_file() else workspace
    if requested:
        info = info_record(p4_exe, requested, cwd=workspace_cwd)
        client_name = info.get("clientName") or requested
        client_root_text = info.get("clientRoot")
        if not client_root_text:
            raise CodexP4DiffError(f"Perforce 客户端 {client_name!r} 没有 clientRoot")
        client_root = Path(client_root_text).resolve()
        require_inside(workspace, client_root, "Workspace")
        return client_name, info

    active = info_record(p4_exe, cwd=workspace_cwd)
    active_name = active.get("clientName")
    active_root_text = active.get("clientRoot")
    if active_name and active_root_text and active_name != "*unknown*":
        active_root = Path(active_root_text).resolve()
        if is_inside(workspace, active_root):
            return active_name, active

    user_name = active.get("userName")
    if not user_name:
        raise CodexP4DiffError("无法确定 P4 用户；请显式传入 --client")
    client_records = p4_tagged(
        p4_exe, ["clients", "-u", user_name], record_key="client", cwd=workspace_cwd
    )
    host_name = socket.gethostname().lower()
    candidates: list[tuple[int, bool, str, Path]] = []
    for record in client_records:
        name = record.get("client")
        root_text = record.get("Root") or record.get("root")
        if not name or not root_text or root_text == "null":
            continue
        root = Path(root_text).resolve()
        if not is_inside(workspace, root):
            continue
        host = (record.get("Host") or record.get("host") or "").lower()
        candidates.append((len(canonical(root)), not host or host == host_name, name, root))
    candidates.sort(reverse=True)
    if not candidates:
        raise CodexP4DiffError(
            f"用户 {user_name!r} 所属的 P4 客户端均不包含 {workspace}。请显式传入 --client。"
        )
    best_length = candidates[0][0]
    best = [candidate for candidate in candidates if candidate[0] == best_length and candidate[1]]
    if len(best) != 1:
        names = ", ".join(candidate[2] for candidate in candidates)
        raise CodexP4DiffError(f"有多个 P4 客户端匹配 {workspace}：{names}。请显式传入 --client。")
    selected = best[0][2]
    return selected, info_record(p4_exe, selected, cwd=workspace_cwd)


def resolve_where(p4_exe: str, client: str, depot_file: str, cwd: Path) -> Path:
    records = p4_tagged(
        p4_exe,
        ["where", depot_file],
        client=client,
        record_key="depotFile",
        cwd=cwd,
    )
    for record in records:
        path_text = record.get("path")
        if path_text:
            return Path(path_text).resolve()
    raise CodexP4DiffError(f"p4 where 未返回 {depot_file} 对应的本地路径")


def parse_revision(value: str | None) -> int | None:
    if not value:
        return None
    try:
        revision = int(value)
    except ValueError:
        return None
    return revision if revision > 0 else None


def opened_files(
    p4_exe: str,
    client: str,
    scope: Path,
    changelist: str,
    client_root: Path,
    project_root: Path,
) -> list[dict[str, Any]]:
    scope_root = scope.parent if scope.is_file() else scope
    escaped_scope = p4_escape_filespec_path(scope)
    file_spec = escaped_scope if scope.is_file() else os.path.join(escaped_scope, "...")
    records = p4_tagged(
        p4_exe,
        ["opened", "-c", changelist, file_spec],
        client=client,
        record_key="depotFile",
        allow_empty_error=True,
        cwd=scope_root,
    )
    result: list[dict[str, Any]] = []
    require_inside(scope_root, project_root, "工作区范围")
    for record in records:
        depot_file = record.get("depotFile")
        if not depot_file:
            continue
        client_file = resolve_where(p4_exe, client, depot_file, scope_root)
        require_inside(client_file, client_root, "已映射的客户端文件")
        require_inside(client_file, scope_root, "已打开的文件")
        require_inside(client_file, project_root, "Codex 项目文件")
        relative_text = os.path.relpath(str(client_file), str(project_root))
        if relative_text == os.pardir or relative_text.startswith(os.pardir + os.sep):
            raise CodexP4DiffError(f"{client_file} 的相对路径不安全")
        action = (record.get("action") or "edit").lower()
        have_revision = None if action in NO_BASE_ACTIONS else parse_revision(record.get("rev"))
        result.append(
            {
                "relativePath": Path(relative_text).as_posix(),
                "depotFile": depot_file,
                "clientFile": str(client_file),
                "action": action,
                "haveRev": have_revision,
                "fileType": record.get("type") or "",
                "change": record.get("change") or changelist,
            }
        )
    result.sort(key=lambda item: item["relativePath"].lower())
    return result


def user_data_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data).expanduser()
    if os.name == "nt":
        return Path.home() / "AppData" / "Local"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    if xdg_state_home:
        return Path(xdg_state_home).expanduser()
    return Path.home() / ".local" / "state"


def default_sidecar(info: dict[str, str], client: str, changelist: str, project_root: Path) -> Path:
    server = info.get("serverAddress") or "server"
    server_hash = hashlib.sha256(server.lower().encode("utf-8")).hexdigest()[:10]
    scope_hash = hashlib.sha256(canonical(project_root).encode("utf-8")).hexdigest()[:10]
    return user_data_root() / "CodexP4Diff" / "Native" / f"{sanitize_component(client)}-{server_hash}" / f"{sanitize_component(changelist)}-{scope_hash}"


def audit_project_root(info: dict[str, str], client: str, project_root: Path) -> Path:
    server = info.get("serverAddress") or "server"
    server_hash = hashlib.sha256(server.lower().encode("utf-8")).hexdigest()[:10]
    scope_hash = hashlib.sha256(canonical(project_root).encode("utf-8")).hexdigest()[:10]
    return (
        user_data_root()
        / "CodexP4Diff"
        / "Audit"
        / f"{sanitize_component(client)}-{server_hash}"
        / scope_hash
    )


def audit_session_root(
    info: dict[str, str], client: str, project_root: Path, session_id: str
) -> Path:
    return audit_project_root(info, client, project_root) / "sessions" / sanitize_component(session_id)


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def replace_text_atomically(path: Path, value: str) -> str:
    pending = path.with_name(f"{path.name}.pending-{uuid.uuid4().hex[:8]}")
    try:
        pending.write_text(value, encoding="utf-8", newline="\n")
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)
    return "atomic-git-pointer-replace"


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CodexP4DiffError(f"{path} 中的 JSON 无效：{exc}") from exc
    if not isinstance(value, dict):
        raise CodexP4DiffError(f"{path} 应为 JSON 对象")
    return value


def resolve_auto_changelist(
    info: dict[str, str], client: str, project_root: Path, session_id: str | None
) -> str:
    if not session_id:
        raise CodexP4DiffError(
            "--changelist auto 需要 Codex 会话 ID；请从 Codex 中运行，或显式传入数字变更列表。"
        )
    ledger_path = audit_session_root(info, client, project_root, session_id) / "session.json"
    if not ledger_path.is_file():
        raise CodexP4DiffError(
            f"当前会话尚未建立 P4 变更列表：{ledger_path}。先让 Codex 完成一次文件修改，"
            "或显式传入 --changelist <数字>。"
        )
    ledger = read_json(ledger_path)
    expected = {
        "schemaVersion": AUDIT_SCHEMA_VERSION,
        "generatedBy": TOOL_NAME,
        "sessionId": session_id,
        "client": client,
        "serverAddress": info.get("serverAddress") or "",
        "userName": info.get("userName") or "",
        "clientRoot": info.get("clientRoot") or "",
        "projectRoot": str(project_root.resolve()),
    }
    mismatches: list[str] = []
    for key, expected_value in expected.items():
        recorded = ledger.get(key)
        if key in {"projectRoot", "clientRoot"}:
            same = bool(
                recorded
                and canonical(Path(str(recorded))) == canonical(Path(str(expected_value)))
            )
        else:
            same = str(recorded or "").casefold() == str(expected_value).casefold()
        if not same:
            mismatches.append(f"{key}={recorded!r}")
    if mismatches:
        raise CodexP4DiffError(
            "当前会话账本不属于所选 P4 上下文：" + "；".join(mismatches)
        )
    changelist = str(ledger.get("changelist") or "")
    if not changelist.isdigit():
        raise CodexP4DiffError("当前会话尚未创建 pending changelist。")
    return changelist


def validate_auto_changelist_context(
    p4_exe: str,
    client: str,
    project_root: Path,
    changelist: str,
    session_id: str,
) -> None:
    completed = run_command(
        [p4_exe, "-c", client, "change", "-o", changelist],
        cwd=project_root,
    )
    spec = decode_output(completed.stdout)
    status = re.search(r"(?im)^Status:\s*(\S+)", spec)
    recorded_client = re.search(r"(?im)^Client:\s*(\S+)", spec)
    if not status or status.group(1).lower() != "pending":
        raise CodexP4DiffError(f"会话 changelist {changelist} 不是 pending 状态。")
    if not recorded_client or recorded_client.group(1).casefold() != client.casefold():
        raise CodexP4DiffError(f"会话 changelist {changelist} 不属于 P4 client {client}。")
    if "Managed-By: codex-p4-diff" not in spec or f"Codex-Session: {session_id}" not in spec:
        raise CodexP4DiffError(
            f"会话 changelist {changelist} 缺少当前 Codex 会话所有权标记，拒绝自动使用。"
        )


def parse_p4_setting(raw: bytes, name: str) -> str | None:
    prefix = f"{name}="
    for line in decode_output(raw).splitlines():
        if not line.startswith(prefix):
            continue
        value = line[len(prefix):]
        marker = value.rfind(" (set")
        if marker >= 0:
            value = value[:marker]
        return value.strip() or None
    return None


def p4_setting(p4_exe: str, name: str, cwd: Path) -> str | None:
    completed = run_command([p4_exe, "set", name], cwd=cwd, allowed_codes=(0, 1))
    return parse_p4_setting(completed.stdout, name)


def p4_path_is_ignored(p4_exe: str, path: Path) -> tuple[bool, str]:
    completed = run_command(
        [p4_exe, "ignores", "-i", "-v", p4_escape_filespec_path(path)], cwd=path.parent, allowed_codes=(0, 1)
    )
    detail = "\n".join(
        part for part in (decode_output(completed.stdout).strip(), decode_output(completed.stderr).strip()) if part
    )
    ignored = bool(detail) and " not ignored" not in detail.lower()
    return ignored, detail


def ensure_git_pointer_is_p4_ignored(p4_exe: str, pointer: Path) -> dict[str, Any]:
    ignored, detail = p4_path_is_ignored(p4_exe, pointer)
    current_setting = p4_setting(p4_exe, "P4IGNORE", pointer.parent)
    if ignored:
        return {"ignored": True, "setting": current_setting or "", "detail": detail}
    setting_hint = f"当前 P4IGNORE={current_setting!r}。" if current_setting else "当前未设置 P4IGNORE。"
    raise CodexP4DiffError(
        f"P4 尚未忽略 Git 指针 {pointer}。{setting_hint}"
        "请在自己的 P4IGNORE 文件中加入 .git，并确认 p4 ignores -i -v <项目\\.git> 显示已忽略后重试。"
    )


def parse_gitdir_pointer(pointer: Path) -> Path:
    try:
        first_line = pointer.read_text(encoding="utf-8-sig").splitlines()[0]
    except (OSError, IndexError, UnicodeError) as exc:
        raise CodexP4DiffError(f"{pointer} 中的 Git 指针文件无效：{exc}") from exc
    prefix = "gitdir:"
    if not first_line.lower().startswith(prefix):
        raise CodexP4DiffError(f"现有 {pointer} 不是 Git 目录指针")
    target_text = first_line[len(prefix):].strip()
    if not target_text:
        raise CodexP4DiffError(f"现有 {pointer} 的 gitdir 目标为空")
    target = Path(target_text)
    return (target if target.is_absolute() else pointer.parent / target).resolve()


def marker_path(git_dir: Path) -> Path:
    return git_dir / MARKER_NAME


def validate_owned_gitdir(git_dir: Path, project_root: Path | None = None) -> dict[str, Any]:
    data = read_json(marker_path(git_dir))
    if data.get("generatedBy") != TOOL_NAME or data.get("schemaVersion") != SCHEMA_VERSION:
        raise CodexP4DiffError(f"Git 目录不属于当前版本的 {TOOL_NAME}：{git_dir}")
    if data.get("mode") != "native-p4-worktree":
        raise CodexP4DiffError(f"Git 目录模式不受支持：{git_dir}")
    if project_root is not None:
        recorded = data.get("projectRoot")
        if not recorded or canonical(Path(str(recorded))) != canonical(project_root):
            raise CodexP4DiffError(f"Git 目录属于另一个项目：{recorded}")
    return data


def validate_sidecar_identity(
    data: dict[str, Any],
    info: dict[str, str],
    client: str,
    changelist: str,
    client_root: Path,
) -> None:
    if data.get("initializing") and not data.get("baselineCommit"):
        return
    expected = {
        "serverAddress": info.get("serverAddress") or "",
        "userName": info.get("userName") or "",
        "client": client,
        "clientRoot": str(client_root),
        "changelist": changelist,
    }
    mismatches: list[str] = []
    for key, expected_value in expected.items():
        recorded_value = str(data.get(key) or "")
        if key == "clientRoot":
            same = bool(
                recorded_value
                and expected_value
                and canonical(Path(recorded_value)) == canonical(Path(expected_value))
            )
        else:
            same = recorded_value.casefold() == expected_value.casefold()
        if same:
            continue
        mismatches.append(f"{key}: marker={recorded_value!r}, current={expected_value!r}")
    if mismatches:
        raise CodexP4DiffError(
            "现有 sidecar 属于不同的 P4 上下文，拒绝复用："
            + "；".join(mismatches)
            + "。请省略 --sidecar 让工具自动选择，或指定一个新的空目录。"
        )


def validate_project_slot(project_root: Path, expected_git_dir: Path) -> tuple[Path, str]:
    pointer = project_root / ".git"
    if not pointer.exists():
        return pointer, "created"
    if pointer.is_dir():
        raise CodexP4DiffError(f"拒绝覆盖 {pointer} 处的真实 Git 仓库")
    current = parse_gitdir_pointer(pointer)
    if canonical(current) == canonical(expected_git_dir):
        validate_owned_gitdir(current, project_root)
        return pointer, "unchanged"
    try:
        validate_owned_gitdir(current, project_root)
    except CodexP4DiffError as exc:
        raise CodexP4DiffError(f"拒绝替换来源不明的 Git 指针 {pointer}：{exc}") from exc
    return pointer, "replaced"


def git_environment(git_dir: Path, project_root: Path, index_file: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_DIR"] = str(git_dir)
    env["GIT_WORK_TREE"] = str(project_root)
    if index_file is not None:
        env["GIT_INDEX_FILE"] = str(index_file)
    return env


def git_run(
    git_exe: str,
    git_dir: Path,
    project_root: Path,
    *args: str,
    index_file: Path | None = None,
    allowed_codes: Iterable[int] = (0,),
) -> subprocess.CompletedProcess[bytes]:
    return run_command(
        [git_exe, *args],
        env=git_environment(git_dir, project_root, index_file),
        allowed_codes=allowed_codes,
    )


def configure_git(git_exe: str, git_dir: Path, project_root: Path) -> None:
    hooks_dir = git_dir / "hooks"
    settings = [
        ("user.name", "Codex P4 Diff"),
        ("user.email", "codex-p4-diff@local"),
        ("core.bare", "false"),
        ("core.worktree", str(project_root)),
        ("core.autocrlf", "false"),
        ("core.safecrlf", "false"),
        ("core.filemode", "false"),
        ("core.hooksPath", str(hooks_dir)),
        ("commit.gpgsign", "false"),
        ("status.showUntrackedFiles", "no"),
        ("diff.renames", "true"),
    ]
    for key, value in settings:
        git_run(git_exe, git_dir, project_root, "config", key, value)
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hooks = {
        "pre-commit": "Codex P4 Diff: Git commit 已禁用；请通过 Perforce 提交。",
        "pre-push": "Codex P4 Diff: Git push 已禁用；P4 工作区不应作为 Git 仓库发布。",
    }
    for hook_name, message in hooks.items():
        hook = hooks_dir / hook_name
        hook.write_text(
            "#!/bin/sh\n" + f"echo '{message}' >&2\n" + "exit 1\n",
            encoding="utf-8",
            newline="\n",
        )
        try:
            hook.chmod(0o755)
        except OSError:
            pass


def ensure_sidecar(
    git_exe: str,
    sidecar: Path,
    project_root: Path,
    client_root: Path,
) -> tuple[Path, dict[str, Any] | None]:
    sidecar = sidecar.resolve()
    if is_inside(sidecar, client_root):
        raise CodexP4DiffError(f"Git sidecar 必须位于 P4 客户端根目录之外：{sidecar}")
    git_dir = sidecar / "git"
    existing_marker: dict[str, Any] | None = None
    if sidecar.exists():
        children = list(sidecar.iterdir())
        if children:
            if not git_dir.is_dir() or not marker_path(git_dir).is_file():
                raise CodexP4DiffError(f"拒绝使用未标记的 sidecar 目录：{sidecar}")
            existing_marker = validate_owned_gitdir(git_dir, project_root)
    else:
        sidecar.mkdir(parents=True)
    if not git_dir.exists():
        run_command([git_exe, "init", "--bare", "-q", str(git_dir)])
    configure_git(git_exe, git_dir, project_root)
    if not marker_path(git_dir).is_file():
        write_json(
            marker_path(git_dir),
            {
                "schemaVersion": SCHEMA_VERSION,
                "generatedBy": TOOL_NAME,
                "mode": "native-p4-worktree",
                "projectRoot": str(project_root),
                "initializing": True,
            },
        )
    return git_dir, existing_marker


def baseline_signature(files: list[dict[str, Any]]) -> str:
    normalized = [
        {
            "relativePath": item["relativePath"],
            "depotFile": item["depotFile"],
            "action": item["action"],
            "haveRev": item.get("haveRev"),
            "fileType": item.get("fileType") or "",
        }
        for item in files
    ]
    raw = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def git_file_mode(item: dict[str, Any]) -> str:
    file_type = str(item.get("fileType") or "").lower()
    if file_type.split("+", 1)[0] == "symlink":
        return "120000"
    return "100755" if "+x" in file_type else "100644"


def print_baseline(
    p4_exe: str,
    client: str,
    destination: Path,
    depot_file: str,
    revision: int,
    cwd: Path,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        [p4_exe, "-c", client, "print", "-q", "-o", str(destination), f"{depot_file}#{revision}"],
        cwd=cwd,
    )


def build_baseline(
    *,
    p4_exe: str,
    git_exe: str,
    git_dir: Path,
    sidecar: Path,
    project_root: Path,
    client: str,
    changelist: str,
    files: list[dict[str, Any]],
) -> str:
    building = sidecar / f"baseline-building-{uuid.uuid4().hex[:10]}"
    building.mkdir()
    index_file = building / "index"
    try:
        git_run(git_exe, git_dir, project_root, "read-tree", "--empty", index_file=index_file)
        for item in files:
            revision = item.get("haveRev")
            if not revision:
                continue
            baseline_file = building / "files" / Path(item["relativePath"])
            print_baseline(
                p4_exe, client, baseline_file, item["depotFile"], int(revision), project_root
            )
            blob = decode_output(
                git_run(
                    git_exe, git_dir, project_root, "hash-object", "-w", "--",
                    str(baseline_file), index_file=index_file,
                ).stdout
            ).strip()
            if not re.fullmatch(r"[0-9a-fA-F]{40,64}", blob):
                raise CodexP4DiffError(f"无法为 {item['relativePath']} 创建 Git blob")
            git_run(
                git_exe, git_dir, project_root, "update-index", "--add", "--cacheinfo",
                git_file_mode(item), blob, item["relativePath"], index_file=index_file,
            )

        tree = decode_output(
            git_run(git_exe, git_dir, project_root, "write-tree", index_file=index_file).stdout
        ).strip()
        commit = decode_output(
            git_run(
                git_exe, git_dir, project_root, "commit-tree", tree, "-m",
                f"P4 #have 基线：{client} 变更列表 {changelist}", index_file=index_file,
            ).stdout
        ).strip()
        git_run(git_exe, git_dir, project_root, "symbolic-ref", "HEAD", BASELINE_REF)
        git_run(git_exe, git_dir, project_root, "update-ref", BASELINE_REF, commit)
        pending_index = git_dir / f"index.pending-{uuid.uuid4().hex[:8]}"
        shutil.copyfile(index_file, pending_index)
        os.replace(pending_index, git_dir / "index")
        return commit
    finally:
        shutil.rmtree(building, ignore_errors=True)


def staged_state(git_exe: str, git_dir: Path, project_root: Path) -> dict[str, Any]:
    staged = git_run(
        git_exe, git_dir, project_root, "diff", "--cached", "--quiet", allowed_codes=(0, 1)
    ).returncode == 1
    unmerged = bool(decode_output(git_run(git_exe, git_dir, project_root, "ls-files", "-u").stdout).strip())
    head = decode_output(git_run(git_exe, git_dir, project_root, "rev-parse", "HEAD").stdout).strip()
    return {"staged": staged, "unmerged": unmerged, "head": head}


def ensure_intent_to_add(
    git_exe: str,
    git_dir: Path,
    project_root: Path,
    files: list[dict[str, Any]],
) -> None:
    for item in files:
        if item["action"] not in NO_BASE_ACTIONS:
            continue
        source = Path(item["clientFile"])
        if not source.is_file() and not source.is_symlink():
            raise CodexP4DiffError(f"本地缺少 P4 新增文件：{source}")
        tracked = git_run(
            git_exe, git_dir, project_root, "ls-files", "--error-unmatch", "--",
            item["relativePath"], allowed_codes=(0, 1),
        ).returncode == 0
        if not tracked:
            git_run(git_exe, git_dir, project_root, "add", "-N", "-f", "--", item["relativePath"])


def codex_new_task_url(path: Path) -> str:
    return f"codex://threads/new?path={quote(str(path), safe='')}"


def nearest_existing_parent(path: Path) -> Path:
    candidate = path.resolve()
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    return candidate


def windows_sandbox_diagnostic() -> dict[str, Any]:
    if os.name != "nt" or not Path("C:/tmp").exists():
        return {"status": "not-applicable"}
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    config_path = codex_home / "config.toml"
    configured = False
    if config_path.is_file():
        try:
            text = config_path.read_text(encoding="utf-8-sig")
        except OSError:
            text = ""
        section = re.search(
            r"(?ms)^\[sandbox_workspace_write\]\s*(.*?)(?=^\[|\Z)", text
        )
        configured = bool(
            section
            and re.search(r"(?m)^\s*exclude_slash_tmp\s*=\s*true\s*(?:#.*)?$", section.group(1))
        )
    result: dict[str, Any] = {
        "status": "pass" if configured else "warning",
        "configPath": str(config_path),
        "excludeSlashTmp": configured,
    }
    if not configured:
        result["detail"] = (
            "检测到 C:\\tmp。如果 Codex 日志出现 SetNamedSecurityInfoW failed: 5 或 "
            "sandbox setup refresh 错误，请在 config.toml 添加 "
            "[sandbox_workspace_write] exclude_slash_tmp = true，然后新建任务。"
        )
    return result


def doctor_report(
    *,
    p4_exe: str,
    git_exe: str,
    p4vc_exe: str | None,
    p4vc_error: str | None,
    project_root: Path,
    sidecar: Path,
    client_root: Path,
    files: list[dict[str, Any]],
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, detail: Any, hint: str | None = None) -> None:
        item: dict[str, Any] = {"name": name, "status": status, "detail": detail}
        if hint:
            item["hint"] = hint
        checks.append(item)

    add("python", "pass", sys.version.split()[0])
    add("p4", "pass", p4_exe)
    add("git", "pass", git_exe)
    if p4vc_exe:
        add("p4vc", "pass", p4vc_exe)
    else:
        add(
            "p4vc",
            "warning",
            p4vc_error or "未找到 P4V/P4VC",
            "原生 Review 仍可使用；如需外部 Diff，请安装 P4V 或传入 --p4vc 完整路径。",
        )

    if files:
        add("opened-files", "pass", len(files))
    else:
        add(
            "opened-files",
            "error",
            0,
            "先用 p4 edit/add/delete 将目标文件放入所选变更列表，或调整 --workspace/--changelist。",
        )

    expected_git_dir = sidecar.resolve() / "git"
    try:
        _pointer, action = validate_project_slot(project_root, expected_git_dir)
        add("project-git-slot", "pass", action)
    except CodexP4DiffError as exc:
        add(
            "project-git-slot",
            "error",
            str(exc),
            "真实 Git 仓库不能覆盖；此时仅使用 --open-p4-diff，或选择不含 .git 的 P4 项目根。",
        )

    try:
        ignored = ensure_git_pointer_is_p4_ignored(p4_exe, project_root / ".git")
        add("p4-ignore", "pass", ignored)
    except CodexP4DiffError as exc:
        add("p4-ignore", "error", str(exc))

    if is_inside(sidecar.resolve(), client_root):
        add("sidecar-location", "error", str(sidecar), "将 --sidecar 放到 P4 client root 之外。")
    else:
        parent = nearest_existing_parent(sidecar)
        writable = os.access(parent, os.W_OK)
        add(
            "sidecar-location",
            "pass" if writable else "error",
            {"path": str(sidecar), "nearestExistingParent": str(parent), "writable": writable},
            None if writable else "选择当前用户可写且位于 P4 client root 之外的 --sidecar 目录。",
        )

    project_writable = os.access(project_root, os.W_OK)
    add(
        "project-root-write",
        "pass" if project_writable else "error",
        {"path": str(project_root), "writable": project_writable},
        None if project_writable else "检查项目目录权限；启用时需要创建或替换项目根 .git 指针文件。",
    )

    sandbox = windows_sandbox_diagnostic()
    if sandbox["status"] != "not-applicable":
        add("windows-sandbox", sandbox["status"], sandbox)

    return {
        "ready": not any(item["status"] == "error" for item in checks),
        "checks": checks,
        "troubleshooting": TROUBLESHOOTING_URL,
    }


def troubleshooting_hints(message: str) -> list[str]:
    hints = ["先运行 --doctor 查看完整环境检查。"]
    lowered = message.lower()
    if "p4" in lowered and ("找不到" in message or "无法" in message):
        hints.append("安装 Helix Core CLI/P4V，并确认 p4 info 能在目标工作区成功。")
    if "git" in lowered and "找不到" in message:
        hints.append("安装 Git，或使用 --git 指向 git 可执行文件；Codex Desktop 的捆绑 Git 也会自动探测。")
    if "p4 尚未忽略" in message:
        hints.append("在生效的 P4IGNORE 文件中加入 .git，再运行 p4 ignores -i -v <项目\\.git> 验证。")
    if "真实 git 仓库" in lowered or "来源不明的 git 指针" in lowered:
        hints.append("不要覆盖已有 Git 元数据；保留该仓库并只使用 P4V diffhave。")
    if "sidecar" in lowered:
        hints.append("省略 --sidecar 使用自动目录，或指定 P4 client root 之外的新空目录。")
    hints.append(f"故障排查：{TROUBLESHOOTING_URL}")
    return list(dict.fromkeys(hints))


def enable_native_review(
    *,
    p4_exe: str,
    git_exe: str,
    project_root: Path,
    workspace: Path,
    client: str,
    info: dict[str, str],
    changelist: str,
    files: list[dict[str, Any]],
    sidecar: Path,
    discard_git_state: bool,
) -> dict[str, Any]:
    project_root = project_root.resolve()
    workspace = workspace.resolve()
    require_inside(workspace, project_root, "工作区范围")
    client_root_text = info.get("clientRoot")
    if not client_root_text:
        raise CodexP4DiffError(f"Perforce 客户端 {client!r} 没有 clientRoot")
    client_root = Path(client_root_text).resolve()

    expected_git_dir = sidecar.resolve() / "git"
    pointer, pointer_action = validate_project_slot(project_root, expected_git_dir)
    p4_ignore = ensure_git_pointer_is_p4_ignored(p4_exe, pointer)
    git_dir, existing_marker = ensure_sidecar(git_exe, sidecar, project_root, client_root)
    if existing_marker:
        validate_sidecar_identity(existing_marker, info, client, changelist, client_root)

    signature = baseline_signature(files)
    baseline_reused = False
    if existing_marker and existing_marker.get("baselineCommit"):
        state = staged_state(git_exe, git_dir, project_root)
        recorded_commit = str(existing_marker.get("baselineCommit") or "")
        same_baseline = existing_marker.get("baselineSignature") == signature
        head_is_baseline = state["head"] == recorded_commit
        if same_baseline and head_is_baseline and not state["unmerged"]:
            baseline_commit = recorded_commit
            baseline_reused = True
        else:
            reasons: list[str] = []
            if state["staged"]:
                reasons.append("存在已暂存更改")
            if state["unmerged"]:
                reasons.append("存在未合并索引项")
            if not head_is_baseline:
                reasons.append("Git HEAD 已偏离 P4 #have 基线")
            if not same_baseline:
                reasons.append("P4 文件范围或 #have 版本已变化")
            if (state["staged"] or state["unmerged"] or not head_is_baseline) and not discard_git_state:
                raise CodexP4DiffError(
                    "；".join(reasons)
                    + "。为避免丢失 Git 审阅状态，已停止刷新；确认后使用 --discard-git-review-state。"
                )
            baseline_commit = build_baseline(
                p4_exe=p4_exe, git_exe=git_exe, git_dir=git_dir, sidecar=sidecar.resolve(),
                project_root=project_root, client=client, changelist=changelist, files=files,
            )
    else:
        baseline_commit = build_baseline(
            p4_exe=p4_exe, git_exe=git_exe, git_dir=git_dir, sidecar=sidecar.resolve(),
            project_root=project_root, client=client, changelist=changelist, files=files,
        )

    ensure_intent_to_add(git_exe, git_dir, project_root, files)
    marker = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedBy": TOOL_NAME,
        "mode": "native-p4-worktree",
        "generatedAtUtc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "projectRoot": str(project_root),
        "gitDir": str(git_dir),
        "serverAddress": info.get("serverAddress") or "",
        "userName": info.get("userName") or "",
        "client": client,
        "clientRoot": str(client_root),
        "workspaceScope": str(workspace),
        "changelist": changelist,
        "baseline": "#have",
        "baselineRef": BASELINE_REF,
        "baselineCommit": baseline_commit,
        "baselineSignature": signature,
        "files": files,
    }
    write_json(marker_path(git_dir), marker)

    expected_pointer = f"gitdir: {git_dir.as_posix()}\n"
    review_refresh_signal = replace_text_atomically(pointer, expected_pointer)

    git_top = Path(
        decode_output(run_command([git_exe, "-C", str(project_root), "rev-parse", "--show-toplevel"]).stdout).strip()
    ).resolve()
    absolute_git_dir = Path(
        decode_output(run_command([git_exe, "-C", str(project_root), "rev-parse", "--absolute-git-dir"]).stdout).strip()
    ).resolve()
    if canonical(git_top) != canonical(project_root):
        raise CodexP4DiffError(f"Git top-level 为 {git_top}，预期为当前项目 {project_root}")
    if canonical(absolute_git_dir) != canonical(git_dir):
        raise CodexP4DiffError(f"Git 目录为 {absolute_git_dir}，预期为 sidecar {git_dir}")

    status = decode_output(
        run_command([git_exe, "-C", str(project_root), "status", "--short", "--untracked-files=no"]).stdout
    ).splitlines()
    review_warnings: list[str] = []
    if not status:
        review_warnings.append(
            "所选 P4 文件当前都与 #have 内容一致，因此 Review 为空；这通常表示文件仅被打开但尚无内容改动。"
        )
    elif len(status) < len(files):
        review_warnings.append(
            f"{len(files) - len(status)} 个 P4 已打开文件与 #have 内容一致，不会出现在 Review 中。"
        )
    return {
        "enabled": True,
        "mode": "native-p4-worktree",
        "projectRoot": str(project_root),
        "gitTopLevel": str(git_top),
        "gitDir": str(git_dir),
        "sidecarRoot": str(sidecar.resolve()),
        "gitPointer": str(pointer),
        "gitPointerAction": pointer_action,
        "reviewRefreshSignal": review_refresh_signal,
        "p4Ignore": p4_ignore,
        "baseline": "#have",
        "baselineCommit": baseline_commit,
        "baselineReused": baseline_reused,
        "visibleGitChangeCount": len(status),
        "gitStatus": status,
        "reviewWarnings": review_warnings,
        "reviewShortcut": "Ctrl+Shift+G",
        "newTaskUrl": codex_new_task_url(project_root),
        "openHint": "使用 newTaskUrl 新建绑定当前项目的任务，再按 Ctrl+Shift+G 打开原生审阅。",
        "warning": "Git 还原会直接改写真实 P4 文件；Git commit/push 已由本地 hooks 拦截，正式提交请使用 Perforce。",
    }


def disable_native_review(project_root: Path) -> dict[str, Any]:
    project_root = project_root.resolve()
    pointer = project_root / ".git"
    if not pointer.exists():
        return {"generatedBy": TOOL_NAME, "disabled": False, "projectRoot": str(project_root), "reason": "不存在 .git 指针"}
    if pointer.is_dir():
        raise CodexP4DiffError(f"拒绝移除 {pointer} 处的真实 Git 目录")
    git_dir = parse_gitdir_pointer(pointer)
    marker = validate_owned_gitdir(git_dir, project_root)
    pointer.unlink()
    marker["disabledAtUtc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    write_json(marker_path(git_dir), marker)
    return {
        "generatedBy": TOOL_NAME,
        "disabled": True,
        "projectRoot": str(project_root),
        "removedPointer": str(pointer),
        "sidecarRoot": str(git_dir.parent),
    }


def repair_p4_state(p4_exe: str, client: str, scope: Path, changelist: str) -> dict[str, Any]:
    escaped_scope = p4_escape_filespec_path(scope)
    file_spec = escaped_scope if scope.is_file() else os.path.join(escaped_scope, "...")
    completed = run_command(
        [p4_exe, "-c", client, "revert", "-a", "-c", changelist, file_spec],
        cwd=scope.parent if scope.is_file() else scope,
        allowed_codes=(0, 1),
    )
    detail = "\n".join(
        part for part in (decode_output(completed.stdout).strip(), decode_output(completed.stderr).strip()) if part
    )
    return {
        "executed": True,
        "command": f"p4 -c {client} revert -a -c {changelist} {file_spec}",
        "detail": detail,
    }


def is_text_file(item: dict[str, Any]) -> bool:
    return str(item.get("fileType") or "").split("+", 1)[0].lower() in TEXT_FILE_TYPES


def p4vc_launcher(p4vc_exe: str) -> list[str]:
    candidate = Path(p4vc_exe)
    if candidate.stem.lower() == "p4v":
        return [str(candidate), "-p4vc"]
    if os.name == "nt" and candidate.suffix.lower() in {".bat", ".cmd"}:
        p4v_exe = candidate.with_name("p4v.exe")
        if p4v_exe.is_file():
            return [str(p4v_exe), "-p4vc"]
    return [p4vc_exe]


def p4vc_diffhave_command(
    p4vc_exe: str,
    client: str,
    files: list[dict[str, Any]],
    connection: dict[str, str],
) -> tuple[list[str], list[str]]:
    text_files = [str(item["clientFile"]) for item in files if is_text_file(item)]
    if not text_files:
        return [], []
    command = p4vc_launcher(p4vc_exe)
    port = connection.get("p4Port")
    user = connection.get("p4User")
    if port:
        command.extend(["-p", port])
    if user:
        command.extend(["-u", user])
    command.extend(["-c", client, "diffhave", *text_files])
    return command, text_files


def launch_detached(argv: list[str]) -> None:
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    try:
        subprocess.Popen(argv, **kwargs)
    except FileNotFoundError as exc:
        raise CodexP4DiffError(f"找不到 P4V/P4VC 启动程序：{argv[0]}") from exc
    except PermissionError as exc:
        raise CodexP4DiffError(f"没有权限启动 P4V/P4VC：{argv[0]}") from exc
    except OSError as exc:
        raise CodexP4DiffError(f"启动 P4V/P4VC 失败：{exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="把 P4 #have 基线接入当前工程的 Codex 原生 Git 审阅。")
    parser._optionals.title = "选项"
    parser._actions[0].help = "显示此帮助信息并退出"
    parser.add_argument("--workspace", default=os.getcwd(), help="P4 工作区目录或文件范围")
    parser.add_argument("--project-root", help="Codex 项目根目录；默认使用 --workspace 目录")
    parser.add_argument("--client", help="显式指定 P4 客户端名称")
    parser.add_argument("--port", help="显式指定 P4PORT")
    parser.add_argument("--user", help="显式指定 P4USER")
    parser.add_argument("--p4v-connection-map", help="P4V connectionmap.xml 路径")
    parser.add_argument(
        "--changelist",
        default="default",
        help="default、auto 或数字形式的待提交变更列表；auto 使用当前 Codex 会话 CL",
    )
    parser.add_argument("--sidecar", help="外置 Git 元数据目录；默认位于当前用户的本地应用数据目录")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--enable-native-review", action="store_true", help="启用或刷新原生 Codex Review")
    mode.add_argument("--disable-native-review", action="store_true", help="移除本工具创建的 .git 指针")
    mode.add_argument("--repair-p4-state", action="store_true", help="执行 p4 revert -a")
    mode.add_argument("--doctor", action="store_true", help="只读检查依赖、P4 映射和 Review 前置条件")
    parser.add_argument(
        "--discard-git-review-state",
        action="store_true",
        help="刷新基线时允许丢弃本地 Git 审阅状态",
    )
    parser.add_argument("--p4", dest="p4_exe", help="p4 可执行文件；默认自动探测")
    parser.add_argument("--git", dest="git_exe", help="git 可执行文件；默认自动探测")
    parser.add_argument("--p4vc", dest="p4vc_exe", help="p4vc.bat 或 p4v.exe；默认自动探测")
    parser.add_argument("--open-p4-diff", action="store_true", help="调用 p4vc diffhave")
    parser.add_argument("--dry-run", action="store_true", help="只解析客户端和文件")
    return parser


def main() -> int:
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args()
    try:
        requested_changelist = str(args.changelist)
        if requested_changelist not in {"default", "auto"} and not requested_changelist.isdigit():
            raise CodexP4DiffError("--changelist 必须为 default、auto 或数字形式")
        workspace = Path(args.workspace).resolve()
        if not workspace.exists():
            raise CodexP4DiffError(f"工作区范围不存在：{workspace}")
        workspace_directory = workspace.parent if workspace.is_file() else workspace
        project_root = Path(args.project_root).resolve() if args.project_root else workspace_directory
        if not project_root.is_dir():
            raise CodexP4DiffError(f"Codex 项目根目录不存在：{project_root}")
        require_inside(workspace_directory, project_root, "工作区范围")

        if args.disable_native_review:
            print(json.dumps(disable_native_review(project_root), ensure_ascii=False, indent=2))
            return 0

        p4_exe = resolve_executable(
            args.p4_exe,
            "p4",
            "请安装 Perforce Helix Core CLI（P4V 安装包可包含 p4.exe）。",
        )
        needs_git = bool(args.enable_native_review or args.doctor)
        git_exe = (
            resolve_executable(args.git_exe, "git", "请安装 Git 2.x。")
            if needs_git
            else args.git_exe
        )

        p4vc_exe: str | None = None
        p4vc_error: str | None = None
        if args.open_p4_diff or args.doctor:
            try:
                p4vc_exe = resolve_executable(
                    args.p4vc_exe,
                    "p4vc",
                    "请安装 Perforce P4V；原生 Codex Review 本身不依赖 P4V。",
                )
            except CodexP4DiffError as exc:
                if args.open_p4_diff:
                    raise
                p4vc_error = str(exc)

        connection_map = Path(args.p4v_connection_map).resolve() if args.p4v_connection_map else None
        client, info, connection = resolve_connection_and_client(
            p4_exe, workspace, args.client, args.port, args.user, connection_map,
        )
        client_root_text = info.get("clientRoot")
        if not client_root_text:
            raise CodexP4DiffError(f"Perforce 客户端 {client!r} 没有 clientRoot")
        client_root = Path(client_root_text).resolve()
        session_id = os.environ.get("CODEX_THREAD_ID")
        changelist = (
            resolve_auto_changelist(
                info,
                client,
                project_root,
                session_id,
            )
            if requested_changelist == "auto"
            else requested_changelist
        )
        if requested_changelist == "auto":
            validate_auto_changelist_context(
                p4_exe,
                client,
                project_root,
                changelist,
                str(session_id),
            )
        files = opened_files(p4_exe, client, workspace, changelist, client_root, project_root)

        result: dict[str, Any] = {
            "generatedBy": TOOL_NAME,
            "dryRun": bool(args.dry_run),
            "client": client,
            "clientRoot": str(client_root),
            "projectRoot": str(project_root),
            "workspaceScope": str(workspace),
            "requestedChangelist": requested_changelist,
            "changelist": changelist,
            "openedFileCount": len(files),
            "files": files,
            "p4Connection": connection,
        }
        sidecar = (
            Path(args.sidecar).resolve()
            if args.sidecar
            else default_sidecar(info, client, changelist, project_root)
        )

        if args.doctor:
            result["doctor"] = doctor_report(
                p4_exe=p4_exe,
                git_exe=str(git_exe),
                p4vc_exe=p4vc_exe,
                p4vc_error=p4vc_error,
                project_root=project_root,
                sidecar=sidecar,
                client_root=client_root,
                files=files,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["doctor"]["ready"] else 2

        if args.enable_native_review and not files:
            raise CodexP4DiffError(
                f"在 {workspace} 的变更列表 {changelist} 中没有找到已打开文件。"
                "请先执行 p4 edit/add/delete，或检查 --workspace、--client 和 --changelist。"
            )

        if args.repair_p4_state and not args.dry_run:
            result["p4StateRepair"] = repair_p4_state(p4_exe, client, workspace, changelist)

        if args.enable_native_review and not args.dry_run:
            result["nativeReview"] = enable_native_review(
                p4_exe=p4_exe,
                git_exe=str(git_exe),
                project_root=project_root,
                workspace=workspace,
                client=client,
                info=info,
                changelist=changelist,
                files=files,
                sidecar=sidecar,
                discard_git_state=bool(args.discard_git_review_state),
            )
        else:
            result["sidecarRoot"] = str(sidecar)

        p4_diff_argv: list[str] = []
        p4_diff_files: list[str] = []
        if p4vc_exe:
            p4_diff_argv, p4_diff_files = p4vc_diffhave_command(
                p4vc_exe, client, files, connection
            )
        p4_diff_launched = False
        if args.open_p4_diff and p4_diff_argv and not args.dry_run:
            launch_detached(p4_diff_argv)
            p4_diff_launched = True
        result["p4NativeDiff"] = {
            "requested": bool(args.open_p4_diff),
            "available": bool(p4vc_exe),
            "launched": p4_diff_launched,
            "mode": "p4vc diffhave",
            "files": p4_diff_files,
            "command": subprocess.list2cmdline(p4_diff_argv) if p4_diff_argv else "",
            "hint": (
                ""
                if p4_diff_argv or not args.open_p4_diff
                else "目标变更列表中没有可由 P4V diffhave 打开的文本文件。"
            ),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except CodexP4DiffError as exc:
        payload = {
            "generatedBy": TOOL_NAME,
            "error": str(exc),
            "hints": troubleshooting_hints(str(exc)),
            "troubleshooting": TROUBLESHOOTING_URL,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        payload = {
            "generatedBy": TOOL_NAME,
            "error": f"本机环境操作失败：{exc}",
            "hints": [
                "检查项目根、sidecar 和可执行文件路径的访问权限。",
                "运行 --doctor 获取更完整的检查结果。",
                f"故障排查：{TROUBLESHOOTING_URL}",
            ],
            "troubleshooting": TROUBLESHOOTING_URL,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except Exception as exc:
        payload = {
            "generatedBy": TOOL_NAME,
            "error": f"Codex P4 Diff 遇到未预期错误：{type(exc).__name__}: {exc}",
            "hints": [
                "先运行 --doctor 并保存输出。",
                "升级到最新版本；若仍复现，请在 GitHub Issues 提交 doctor 输出和当前命令（移除敏感信息）。",
                f"问题反馈：{REPOSITORY_URL}/issues",
            ],
            "platform": sys.platform,
            "python": sys.version.split()[0],
            "troubleshooting": TROUBLESHOOTING_URL,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
