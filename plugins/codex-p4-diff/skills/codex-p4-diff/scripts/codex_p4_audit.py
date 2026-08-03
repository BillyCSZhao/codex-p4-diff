#!/usr/bin/env python3
"""Codex lifecycle audit and pending changelist ownership for P4 workspaces."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import datetime as dt
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Iterable

import codex_p4_diff as bridge


AUDIT_SCHEMA_VERSION = bridge.AUDIT_SCHEMA_VERSION
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
MAX_DESCRIPTION_CHARS = 7000
MANAGED_MARKER = "Managed-By: codex-p4-diff"
PATCH_PATH_PATTERN = re.compile(
    r"^\*\*\* (?:Update|Add|Delete) File:\s*(.+?)\s*$|^\*\*\* Move to:\s*(.+?)\s*$",
    re.MULTILINE,
)
MUTATING_SHELL_PATTERN = re.compile(
    r"(?ix)(?:"
    r"\bSet-Content\b|\bAdd-Content\b|\bOut-File\b|\bCopy-Item\b|"
    r"\bMove-Item\b|\bRemove-Item\b|\bNew-Item\b|"
    r"\bsed\s+-[^\r\n]*i\b|\bperl\s+-[^\r\n]*pi\b|"
    r"\btee\b|\bcp\b|\bmv\b|\brm\b|\btouch\b|"
    r"\bclang-format\b[^\r\n]*\s-i\b|\bprettier\b[^\r\n]*--write\b|"
    r"\bdotnet\s+format\b|\bblack\b|\bruff\b[^\r\n]*--fix\b|"
    r"(?<![<>=])>(?![>&=])"
    r")"
)


class AuditError(RuntimeError):
    pass


class HookSkip(RuntimeError):
    pass


class OwnershipConflict(AuditError):
    pass


@dataclass(frozen=True)
class AuditContext:
    p4_exe: str
    project_root: Path
    client: str
    client_root: Path
    info: dict[str, str]
    connection: dict[str, str]
    session_id: str
    session_root: Path
    project_audit_root: Path


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.pending-{os.getpid()}-{time.time_ns()}")
    pending.write_text(text, encoding="utf-8", newline="\n")
    os.replace(pending, path)


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"审计文件无效：{path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AuditError(f"审计文件应为 JSON 对象：{path}")
    return value


@contextmanager
def project_lock(project_audit_root: Path):
    project_audit_root.mkdir(parents=True, exist_ok=True)
    lock_path = project_audit_root / ".audit.lock"
    deadline = time.monotonic() + 10.0
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, f"{os.getpid()} {utc_now()}\n".encode("ascii"))
        except FileExistsError:
            try:
                stale = time.time() - lock_path.stat().st_mtime > 120
            except OSError:
                stale = False
            if stale:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise AuditError(f"等待 Codex P4 审计锁超时：{lock_path}")
            time.sleep(0.1)
    try:
        yield
    finally:
        try:
            os.close(descriptor)
        finally:
            try:
                lock_path.unlink()
            except OSError:
                pass


def redact_summary(text: str, limit: int = 180) -> str:
    normalized = " ".join(text.split())
    normalized = re.sub(
        r"(?i)\b(api[_-]?key|token|password|secret)\s*[:=]\s*\S+",
        r"\1=<redacted>",
        normalized,
    )
    normalized = re.sub(r"(?i)\bBearer\s+\S+", "Bearer <redacted>", normalized)
    if len(normalized) > limit:
        return normalized[: limit - 3].rstrip() + "..."
    return normalized or "Codex 文件修改"


def normalize_session_id(value: Any) -> str:
    session_id = str(value or "").strip()
    if not session_id:
        raise HookSkip("缺少 Codex session_id")
    return session_id


def find_git_metadata(project_root: Path, client_root: Path) -> Path | None:
    candidate = project_root
    while bridge.is_inside(candidate, client_root):
        metadata = candidate / ".git"
        if metadata.exists():
            return metadata
        if candidate == client_root or candidate.parent == candidate:
            break
        candidate = candidate.parent
    return None


def validate_p4_mapping(ctx_root: Path, p4_exe: str, client: str) -> None:
    escaped = bridge.p4_escape_filespec_path(ctx_root)
    records = bridge.p4_tagged(
        p4_exe,
        ["where", escaped],
        client=client,
        record_key="depotFile",
        cwd=ctx_root,
    )
    if not any(record.get("path") and not record.get("unmap") for record in records):
        raise HookSkip(f"目录不在 P4 client 映射中：{ctx_root}")


def resolve_context(event: dict[str, Any]) -> AuditContext:
    cwd_text = str(event.get("cwd") or "").strip()
    if not cwd_text:
        raise HookSkip("Hook 输入没有 cwd")
    project_root = Path(cwd_text).resolve()
    if not project_root.is_dir():
        raise HookSkip(f"cwd 不存在：{project_root}")
    try:
        p4_exe = bridge.resolve_executable(
            None,
            "p4",
            "请安装 Perforce Helix Core CLI。",
        )
        client, info, connection = bridge.resolve_connection_and_client(
            p4_exe,
            project_root,
            None,
            None,
            None,
            None,
        )
    except bridge.CodexP4DiffError as exc:
        raise HookSkip(str(exc)) from exc
    client_root_text = info.get("clientRoot")
    if not client_root_text:
        raise HookSkip(f"P4 client {client!r} 没有 root")
    client_root = Path(client_root_text).resolve()
    bridge.require_inside(project_root, client_root, "Codex cwd")
    validate_p4_mapping(project_root, p4_exe, client)

    git_metadata = find_git_metadata(project_root, client_root)
    if git_metadata and git_metadata.is_dir():
        raise HookSkip("真实 Git 工作区不启用 Codex P4 会话 CL")
    if git_metadata and git_metadata.is_file():
        try:
            git_dir = bridge.parse_gitdir_pointer(git_metadata)
            bridge.validate_owned_gitdir(git_dir)
        except bridge.CodexP4DiffError as exc:
            raise HookSkip(str(exc)) from exc

    session_id = normalize_session_id(event.get("session_id"))
    session_root = bridge.audit_session_root(info, client, project_root, session_id)
    project_audit_root = bridge.audit_project_root(info, client, project_root)
    return AuditContext(
        p4_exe=p4_exe,
        project_root=project_root,
        client=client,
        client_root=client_root,
        info=info,
        connection=connection,
        session_id=session_id,
        session_root=session_root,
        project_audit_root=project_audit_root,
    )


def session_path(ctx: AuditContext) -> Path:
    return ctx.session_root / "session.json"


def base_session(ctx: AuditContext) -> dict[str, Any]:
    now = utc_now()
    return {
        "schemaVersion": AUDIT_SCHEMA_VERSION,
        "generatedBy": bridge.TOOL_NAME,
        "mode": "codex-session-pending-cl",
        "sessionId": ctx.session_id,
        "projectRoot": str(ctx.project_root),
        "serverAddress": ctx.info.get("serverAddress") or "",
        "userName": ctx.info.get("userName") or "",
        "client": ctx.client,
        "clientRoot": str(ctx.client_root),
        "connection": ctx.connection,
        "createdAtUtc": now,
        "lastUpdatedAtUtc": now,
        "status": "active",
        "changelist": None,
        "turns": [],
    }


def load_session(ctx: AuditContext) -> dict[str, Any]:
    path = session_path(ctx)
    session = read_json_if_exists(path) or base_session(ctx)
    expected = {
        "generatedBy": bridge.TOOL_NAME,
        "sessionId": ctx.session_id,
        "client": ctx.client,
        "serverAddress": ctx.info.get("serverAddress") or "",
    }
    for key, expected_value in expected.items():
        if str(session.get(key) or "").casefold() != str(expected_value).casefold():
            raise AuditError(
                f"会话账本上下文不匹配：{key}={session.get(key)!r}，预期 {expected_value!r}"
            )
    recorded_root = session.get("projectRoot")
    if not recorded_root or bridge.canonical(Path(str(recorded_root))) != bridge.canonical(ctx.project_root):
        raise AuditError("会话账本属于另一个项目根")
    return session


def save_session(ctx: AuditContext, session: dict[str, Any]) -> None:
    session["lastUpdatedAtUtc"] = utc_now()
    atomic_write_json(session_path(ctx), session)


def run_p4(
    ctx: AuditContext,
    args: list[str],
    *,
    input_text: str | None = None,
    allowed_codes: Iterable[int] = (0,),
) -> subprocess.CompletedProcess[bytes]:
    argv = [ctx.p4_exe, "-c", ctx.client, *args]
    try:
        completed = subprocess.run(
            argv,
            cwd=str(ctx.project_root),
            input=input_text.encode("utf-8") if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise AuditError(f"无法执行 P4：{exc}") from exc
    if completed.returncode not in set(allowed_codes):
        detail = "\n".join(
            value
            for value in (
                bridge.decode_output(completed.stdout).strip(),
                bridge.decode_output(completed.stderr).strip(),
            )
            if value
        )
        raise AuditError(
            f"P4 命令失败：{subprocess.list2cmdline(argv)}"
            + (f"\n{detail}" if detail else "")
        )
    return completed


def change_record(ctx: AuditContext, changelist: str) -> dict[str, str] | None:
    try:
        records = bridge.p4_tagged(
            ctx.p4_exe,
            ["change", "-o", changelist],
            client=ctx.client,
            record_key="Change",
            cwd=ctx.project_root,
        )
    except bridge.CodexP4DiffError:
        return None
    return records[0] if records else None


def validate_managed_change(ctx: AuditContext, session: dict[str, Any], changelist: str) -> bool:
    if not change_record(ctx, changelist):
        return False
    try:
        spec = bridge.decode_output(run_p4(ctx, ["change", "-o", changelist]).stdout)
    except AuditError:
        return False
    status = re.search(r"(?im)^Status:\s*(\S+)", spec)
    client = re.search(r"(?im)^Client:\s*(\S+)", spec)
    return bool(
        status
        and status.group(1).lower() == "pending"
        and client
        and client.group(1).casefold() == ctx.client.casefold()
        and MANAGED_MARKER in spec
        and f"Codex-Session: {ctx.session_id}" in spec
    )


def render_change_description(ctx: AuditContext, session: dict[str, Any]) -> str:
    title = redact_summary(str(session.get("promptSummary") or "Codex 文件修改"), 120)
    lines = [
        f"[Codex] {title}",
        "",
        MANAGED_MARKER,
        f"Codex-Session: {ctx.session_id}",
        f"Project: {ctx.project_root}",
        f"Created-At-UTC: {session.get('createdAtUtc') or utc_now()}",
        f"Audit-Path: {ctx.session_root}",
    ]
    turns = session.get("turns")
    if isinstance(turns, list) and turns:
        lines.extend(("", "Changes:"))
        for turn in turns[-20:]:
            if not isinstance(turn, dict):
                continue
            turn_id = str(turn.get("turnId") or "unknown")[:64]
            summary = redact_summary(str(turn.get("summary") or "Codex 文件修改"), 180)
            count = int(turn.get("fileCount") or 0)
            lines.append(f"- {turn_id}: {summary} ({count} files)")
    files = session.get("files")
    if isinstance(files, list) and files:
        lines.extend(("", "Files:"))
        for item in files[-80:]:
            if isinstance(item, dict):
                lines.append(f"- {item.get('action', 'edit')} {item.get('path', '')}")
    rendered = "\n".join(lines).strip()
    if len(rendered) > MAX_DESCRIPTION_CHARS:
        rendered = rendered[: MAX_DESCRIPTION_CHARS - 24].rstrip() + "\n... details truncated"
    return rendered


def create_pending_change(ctx: AuditContext, session: dict[str, Any]) -> str:
    description = render_change_description(ctx, session)
    user_name = ctx.info.get("userName") or ""
    spec_lines = [
        "Change: new",
        f"Client: {ctx.client}",
        f"User: {user_name}",
        "Status: new",
        "Description:",
        *[f"\t{line}" for line in description.splitlines()],
        "",
    ]
    completed = run_p4(ctx, ["change", "-i"], input_text="\n".join(spec_lines))
    output = bridge.decode_output(completed.stdout) + "\n" + bridge.decode_output(completed.stderr)
    match = re.search(r"\bChange\s+(\d+)\s+(?:created|updated)\b", output, re.IGNORECASE)
    if not match:
        raise AuditError(f"无法从 p4 change 输出解析变更列表编号：{output.strip()}")
    return match.group(1)


def ensure_changelist(ctx: AuditContext, session: dict[str, Any]) -> str:
    existing = str(session.get("changelist") or "")
    if existing.isdigit() and validate_managed_change(ctx, session, existing):
        return existing
    changelist = create_pending_change(ctx, session)
    session["changelist"] = changelist
    session["changelistCreatedAtUtc"] = utc_now()
    save_session(ctx, session)
    return changelist


def replace_description_in_spec(spec: str, description: str) -> str:
    newline = "\r\n" if "\r\n" in spec else "\n"
    lines = spec.replace("\r\n", "\n").split("\n")
    output: list[str] = []
    index = 0
    replaced = False
    while index < len(lines):
        line = lines[index]
        if line == "Description:":
            output.append(line)
            output.extend(f"\t{item}" for item in description.splitlines())
            index += 1
            while index < len(lines) and (lines[index].startswith("\t") or not lines[index]):
                index += 1
            replaced = True
            continue
        output.append(line)
        index += 1
    if not replaced:
        raise AuditError("p4 change spec 缺少 Description 字段")
    return newline.join(output).rstrip("\r\n") + newline


def update_change_description(ctx: AuditContext, session: dict[str, Any]) -> None:
    changelist = str(session.get("changelist") or "")
    if not changelist.isdigit():
        return
    if not validate_managed_change(ctx, session, changelist):
        raise AuditError(f"拒绝更新非本插件拥有的 changelist {changelist}")
    spec = bridge.decode_output(run_p4(ctx, ["change", "-o", changelist]).stdout)
    updated = replace_description_in_spec(spec, render_change_description(ctx, session))
    run_p4(ctx, ["change", "-i"], input_text=updated)


def parse_apply_patch_paths(command: str, cwd: Path, project_root: Path) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()
    for match in PATCH_PATH_PATTERN.finditer(command):
        raw = (match.group(1) or match.group(2) or "").strip().strip('"')
        if not raw or raw == "/dev/null":
            continue
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        candidate = candidate.resolve()
        bridge.require_inside(candidate, project_root, "apply_patch 文件")
        key = bridge.canonical(candidate)
        if key not in seen:
            seen.add(key)
            paths.append(candidate)
    return paths


def relative_path(ctx: AuditContext, path: Path) -> str:
    bridge.require_inside(path, ctx.project_root, "审计文件")
    return path.relative_to(ctx.project_root).as_posix()


def is_probably_binary(data: bytes) -> bool:
    if not data:
        return False
    sample = data[:8192]
    if b"\x00" in sample:
        if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
            return False
        return True
    try:
        sample.decode("utf-8")
        return False
    except UnicodeDecodeError:
        return True


def snapshot_metadata(path: Path, snapshot_path: Path | None = None) -> dict[str, Any]:
    if not path.exists() and not path.is_symlink():
        return {"exists": False, "sha256": None, "size": 0, "binary": False}
    if not path.is_file() and not path.is_symlink():
        return {"exists": True, "kind": "non-file", "sha256": None, "size": 0, "binary": True}
    size = path.stat().st_size
    digest = sha256_file(path)
    data: bytes | None = path.read_bytes() if size <= MAX_SNAPSHOT_BYTES else None
    binary = True if data is None else is_probably_binary(data)
    result: dict[str, Any] = {
        "exists": True,
        "sha256": digest,
        "size": size,
        "binary": binary,
    }
    if snapshot_path is not None and data is not None:
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_bytes(data)
        result["snapshotPath"] = str(snapshot_path)
    return result


def snapshot_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return bool(
        before.get("sha256") != after.get("sha256")
        or bool(before.get("exists")) != bool(after.get("exists"))
    )


def turn_root(ctx: AuditContext, turn_id: str) -> Path:
    return ctx.session_root / "turns" / bridge.sanitize_component(turn_id)


def before_manifest_path(ctx: AuditContext, turn_id: str) -> Path:
    return turn_root(ctx, turn_id) / "before.json"


def record_before_path(ctx: AuditContext, turn_id: str, path: Path) -> dict[str, Any]:
    manifest_path = before_manifest_path(ctx, turn_id)
    manifest = read_json_if_exists(manifest_path)
    entries = manifest.setdefault("files", {})
    if not isinstance(entries, dict):
        raise AuditError(f"before manifest files 字段无效：{manifest_path}")
    relative = relative_path(ctx, path)
    if relative in entries:
        return entries[relative]
    snapshot_path = turn_root(ctx, turn_id) / "before" / Path(relative)
    metadata = snapshot_metadata(path, snapshot_path)
    metadata["path"] = relative
    entries[relative] = metadata
    manifest.update(
        {
            "schemaVersion": AUDIT_SCHEMA_VERSION,
            "sessionId": ctx.session_id,
            "turnId": turn_id,
            "capturedAtUtc": utc_now(),
        }
    )
    atomic_write_json(manifest_path, manifest)
    return metadata


def record_before_from_have(ctx: AuditContext, turn_id: str, path: Path) -> dict[str, Any]:
    manifest_path = before_manifest_path(ctx, turn_id)
    manifest = read_json_if_exists(manifest_path)
    entries = manifest.setdefault("files", {})
    if not isinstance(entries, dict):
        raise AuditError(f"before manifest files 字段无效：{manifest_path}")
    relative = relative_path(ctx, path)
    if relative in entries:
        return entries[relative]

    snapshot_path = turn_root(ctx, turn_id) / "before" / Path(relative)
    fstat = fstat_record(ctx, path)
    have_revision = None
    if fstat:
        try:
            have_revision = int(fstat.get("haveRev") or 0)
        except ValueError:
            have_revision = None
    if fstat and have_revision and fstat.get("depotFile"):
        try:
            bridge.print_baseline(
                ctx.p4_exe,
                ctx.client,
                snapshot_path,
                str(fstat["depotFile"]),
                have_revision,
                ctx.project_root,
            )
        except bridge.CodexP4DiffError as exc:
            raise AuditError(str(exc)) from exc
        metadata = snapshot_metadata(snapshot_path)
        metadata["snapshotPath"] = str(snapshot_path)
    else:
        metadata = {"exists": False, "sha256": None, "size": 0, "binary": False}
    metadata["path"] = relative
    metadata["source"] = "p4-have"
    entries[relative] = metadata
    manifest.update(
        {
            "schemaVersion": AUDIT_SCHEMA_VERSION,
            "sessionId": ctx.session_id,
            "turnId": turn_id,
            "capturedAtUtc": utc_now(),
        }
    )
    atomic_write_json(manifest_path, manifest)
    return metadata


def opened_records(
    ctx: AuditContext, *, changelist: str | None = None, paths: list[Path] | None = None
) -> list[dict[str, str]]:
    args = ["opened"]
    if changelist:
        args.extend(["-c", changelist])
    if paths:
        args.extend(bridge.p4_escape_filespec_path(path) for path in paths)
    elif changelist:
        args.append(bridge.p4_escape_filespec_path(ctx.project_root / "..."))
    try:
        return bridge.p4_tagged(
            ctx.p4_exe,
            args,
            client=ctx.client,
            record_key="depotFile",
            allow_empty_error=True,
            cwd=ctx.project_root,
        )
    except bridge.CodexP4DiffError as exc:
        raise AuditError(str(exc)) from exc


def opened_record_for_path(ctx: AuditContext, path: Path) -> dict[str, str] | None:
    records = opened_records(ctx, paths=[path])
    return records[0] if records else None


def client_file_from_record(ctx: AuditContext, record: dict[str, str]) -> Path:
    client_file = record.get("clientFile")
    if client_file:
        return Path(client_file).resolve()
    depot_file = record.get("depotFile")
    if not depot_file:
        raise AuditError(f"P4 opened 记录缺少路径：{record}")
    try:
        return bridge.resolve_where(ctx.p4_exe, ctx.client, depot_file, ctx.project_root)
    except bridge.CodexP4DiffError as exc:
        raise AuditError(str(exc)) from exc


def assert_file_ownership(
    ctx: AuditContext, path: Path, changelist: str
) -> dict[str, str] | None:
    record = opened_record_for_path(ctx, path)
    if not record:
        return None
    owner = str(record.get("change") or "default")
    if owner != changelist:
        raise OwnershipConflict(
            f"{relative_path(ctx, path)} 已属于 changelist {owner}，"
            f"当前 Codex 会话 CL 为 {changelist}。为避免合并不同会话，已拒绝修改；"
            "请先明确移交该文件。"
        )
    return record


def preflight_existing_ownership(
    ctx: AuditContext, paths: list[Path], session_changelist: str
) -> None:
    for path in paths:
        record = opened_record_for_path(ctx, path)
        if not record:
            continue
        owner = str(record.get("change") or "default")
        if session_changelist.isdigit() and owner == session_changelist:
            continue
        raise OwnershipConflict(
            f"{relative_path(ctx, path)} 已属于 changelist {owner}，"
            "当前 Codex 会话不能接管该文件。为避免合并不同会话，已拒绝修改；"
            "请先明确移交该文件。"
        )


def fstat_record(ctx: AuditContext, path: Path) -> dict[str, str] | None:
    try:
        records = bridge.p4_tagged(
            ctx.p4_exe,
            ["fstat", "-T", "depotFile,clientFile,haveRev,headRev,type", bridge.p4_escape_filespec_path(path)],
            client=ctx.client,
            record_key="depotFile",
            allow_empty_error=True,
            cwd=ctx.project_root,
        )
    except bridge.CodexP4DiffError as exc:
        text = str(exc).lower()
        if "no such file" in text or "not in client view" in text:
            return None
        raise AuditError(str(exc)) from exc
    return records[0] if records else None


def open_for_edit_if_needed(ctx: AuditContext, path: Path, changelist: str) -> None:
    if assert_file_ownership(ctx, path, changelist):
        return
    if not path.exists():
        return
    if fstat_record(ctx, path):
        run_p4(ctx, ["edit", "-c", changelist, bridge.p4_escape_filespec_path(path)])


def reconcile_paths(ctx: AuditContext, changelist: str, paths: list[Path]) -> None:
    if not paths:
        return
    for path in paths:
        assert_file_ownership(ctx, path, changelist)
    args = ["reconcile", "-c", changelist, "-M"]
    args.extend(bridge.p4_escape_filespec_path(path) for path in paths)
    run_p4(ctx, args)
    for path in paths:
        record = opened_record_for_path(ctx, path)
        if record:
            assert_file_ownership(ctx, path, changelist)


def tool_record_path(ctx: AuditContext, turn_id: str, tool_use_id: str) -> Path:
    return turn_root(ctx, turn_id) / "tools" / f"{bridge.sanitize_component(tool_use_id)}.json"


def save_tool_record(
    ctx: AuditContext, turn_id: str, tool_use_id: str, value: dict[str, Any]
) -> None:
    value.update(
        {
            "schemaVersion": AUDIT_SCHEMA_VERSION,
            "sessionId": ctx.session_id,
            "turnId": turn_id,
            "toolUseId": tool_use_id,
        }
    )
    atomic_write_json(tool_record_path(ctx, turn_id, tool_use_id), value)


def prepare_apply_patch(ctx: AuditContext, event: dict[str, Any]) -> str:
    turn_id = str(event.get("turn_id") or "unknown")
    tool_use_id = str(event.get("tool_use_id") or f"tool-{time.time_ns()}")
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    command = str(tool_input.get("command") or "")
    paths = parse_apply_patch_paths(command, ctx.project_root, ctx.project_root)
    if not paths:
        raise AuditError("无法从 apply_patch 输入识别目标文件，已拒绝未归属的文件修改。")
    with project_lock(ctx.project_audit_root):
        session = load_session(ctx)
        preflight_existing_ownership(ctx, paths, str(session.get("changelist") or ""))
        changelist = ensure_changelist(ctx, session)
        for path in paths:
            record_before_path(ctx, turn_id, path)
            open_for_edit_if_needed(ctx, path, changelist)
        save_tool_record(
            ctx,
            turn_id,
            tool_use_id,
            {
                "event": "PreToolUse",
                "toolName": "apply_patch",
                "preparedAtUtc": utc_now(),
                "changelist": changelist,
                "paths": [relative_path(ctx, path) for path in paths],
            },
        )
        save_session(ctx, session)
    return changelist


def finish_apply_patch(ctx: AuditContext, event: dict[str, Any]) -> str | None:
    turn_id = str(event.get("turn_id") or "unknown")
    tool_use_id = str(event.get("tool_use_id") or "unknown")
    record_path = tool_record_path(ctx, turn_id, tool_use_id)
    record = read_json_if_exists(record_path)
    if not record:
        return None
    with project_lock(ctx.project_audit_root):
        session = load_session(ctx)
        changelist = str(session.get("changelist") or "")
        if not changelist.isdigit():
            raise AuditError("apply_patch 完成后找不到当前会话 CL")
        paths = [(ctx.project_root / Path(item)).resolve() for item in record.get("paths", [])]
        reconcile_paths(ctx, changelist, paths)
        record["event"] = "PostToolUse"
        record["completedAtUtc"] = utc_now()
        record["toolResponse"] = redact_summary(json.dumps(event.get("tool_response"), ensure_ascii=False), 300)
        atomic_write_json(record_path, record)
        save_session(ctx, session)
    return changelist


def likely_mutating_shell(command: str) -> bool:
    normalized = re.sub(
        r"(?ix)(?:\d*>\s*(?:\$null\b|NUL\b|/dev/null\b)|\d*>&\d)",
        "",
        command,
    )
    return bool(MUTATING_SHELL_PATTERN.search(normalized))


def reconcile_preview(ctx: AuditContext) -> list[Path]:
    args = [
        ctx.p4_exe,
        "-c",
        ctx.client,
        "-ztag",
        "reconcile",
        "-n",
        "-e",
        "-a",
        "-d",
        bridge.p4_escape_filespec_path(ctx.project_root / "..."),
    ]
    completed = bridge.run_command(args, cwd=ctx.project_root, allowed_codes=(0, 1))
    records = bridge.parse_ztag(completed.stdout, "depotFile")
    paths: list[Path] = []
    for record in records:
        client_file = record.get("clientFile") or record.get("path")
        if not client_file:
            continue
        path = Path(client_file).resolve()
        if bridge.is_inside(path, ctx.project_root):
            paths.append(path)
    return paths


def snapshot_session_files(ctx: AuditContext, session: dict[str, Any], turn_id: str) -> None:
    changelist = str(session.get("changelist") or "")
    if not changelist.isdigit():
        return
    for record in opened_records(ctx, changelist=changelist):
        path = client_file_from_record(ctx, record)
        if bridge.is_inside(path, ctx.project_root):
            record_before_path(ctx, turn_id, path)


def prepare_shell(ctx: AuditContext, event: dict[str, Any]) -> None:
    turn_id = str(event.get("turn_id") or "unknown")
    tool_use_id = str(event.get("tool_use_id") or f"tool-{time.time_ns()}")
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    command = str(tool_input.get("command") or "")
    if not likely_mutating_shell(command):
        return
    with project_lock(ctx.project_audit_root):
        session = load_session(ctx)
        preexisting = reconcile_preview(ctx)
        if preexisting:
            names = ", ".join(relative_path(ctx, path) for path in preexisting[:8])
            raise OwnershipConflict(
                "写命令执行前检测到未归属的 workspace 改动，无法安全判断本命令的责任边界："
                + names
            )
        snapshot_session_files(ctx, session, turn_id)
        save_tool_record(
            ctx,
            turn_id,
            tool_use_id,
            {
                "event": "PreToolUse",
                "toolName": "Bash",
                "preparedAtUtc": utc_now(),
                "mutating": True,
                "commandSha256": sha256_bytes(command.encode("utf-8")),
            },
        )
        save_session(ctx, session)


def finish_shell(ctx: AuditContext, event: dict[str, Any]) -> str | None:
    turn_id = str(event.get("turn_id") or "unknown")
    tool_use_id = str(event.get("tool_use_id") or "unknown")
    record_path = tool_record_path(ctx, turn_id, tool_use_id)
    record = read_json_if_exists(record_path)
    if not record or not record.get("mutating"):
        return None
    with project_lock(ctx.project_audit_root):
        session = load_session(ctx)
        paths = reconcile_preview(ctx)
        if not paths:
            record["completedAtUtc"] = utc_now()
            record["paths"] = []
            atomic_write_json(record_path, record)
            save_session(ctx, session)
            return None
        changelist = ensure_changelist(ctx, session)
        for path in paths:
            record_before_from_have(ctx, turn_id, path)
        reconcile_paths(ctx, changelist, paths)
        record["completedAtUtc"] = utc_now()
        record["paths"] = [relative_path(ctx, path) for path in paths]
        atomic_write_json(record_path, record)
        save_session(ctx, session)
        return changelist


def decode_text(data: bytes) -> tuple[str, str] | None:
    for encoding in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return None


def build_file_diff(relative: str, before: dict[str, Any], path: Path) -> str:
    before_snapshot = before.get("snapshotPath")
    before_data = Path(str(before_snapshot)).read_bytes() if before_snapshot and Path(str(before_snapshot)).is_file() else b""
    after_data = path.read_bytes() if path.is_file() and path.stat().st_size <= MAX_SNAPSHOT_BYTES else b""
    if before.get("binary") or (after_data and is_probably_binary(after_data)):
        return ""
    before_text = decode_text(before_data)
    after_text = decode_text(after_data)
    if before_text is None or after_text is None:
        return ""
    return "".join(
        difflib.unified_diff(
            before_text[0].splitlines(keepends=True),
            after_text[0].splitlines(keepends=True),
            fromfile=f"a/{relative}" if before.get("exists") else "/dev/null",
            tofile=f"b/{relative}" if path.exists() else "/dev/null",
        )
    )


def finalize_turn(ctx: AuditContext, event: dict[str, Any]) -> dict[str, Any] | None:
    turn_id = str(event.get("turn_id") or "unknown")
    with project_lock(ctx.project_audit_root):
        session = load_session(ctx)
        changelist = str(session.get("changelist") or "")
        if not changelist.isdigit():
            return None
        before_manifest = read_json_if_exists(before_manifest_path(ctx, turn_id))
        before_files = before_manifest.get("files") if isinstance(before_manifest.get("files"), dict) else {}
        current_records = opened_records(ctx, changelist=changelist)
        current_by_path: dict[str, dict[str, str]] = {}
        for record in current_records:
            path = client_file_from_record(ctx, record)
            if bridge.is_inside(path, ctx.project_root):
                current_by_path[relative_path(ctx, path)] = record
                if relative_path(ctx, path) not in before_files:
                    record_before_from_have(ctx, turn_id, path)
        before_manifest = read_json_if_exists(before_manifest_path(ctx, turn_id))
        before_files = before_manifest.get("files") if isinstance(before_manifest.get("files"), dict) else {}
        all_paths = sorted(set(before_files) | set(current_by_path), key=str.casefold)
        file_events: list[dict[str, Any]] = []
        patches: list[str] = []
        for relative in all_paths:
            path = (ctx.project_root / Path(relative)).resolve()
            before = before_files.get(relative) if isinstance(before_files.get(relative), dict) else {
                "exists": False,
                "sha256": None,
                "size": 0,
                "binary": False,
            }
            after = snapshot_metadata(path)
            record = current_by_path.get(relative, {})
            action = str(record.get("action") or ("add" if not before.get("exists") else "edit"))
            if not snapshot_changed(before, after):
                continue
            item = {
                "path": relative,
                "depotFile": record.get("depotFile"),
                "action": action,
                "before": {key: before.get(key) for key in ("exists", "sha256", "size", "binary")},
                "after": after,
            }
            file_events.append(item)
            patch = build_file_diff(relative, before, path)
            if patch:
                patches.append(patch)
        if not file_events:
            return None

        prompt_summary = str(session.get("promptSummary") or "Codex 文件修改")
        assistant_summary = redact_summary(str(event.get("last_assistant_message") or ""), 300)
        event_record = {
            "schemaVersion": AUDIT_SCHEMA_VERSION,
            "generatedBy": bridge.TOOL_NAME,
            "sessionId": ctx.session_id,
            "turnId": turn_id,
            "changelist": changelist,
            "projectRoot": str(ctx.project_root),
            "createdAtUtc": utc_now(),
            "summary": prompt_summary,
            "assistantSummary": assistant_summary,
            "files": file_events,
            "patchFile": str(turn_root(ctx, turn_id) / "changes.patch") if patches else None,
        }
        atomic_write_json(turn_root(ctx, turn_id) / "event.json", event_record)
        if patches:
            atomic_write_text(turn_root(ctx, turn_id) / "changes.patch", "\n".join(patches))

        turns = session.setdefault("turns", [])
        if not isinstance(turns, list):
            raise AuditError("session.turns 字段无效")
        turns[:] = [item for item in turns if not isinstance(item, dict) or item.get("turnId") != turn_id]
        turns.append(
            {
                "turnId": turn_id,
                "summary": prompt_summary,
                "fileCount": len(file_events),
                "recordedAtUtc": event_record["createdAtUtc"],
                "eventPath": str(turn_root(ctx, turn_id) / "event.json"),
            }
        )
        cumulative: dict[str, dict[str, str]] = {}
        for record in current_records:
            path = client_file_from_record(ctx, record)
            if bridge.is_inside(path, ctx.project_root):
                relative = relative_path(ctx, path)
                cumulative[relative] = {"path": relative, "action": str(record.get("action") or "edit")}
        session["files"] = list(cumulative.values())
        save_session(ctx, session)
        update_change_description(ctx, session)
        return event_record


def record_prompt(ctx: AuditContext, event: dict[str, Any]) -> None:
    turn_id = str(event.get("turn_id") or "unknown")
    prompt = str(event.get("prompt") or "")
    with project_lock(ctx.project_audit_root):
        session = load_session(ctx)
        summary = redact_summary(prompt)
        session["promptSummary"] = summary
        session["activeTurnId"] = turn_id
        turn = turn_root(ctx, turn_id)
        turn.mkdir(parents=True, exist_ok=True)
        atomic_write_text(turn / "prompt.txt", prompt)
        atomic_write_json(
            turn / "prompt.json",
            {
                "schemaVersion": AUDIT_SCHEMA_VERSION,
                "sessionId": ctx.session_id,
                "turnId": turn_id,
                "capturedAtUtc": utc_now(),
                "summary": summary,
                "sha256": sha256_bytes(prompt.encode("utf-8")),
            },
        )
        snapshot_session_files(ctx, session, turn_id)
        save_session(ctx, session)


def mark_session(ctx: AuditContext, status: str) -> None:
    with project_lock(ctx.project_audit_root):
        session = load_session(ctx)
        session["status"] = status
        session[f"{status}AtUtc"] = utc_now()
        save_session(ctx, session)


def additional_context(event_name: str, text: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": text,
        }
    }


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
        "decision": "block",
        "reason": reason,
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": reason,
        },
    }


def handle_event(event: dict[str, Any]) -> dict[str, Any] | None:
    event_name = str(event.get("hook_event_name") or "")
    ctx = resolve_context(event)
    if event_name == "SessionStart":
        mark_session(ctx, "active")
        return additional_context(
            event_name,
            "Codex P4 会话追溯已启用。首次实际写文件时会创建本会话专属 pending CL；"
            "不要把其他 CL 的文件静默 reopen 到本会话。",
        )
    if event_name == "UserPromptSubmit":
        record_prompt(ctx, event)
        return additional_context(
            event_name,
            "本回合修改将记录到当前 Codex 会话 CL，并在回合结束时生成本地审计补丁。",
        )
    if event_name == "PreToolUse":
        tool_name = str(event.get("tool_name") or "")
        try:
            if tool_name == "apply_patch":
                changelist = prepare_apply_patch(ctx, event)
                return additional_context(event_name, f"本次文件修改已归属 Codex 会话 CL {changelist}。")
            if tool_name == "Bash":
                prepare_shell(ctx, event)
        except AuditError as exc:
            return deny_pretool(str(exc))
        return None
    if event_name == "PostToolUse":
        tool_name = str(event.get("tool_name") or "")
        try:
            changelist = None
            if tool_name == "apply_patch":
                changelist = finish_apply_patch(ctx, event)
            elif tool_name == "Bash":
                changelist = finish_shell(ctx, event)
            if changelist:
                return additional_context(event_name, f"文件已核对并保留在 Codex 会话 CL {changelist}。")
        except AuditError as exc:
            return post_feedback(f"Codex P4 修改归档失败：{exc}")
        return None
    if event_name == "Stop":
        try:
            record = finalize_turn(ctx, event)
        except AuditError as exc:
            if not event.get("stop_hook_active"):
                return {"decision": "block", "reason": f"先修复 Codex P4 审计失败：{exc}"}
            return {"systemMessage": f"Codex P4 审计仍失败：{exc}"}
        if record:
            return {
                "systemMessage": (
                    f"Codex P4 已记录 {len(record['files'])} 个文件到 CL {record['changelist']}；"
                    f"审计记录：{turn_root(ctx, str(record['turnId'])) / 'event.json'}"
                )
            }
        return {}
    if event_name == "SessionEnd":
        mark_session(ctx, "ended")
        return None
    return None


def hook_main() -> int:
    bridge.configure_stdio()
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise AuditError("Hook stdin 应为 JSON 对象")
        result = handle_event(event)
        if result is not None:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    except HookSkip:
        return 0
    except (AuditError, bridge.CodexP4DiffError) as exc:
        event_name = str(locals().get("event", {}).get("hook_event_name") or "")
        if event_name == "PreToolUse":
            print(json.dumps(deny_pretool(str(exc)), ensure_ascii=False))
            return 0
        if event_name == "PostToolUse":
            print(json.dumps(post_feedback(str(exc)), ensure_ascii=False))
            return 0
        print(json.dumps({"systemMessage": f"Codex P4 Hook 失败：{exc}"}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"systemMessage": f"Codex P4 Hook 未预期错误：{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(hook_main())
