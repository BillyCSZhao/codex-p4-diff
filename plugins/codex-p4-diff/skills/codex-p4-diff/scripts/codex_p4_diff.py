#!/usr/bin/env python3
"""Read-only Perforce discovery and local Git history helpers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Iterable
import xml.etree.ElementTree as ET


TOOL_NAME = "codex-p4-diff"
SCHEMA_VERSION = 5
HISTORY_BRANCH = "codex/local-history"
READ_ONLY_P4_COMMANDS = frozenset({"info", "where", "fstat", "diff", "opened", "print"})
PRE_PUSH_HOOK_CONTENT = (
    "#!/bin/sh\n"
    "echo 'Codex P4 Diff history is local-only; Git push is blocked.' >&2\n"
    "exit 1\n"
)


class CodexP4DiffError(RuntimeError):
    """Raised for actionable configuration or safety errors."""


class ActionableP4ContextError(CodexP4DiffError):
    """Raised when a recognized project needs visible P4 configuration feedback."""


class AmbiguousP4ContextError(ActionableP4ContextError):
    """Raised when automatic discovery cannot choose one mapped P4 client."""


class OwnedHistoryContextError(ActionableP4ContextError):
    """Raised when an existing plugin-owned history identity cannot be validated."""


class ForeignGitBoundaryError(CodexP4DiffError):
    """Raised when ancestor discovery reaches a Git repository owned by another tool."""


def user_data_root() -> Path:
    if os.name == "nt":
        value = os.environ.get("LOCALAPPDATA")
        if value:
            return Path(value).expanduser()
    value = os.environ.get("XDG_STATE_HOME")
    if value:
        return Path(value).expanduser()
    return Path.home() / ".local" / "state"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8", errors="surrogatepass"))


def sanitize_component(value: Any, limit: int = 96) -> str:
    result = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip(".-")
    if not result:
        result = "unknown"
    if len(result) > limit:
        suffix = sha256_text(result)[:12]
        result = f"{result[: limit - 13]}-{suffix}"
    return result


def path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def p4_escape_filespec_path(value: str) -> str:
    return (
        value.replace("%", "%25")
        .replace("@", "%40")
        .replace("#", "%23")
        .replace("*", "%2A")
    )


def run_command(
    args: Iterable[str],
    *,
    cwd: Path | None = None,
    input_bytes: bytes | None = None,
    env: dict[str, str] | None = None,
    timeout_seconds: float | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    command = [str(item) for item in args]
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CodexP4DiffError(f"找不到命令：{command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CodexP4DiffError(
            f"命令超时 ({timeout_seconds:g}s)：{' '.join(command)}"
        ) from exc
    if check and completed.returncode != 0:
        message = (completed.stderr or completed.stdout).decode("utf-8", errors="replace").strip()
        raise CodexP4DiffError(
            f"命令失败 ({completed.returncode})：{' '.join(command)}"
            + (f"\n{message}" if message else "")
        )
    return completed


def resolve_executable(explicit: str | None, name: str, install_hint: str) -> str:
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            return str(path.resolve())
        resolved = shutil.which(explicit)
        if resolved:
            return resolved
        raise CodexP4DiffError(f"找不到 {name}：{explicit}。{install_hint}")
    else:
        resolved = shutil.which(name)
        if resolved:
            return resolved

    candidates: list[Path] = []
    if os.name == "nt":
        program_files = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")]
        if name == "p4":
            candidates.extend(Path(item) / "Perforce" / "p4.exe" for item in program_files if item)
        if name == "git":
            candidates.extend(Path(item) / "Git" / "cmd" / "git.exe" for item in program_files if item)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    raise CodexP4DiffError(f"找不到 {name}。{install_hint}")


def parse_tagged(output: bytes) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw_line in output.decode("utf-8", errors="replace").splitlines():
        if not raw_line.startswith("... "):
            continue
        remainder = raw_line[4:]
        key, separator, value = remainder.partition(" ")
        if not separator:
            continue
        if key in current and current:
            records.append(current)
            current = {}
        current[key] = value
    if current:
        records.append(current)
    return records


def p4_tagged(
    p4_exe: str,
    args: Iterable[str],
    *,
    cwd: Path,
    client: str | None = None,
    port: str | None = None,
    user: str | None = None,
    check: bool = True,
) -> list[dict[str, str]]:
    arguments = [str(item) for item in args]
    if not arguments or arguments[0] not in READ_ONLY_P4_COMMANDS:
        raise CodexP4DiffError(
            f"拒绝非只读 P4 命令：{arguments[0] if arguments else '<empty>'}"
        )
    command = [p4_exe, "-ztag"]
    if port:
        command.extend(["-p", port])
    if user:
        command.extend(["-u", user])
    if client:
        command.extend(["-c", client])
    command.extend(arguments)
    completed = run_command(command, cwd=cwd, timeout_seconds=20, check=check)
    return parse_tagged(completed.stdout)


def load_p4v_candidates(
    project_root: Path, connection_map: Path | None = None
) -> list[dict[str, str]]:
    map_path = (connection_map or (Path.home() / ".p4qt" / "connectionmap.xml")).expanduser()
    if not map_path.is_file():
        return []
    try:
        root = ET.parse(map_path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise CodexP4DiffError(f"无法读取 P4V 连接映射 {map_path}：{exc}") from exc

    project_text = str(project_root.resolve()).casefold().rstrip("\\/")
    candidates: list[dict[str, str]] = []
    exact_candidates: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in root.findall(".//ConnectionMap"):
        name = (entry.findtext("Name") or "").strip()
        port = (entry.findtext("P4Port") or "").strip()
        user = (entry.findtext("User") or "").strip()
        if not port:
            continue
        settings = map_path.parent / f"{name}Clients" / "WorkspaceSettings.xml"
        client_entries: list[tuple[str, list[str]]] = []
        if settings.is_file():
            try:
                settings_root = ET.parse(settings).getroot()
                client_list = settings_root.find(".//PropertyList[@varName='ClientList']")
                if client_list is not None:
                    for client_entry in client_list.findall("./PropertyList"):
                        client_name = (client_entry.get("varName") or "").strip()
                        if client_name:
                            values: list[str] = []
                            for property_list in settings_root.findall(".//PropertyList"):
                                if (property_list.get("varName") or "").strip() != client_name:
                                    continue
                                values.extend(
                                    str(item.text or "").strip()
                                    for item in property_list.findall(".//String")
                                )
                            client_entries.append((client_name, values))
            except (OSError, ET.ParseError):
                client_entries = []
        matching = [
            client_name
            for client_name, values in client_entries
            if any(
                value.casefold().rstrip("\\/") == project_text
                or value.casefold().startswith(project_text + os.sep.casefold())
                for value in values
                if value and value != "<client root>"
            )
        ]
        names = matching or [client_name for client_name, _values in client_entries]
        for client_name in names:
            key = (port.casefold(), user.casefold(), client_name.casefold())
            if key in seen:
                continue
            seen.add(key)
            candidate = {
                "port": port,
                "user": user,
                "client": client_name,
                "connectionMap": str(map_path.resolve()),
            }
            candidates.append(candidate)
            if client_name in matching:
                exact_candidates.append(candidate)
    return exact_candidates or candidates


def context_if_mapped(
    executable: str,
    project_root: Path,
    *,
    client: str | None,
    port: str | None,
    user: str | None,
    source: str,
) -> dict[str, Any] | None:
    info_records = p4_tagged(
        executable,
        ["info"],
        cwd=project_root,
        client=client,
        port=port,
        user=user,
        check=False,
    )
    if not info_records:
        return None
    info = info_records[0]
    selected_client = client or info.get("clientName")
    if not selected_client or selected_client in {"*unknown*", "unknown"}:
        return None
    filespec = p4_escape_filespec_path(str(project_root / "..."))
    where_records = p4_tagged(
        executable,
        ["where", filespec],
        cwd=project_root,
        client=selected_client,
        port=port,
        user=user,
        check=False,
    )
    mapped = next(
        (
            item
            for item in where_records
            if item.get("path") and not item.get("unmap") and not item.get("depotFile", "").startswith("-")
        ),
        None,
    )
    if mapped is None:
        return None
    client_root_value = info.get("clientRoot")
    client_root = Path(client_root_value).resolve() if client_root_value else project_root
    return {
        "p4Exe": executable,
        # serverAddress identifies the backing server but is not necessarily a
        # connectable broker/proxy endpoint. Ambient resolution may also come
        # from P4CONFIG, so persist only an endpoint explicitly used here.
        "p4Port": port,
        "p4User": user or info.get("userName") or os.environ.get("P4USER"),
        "client": selected_client,
        "clientRoot": str(client_root),
        "projectRoot": str(project_root),
        "serverAddress": info.get("serverAddress") or info.get("serverName") or "unknown-server",
        "userName": info.get("userName") or user or os.environ.get("P4USER") or "unknown-user",
        "source": source,
        "info": info,
        "where": mapped,
    }


def discover_p4_context(
    project_root: Path,
    *,
    p4_exe: str | None = None,
    client: str | None = None,
    port: str | None = None,
    user: str | None = None,
    connection_map: Path | None = None,
) -> dict[str, Any]:
    project_root = project_root.resolve()
    executable = resolve_executable(p4_exe, "p4", "请安装 Perforce Helix Core CLI 并完成登录。")
    direct = context_if_mapped(
        executable,
        project_root,
        client=client,
        port=port,
        user=user,
        source="explicit" if client or port or user else "environment",
    )
    if direct is not None:
        return direct
    if port or client:
        raise CodexP4DiffError(
            f"显式 P4 上下文没有映射项目目录：port={port or '<environment>'}, "
            f"client={client or '<environment>'}, project={project_root}"
        )

    matches: list[dict[str, Any]] = []
    for candidate in load_p4v_candidates(project_root, connection_map):
        resolved = context_if_mapped(
            executable,
            project_root,
            client=candidate["client"],
            port=candidate["port"],
            user=user or candidate["user"],
            source="p4v-workspace-settings",
        )
        if resolved is not None:
            resolved["connectionMap"] = candidate.get("connectionMap")
            matches.append(resolved)
    unique = {
        (str(item.get("p4Port")).casefold(), str(item.get("client")).casefold()): item
        for item in matches
    }
    if len(unique) == 1:
        return next(iter(unique.values()))
    if len(unique) > 1:
        choices = ", ".join(
            sorted(
                f"{item.get('p4Port')} / {item.get('client')}"
                for item in unique.values()
            )
        )
        raise AmbiguousP4ContextError(
            f"多个 P4V 工作区都能映射 {project_root}：{choices}。请显式传入 --port 和 --client。"
        )
    raise CodexP4DiffError(
        f"无法为 {project_root} 找到映射的 P4 client。请在 P4V 中打开该工作区，"
        "或显式传入 --port、--user 和 --client。"
    )


def project_key(context: dict[str, Any], project_root: Path) -> str:
    identity = "\0".join(
        [
            str(context.get("serverAddress") or ""),
            str(context.get("client") or ""),
            str(project_root.resolve()).casefold(),
        ]
    )
    return f"{sanitize_component(project_root.name, 48)}-{sha256_text(identity)[:12]}"


def storage_paths(context: dict[str, Any], project_root: Path) -> dict[str, Path]:
    root = user_data_root() / "CodexP4Diff"
    key = project_key(context, project_root)
    return {
        "root": root,
        "history": project_root.resolve(),
        "state": root / "State" / key,
    }


def git_output(git_exe: str, repo: Path, args: Iterable[str], *, check: bool = True) -> str:
    completed = run_command([git_exe, "-C", str(repo), *args], check=check)
    return completed.stdout.decode("utf-8", errors="replace").strip()


def git_metadata_dir(git_exe: str, project_root: Path) -> Path:
    value = git_output(git_exe, project_root, ["rev-parse", "--absolute-git-dir"])
    return Path(value).resolve()


def install_info_exclude(git_dir: Path) -> None:
    exclude = git_dir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    begin = "# BEGIN codex-p4-diff managed untracked-file policy"
    end = "# END codex-p4-diff managed untracked-file policy"
    block = f"{begin}\n*\n{end}\n"
    current = exclude.read_text(encoding="utf-8", errors="replace") if exclude.is_file() else ""
    if begin in current and end in current:
        prefix, remainder = current.split(begin, 1)
        _old, suffix = remainder.split(end, 1)
        updated = prefix.rstrip("\n") + ("\n" if prefix.strip() else "") + block + suffix.lstrip("\n")
    else:
        updated = current.rstrip("\n") + ("\n" if current.strip() else "") + block
    if current != updated:
        exclude.write_text(updated, encoding="utf-8", newline="\n")


def owned_repo_marker(git_dir: Path) -> Path:
    return git_dir / TOOL_NAME / "project.json"


def load_owned_history_marker(history_root: Path) -> dict[str, Any] | None:
    history_root = history_root.resolve()
    git_entry = history_root / ".git"
    if not git_entry.is_dir():
        return None
    marker_path = owned_repo_marker(git_entry)
    if not marker_path.exists():
        return None
    if not marker_path.is_file():
        raise OwnedHistoryContextError(f"本地历史身份标记不是文件：{marker_path}")
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OwnedHistoryContextError(f"无法读取本地历史身份标记 {marker_path}：{exc}") from exc
    if not isinstance(marker, dict) or marker.get("generatedBy") != TOOL_NAME:
        raise OwnedHistoryContextError(f"本地历史身份标记不属于 {TOOL_NAME}：{marker_path}")

    marker_root_text = str(marker.get("projectRoot") or "").strip()
    if not marker_root_text:
        raise OwnedHistoryContextError(f"本地历史身份标记缺少 projectRoot：{marker_path}")
    try:
        marker_root = Path(marker_root_text).resolve()
    except (OSError, RuntimeError) as exc:
        raise OwnedHistoryContextError(
            f"本地历史身份标记包含无效 projectRoot：{marker_root_text}"
        ) from exc
    if marker_root != history_root:
        raise OwnedHistoryContextError(
            f"本地历史身份标记属于另一个项目：marker={marker_root}, history={history_root}"
        )
    if str(marker.get("branch") or "") != HISTORY_BRANCH:
        raise OwnedHistoryContextError(
            f"本地历史身份标记分支无效：{marker.get('branch') or '<missing>'}"
        )
    if not str(marker.get("client") or "").strip():
        raise OwnedHistoryContextError(f"本地历史身份标记缺少 P4 client：{marker_path}")
    if not str(marker.get("serverAddress") or "").strip():
        raise OwnedHistoryContextError(f"本地历史身份标记缺少 P4 serverAddress：{marker_path}")
    return marker


def owned_legacy_git_pointer_target(project_root: Path) -> Path | None:
    pointer = project_root.resolve() / ".git"
    if not pointer.is_file():
        return None
    try:
        text = pointer.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        raise CodexP4DiffError(f"无法读取旧 .git 指针：{pointer}") from exc
    if not text.lower().startswith("gitdir:"):
        return None
    target = Path(text.split(":", 1)[1].strip())
    if not target.is_absolute():
        target = (project_root / target).resolve()
    owned_roots = [
        user_data_root() / "CodexP4Diff" / "Native",
        user_data_root() / "CodexP4Diff" / "Sidecars",
        user_data_root() / "CodexP4Diff" / "Audit",
    ]
    return target if any(path_is_within(target, root) for root in owned_roots) else None


def find_owned_project(start: Path) -> tuple[Path, dict[str, Any] | None] | None:
    current = start.resolve()
    for candidate in (current, *current.parents):
        git_entry = candidate / ".git"
        if not git_entry.exists():
            continue
        marker = load_owned_history_marker(candidate)
        if marker is not None:
            return candidate, marker
        if owned_legacy_git_pointer_target(candidate) is not None:
            return candidate, None
        raise ForeignGitBoundaryError(
            f"在 {candidate} 遇到非 {TOOL_NAME} 所有的 .git，"
            "不会继续向外查找或初始化本地历史。"
        )
    return None


def resolve_project_p4_context(
    start: Path,
    *,
    p4_exe: str | None = None,
    client: str | None = None,
    port: str | None = None,
    user: str | None = None,
    connection_map: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    requested_root = start.resolve()
    owned = find_owned_project(requested_root)
    project_root = owned[0] if owned is not None else requested_root
    marker = owned[1] if owned is not None else None
    owned_legacy_pointer = owned is not None and marker is None

    marker_client = str(marker.get("client") or "").strip() if marker else ""
    marker_port = str(marker.get("p4Port") or "").strip() if marker else ""
    marker_server = str(marker.get("serverAddress") or "").strip() if marker else ""
    marker_user = (
        str(marker.get("p4User") or marker.get("userName") or "").strip() if marker else ""
    )
    if marker is not None and client and client.casefold() != marker_client.casefold():
        raise OwnedHistoryContextError(
            f"已有本地历史绑定 P4 client {marker_client}，"
            f"显式参数请求的是 {client}；拒绝改变历史归属。"
        )
    if (
        marker is not None
        and marker_port
        and port
        and port.casefold() != marker_port.casefold()
    ):
        raise OwnedHistoryContextError(
            f"已有本地历史绑定 P4 port {marker_port}，"
            f"显式参数请求的是 {port}；拒绝改变历史归属。"
        )

    if marker is None:
        try:
            context = discover_p4_context(
                project_root,
                p4_exe=p4_exe,
                client=client,
                port=port,
                user=user,
                connection_map=connection_map,
            )
        except CodexP4DiffError as exc:
            if not owned_legacy_pointer:
                raise
            raise OwnedHistoryContextError(
                f"已识别的 {TOOL_NAME} 旧版 .git 指针无法验证 P4 上下文："
                f"project={project_root}；{exc}"
            ) from exc
        return project_root, context

    attempts: list[tuple[str, str | None, str | None, str | None]] = []
    effective_user = user or marker_user or None
    if port:
        attempts.append(("explicit", port, effective_user, None))
    elif marker_port:
        attempts.append(("owned-marker-p4Port", marker_port, effective_user, None))
    else:
        # 0.5.4 markers stored serverAddress but not the client connection endpoint.
        attempts.append(("environment", None, effective_user, None))
        if marker_server:
            attempts.append(("legacy-marker-serverAddress", marker_server, effective_user, None))

    failures: list[str] = []
    seen_attempts: set[tuple[str, str]] = set()

    def try_owned_context(
        source: str,
        attempt_port: str | None,
        attempt_user: str | None,
        connection_map_text: str | None,
    ) -> dict[str, Any] | None:
        key = ((attempt_port or "<environment>").casefold(), (attempt_user or "").casefold())
        if key in seen_attempts:
            return None
        seen_attempts.add(key)
        try:
            candidate = discover_p4_context(
                project_root,
                p4_exe=p4_exe,
                client=marker_client,
                port=attempt_port,
                user=attempt_user,
                connection_map=connection_map,
            )
        except CodexP4DiffError as exc:
            failures.append(f"{source}: {exc}")
            return None
        resolved_client = str(candidate.get("client") or "").strip()
        resolved_server = str(candidate.get("serverAddress") or "").strip()
        if resolved_client.casefold() != marker_client.casefold():
            failures.append(
                f"{source}: client={resolved_client or '<missing>'}, expected={marker_client}"
            )
            return None
        if (
            marker_server
            and resolved_server.casefold() != marker_server.casefold()
        ):
            failures.append(
                f"{source}: server={resolved_server or '<missing>'}, expected={marker_server}"
            )
            return None
        candidate["source"] = "owned-history-marker"
        candidate["historyMarker"] = str(owned_repo_marker(project_root / ".git"))
        if connection_map_text:
            candidate["connectionMap"] = connection_map_text
        return candidate

    for attempt in attempts:
        context = try_owned_context(*attempt)
        if context is not None:
            return project_root, context

    if not port and not marker_port:
        try:
            p4v_candidates = load_p4v_candidates(project_root, connection_map)
        except CodexP4DiffError as exc:
            failures.append(f"p4v-workspace-settings: {exc}")
            p4v_candidates = []
        for candidate in p4v_candidates:
            if str(candidate.get("client") or "").casefold() != marker_client.casefold():
                continue
            context = try_owned_context(
                "p4v-workspace-settings",
                str(candidate.get("port") or "") or None,
                effective_user or str(candidate.get("user") or "") or None,
                candidate.get("connectionMap"),
            )
            if context is not None:
                return project_root, context

    detail = "；".join(failures[-4:]) if failures else "没有可验证的只读连接入口"
    raise OwnedHistoryContextError(
        f"已有本地历史绑定的 P4 上下文无法验证：client={marker_client}, "
        f"server={marker_server or '<missing>'}, project={project_root}；{detail}"
    )


def inspect_history_repo(
    context: dict[str, Any],
    project_root: Path,
    *,
    git_exe: str | None = None,
    require_push_blocker: bool = True,
) -> dict[str, Any]:
    executable = resolve_executable(git_exe, "git", "请安装 Git 2.x。")
    history_root = project_root.resolve()
    pointer = history_root / ".git"
    if not pointer.is_dir():
        raise CodexP4DiffError(f"项目缺少可验证的本地 Git 历史：{pointer}")

    top_level = Path(
        git_output(executable, history_root, ["rev-parse", "--show-toplevel"])
    ).resolve()
    if top_level != history_root:
        raise CodexP4DiffError(
            f"Git 顶层目录不是 P4 项目根，拒绝使用：{top_level}"
        )
    git_dir = git_metadata_dir(executable, history_root)
    marker_path = owned_repo_marker(git_dir)
    marker = load_owned_history_marker(history_root)
    if marker is None:
        raise CodexP4DiffError(
            f"项目已有 Git 仓库但缺少 {TOOL_NAME} 所有权标记，拒绝接管：{marker_path}"
        )
    marker_client = str(marker.get("client") or "").casefold()
    context_client = str(context.get("client") or "").casefold()
    if marker_client != context_client:
        raise OwnedHistoryContextError(
            f"项目本地历史已绑定 P4 client {marker.get('client')}，拒绝改用 {context.get('client')}。"
        )
    marker_server = str(marker.get("serverAddress") or "").casefold()
    context_server = str(context.get("serverAddress") or "").casefold()
    if marker_server and context_server and marker_server != context_server:
        raise OwnedHistoryContextError(
            f"项目本地历史已绑定 P4 server {marker.get('serverAddress')}，"
            f"拒绝改用 {context.get('serverAddress')}。"
        )

    branch = git_output(executable, history_root, ["symbolic-ref", "--short", "HEAD"], check=False)
    if branch != HISTORY_BRANCH:
        raise CodexP4DiffError(
            f"本地历史必须停留在固定分支 {HISTORY_BRANCH}，当前为 {branch or '<detached>'}；"
            "插件不会自动切换分支。"
        )
    remotes = git_output(executable, history_root, ["remote"], check=False).splitlines()
    if remotes:
        raise CodexP4DiffError(
            f"本地历史仓库不允许配置 Git remote，请先移除：{', '.join(remotes)}"
        )
    push_hook = git_dir / "hooks" / "pre-push"
    if require_push_blocker:
        try:
            push_hook_content = push_hook.read_text(encoding="utf-8")
        except OSError as exc:
            raise CodexP4DiffError(f"本地历史缺少 pre-push 发布阻断：{push_hook}") from exc
        if push_hook_content != PRE_PUSH_HOOK_CONTENT:
            raise CodexP4DiffError(f"本地历史 pre-push 发布阻断内容无效：{push_hook}")
        if os.name != "nt" and not os.access(push_hook, os.X_OK):
            raise CodexP4DiffError(f"本地历史 pre-push 发布阻断不可执行：{push_hook}")

    paths = storage_paths(context, history_root)
    return {
        "gitExe": executable,
        "gitDir": git_dir,
        "historyRoot": history_root,
        "stateRoot": paths["state"],
        "branch": HISTORY_BRANCH,
        "baseCommit": str(marker.get("initialCommit") or ""),
        "headCommit": git_output(executable, history_root, ["rev-parse", "HEAD"]),
    }


def ensure_history_repo(
    context: dict[str, Any],
    project_root: Path,
    *,
    git_exe: str | None = None,
) -> dict[str, Any]:
    executable = resolve_executable(git_exe, "git", "请安装 Git 2.x。")
    paths = storage_paths(context, project_root)
    history_root = project_root.resolve()
    state_root = paths["state"]
    state_root.mkdir(parents=True, exist_ok=True)

    pointer = history_root / ".git"
    if pointer.is_file():
        migration = detach_legacy_git_pointer(history_root)
        if not migration.get("removed"):
            raise CodexP4DiffError(
                f"项目已有非本工具拥有的 .git 指针，拒绝接管：{pointer}"
            )

    if not pointer.exists():
        enclosing = git_output(
            executable,
            history_root,
            ["rev-parse", "--show-toplevel"],
            check=False,
        )
        if enclosing:
            raise CodexP4DiffError(
                f"项目位于现有 Git 仓库 {Path(enclosing).resolve()} 内，拒绝创建嵌套历史仓库。"
            )
        init = run_command([executable, "init", "-b", HISTORY_BRANCH, str(history_root)], check=False)
        if init.returncode != 0:
            run_command([executable, "init", str(history_root)])
            git_output(
                executable,
                history_root,
                ["symbolic-ref", "HEAD", f"refs/heads/{HISTORY_BRANCH}"],
            )
        git_output(executable, history_root, ["config", "user.name", "Codex P4 Diff"])
        git_output(executable, history_root, ["config", "user.email", "codex-p4-diff@local.invalid"])
        git_dir = git_metadata_dir(executable, history_root)
        marker_path = owned_repo_marker(git_dir)
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker = {
            "schemaVersion": SCHEMA_VERSION,
            "generatedBy": TOOL_NAME,
            "projectRoot": str(project_root.resolve()),
            "serverAddress": context.get("serverAddress"),
            "p4Port": context.get("p4Port"),
            "p4User": context.get("p4User"),
            "client": context.get("client"),
            "branch": HISTORY_BRANCH,
            "createdFor": "project-local Codex turn history; never push",
        }
        marker_path.write_text(
            json.dumps(marker, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        install_info_exclude(git_dir)
        run_command(
            [
                executable,
                "-C",
                str(history_root),
                "commit",
                "--allow-empty",
                "-m",
                "Initialize Codex local history",
            ],
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "Codex P4 Diff",
                "GIT_AUTHOR_EMAIL": "codex-p4-diff@local.invalid",
                "GIT_COMMITTER_NAME": "Codex P4 Diff",
                "GIT_COMMITTER_EMAIL": "codex-p4-diff@local.invalid",
            },
        )
        marker["initialCommit"] = git_output(executable, history_root, ["rev-parse", "HEAD"])
        marker_path.write_text(
            json.dumps(marker, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    history = inspect_history_repo(
        context,
        history_root,
        git_exe=executable,
        require_push_blocker=False,
    )
    install_info_exclude(Path(history["gitDir"]))
    install_pre_push_hook_with_exe(executable, history_root)
    history.pop("gitDir", None)
    return history


def install_pre_push_hook_with_exe(git_exe: str, history_root: Path) -> None:
    git_dir = Path(git_output(git_exe, history_root, ["rev-parse", "--absolute-git-dir"]))
    hooks = git_dir / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-push"
    content = PRE_PUSH_HOOK_CONTENT
    if not hook.exists() or hook.read_text(encoding="utf-8", errors="replace") != content:
        hook.write_text(content, encoding="utf-8", newline="\n")
    try:
        hook.chmod(0o755)
    except OSError:
        pass


def detach_legacy_git_pointer(project_root: Path) -> dict[str, Any]:
    pointer = project_root.resolve() / ".git"
    if not pointer.exists() or pointer.is_dir():
        return {"removed": False, "reason": "none-or-real-git"}
    try:
        text = pointer.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        raise CodexP4DiffError(f"无法读取旧 .git 指针：{pointer}") from exc
    if not text.lower().startswith("gitdir:"):
        return {"removed": False, "reason": "unknown-pointer"}
    target_text = text.split(":", 1)[1].strip()
    target = Path(target_text)
    if not target.is_absolute():
        target = (project_root / target).resolve()
    if owned_legacy_git_pointer_target(project_root) is None:
        return {"removed": False, "reason": "foreign-pointer", "target": str(target)}
    pointer.unlink()
    return {"removed": True, "reason": "legacy-native-review", "target": str(target)}


def readonly_fstat(context: dict[str, Any], path: Path) -> dict[str, Any] | None:
    records = p4_tagged(
        str(context["p4Exe"]),
        [
            "fstat",
            "-Ol",
            "-T",
            "depotFile,clientFile,haveRev,headRev,digest,fileSize,type,action,headAction",
            p4_escape_filespec_path(str(path.resolve())),
        ],
        cwd=Path(str(context["projectRoot"])),
        client=str(context["client"]),
        port=str(context.get("p4Port") or "") or None,
        user=str(context.get("p4User") or "") or None,
        check=False,
    )
    if not records:
        return None
    record = records[0]
    return {key: value for key, value in record.items() if key != "code"}


def doctor_report(
    project_root: Path,
    *,
    p4_exe: str | None = None,
    git_exe: str | None = None,
    client: str | None = None,
    port: str | None = None,
    user: str | None = None,
    connection_map: Path | None = None,
    initialize: bool = True,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedBy": TOOL_NAME,
        "projectRoot": str(project_root.resolve()),
        "p4Mode": "read-only",
        "p4Writes": False,
        "ready": False,
        "checks": [],
    }
    try:
        resolved_root, context = resolve_project_p4_context(
            project_root,
            p4_exe=p4_exe,
            client=client,
            port=port,
            user=user,
            connection_map=connection_map,
        )
        project_root = resolved_root
        report["projectRoot"] = str(project_root)
        report["context"] = {
            key: context[key]
            for key in ("serverAddress", "userName", "client", "clientRoot", "projectRoot")
        }
        report["checks"].append({"name": "p4-readonly-context", "ok": True})
        if initialize:
            history = ensure_history_repo(context, project_root, git_exe=git_exe)
        else:
            history = inspect_history_repo(context, project_root, git_exe=git_exe)
        report["historyRoot"] = str(history["historyRoot"])
        report["baseCommit"] = history["baseCommit"]
        report["branch"] = HISTORY_BRANCH
        report["checks"].append({"name": "project-local-git-history", "ok": True})
        pointer = project_root.resolve() / ".git"
        report["legacyGitPointer"] = str(pointer) if pointer.is_file() else None
        report["checks"].append(
            {
                "name": "project-git-root",
                "ok": pointer.is_dir(),
                "hint": "运行 --detach-legacy-review 移除旧版工具拥有的 .git 指针。"
                if pointer.is_file()
                else None,
            }
        )
        report["ready"] = all(item["ok"] for item in report["checks"])
    except CodexP4DiffError as exc:
        report["error"] = str(exc)
        report["checks"].append({"name": "configuration", "ok": False, "hint": str(exc)})
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record Codex turn changes as local Git commits without modifying Perforce."
    )
    parser.add_argument("--workspace", default=os.getcwd())
    parser.add_argument("--project-root")
    parser.add_argument("--p4")
    parser.add_argument("--git")
    parser.add_argument("--client")
    parser.add_argument("--port")
    parser.add_argument("--user")
    parser.add_argument("--p4v-connection-map")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--doctor", action="store_true")
    mode.add_argument("--status", action="store_true")
    mode.add_argument("--history-path", action="store_true")
    mode.add_argument("--detach-legacy-review", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    project_root = Path(args.project_root or args.workspace).expanduser().resolve()
    if not project_root.is_dir():
        print(json.dumps({"error": f"项目目录不存在：{project_root}"}, ensure_ascii=False))
        return 2
    try:
        if args.detach_legacy_review:
            result = detach_legacy_git_pointer(project_root)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        report = doctor_report(
            project_root,
            p4_exe=args.p4,
            git_exe=args.git,
            client=args.client,
            port=args.port,
            user=args.user,
            connection_map=Path(args.p4v_connection_map).resolve()
            if args.p4v_connection_map
            else None,
            initialize=True,
        )
        if args.history_path and report.get("historyRoot"):
            print(report["historyRoot"])
        else:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report.get("ready") else 2
    except CodexP4DiffError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
