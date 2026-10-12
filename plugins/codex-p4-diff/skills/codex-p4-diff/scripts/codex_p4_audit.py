#!/usr/bin/env python3
"""Codex lifecycle hook that records one local Git commit per changed turn."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import difflib
import glob
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import time
from typing import Any, Iterable

import codex_p4_diff as bridge
import codex_p4_description as description


MAX_CAPTURE_BYTES = int(os.environ.get("CODEX_P4_DIFF_MAX_BLOB_BYTES", 8 * 1024 * 1024))
OWNERSHIP_LEASE_SECONDS = max(
    60, int(os.environ.get("CODEX_P4_DIFF_OWNERSHIP_LEASE_SECONDS", 10 * 60))
)
PROJECT_LOCK_WAIT_SECONDS = max(
    1, int(os.environ.get("CODEX_P4_DIFF_PROJECT_LOCK_WAIT_SECONDS", 5))
)
TOOL_LOCK_WAIT_SECONDS = max(
    0.05, float(os.environ.get("CODEX_P4_DIFF_TOOL_LOCK_WAIT_SECONDS", 0.25))
)
MAX_EXPANDED_SHELL_PATHS = max(
    100, int(os.environ.get("CODEX_P4_DIFF_MAX_SHELL_PATHS", 10_000))
)
OWNERSHIP_SCHEMA_VERSION = 8
CASE_INSENSITIVE_OWNERSHIP_PATHS = os.name == "nt"
MUTATING_TOOLS = {"apply_patch", "Edit", "Write", "Bash", "shell_command", "exec_command"}
IGNORED_TOP_LEVEL = {
    ".git",
    ".svn",
    "Binaries",
    "DerivedDataCache",
    "Intermediate",
    "Saved",
    "node_modules",
}
IGNORED_ANY_LEVEL = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
TRANSACTION_SCHEMA_VERSION = 1
UNCERTAINTY_SUMMARY_SCHEMA_VERSION = 1
RECOVERY_MARKER_SCHEMA_VERSION = 1
DEFERRED_AFTER_SCHEMA_VERSION = 1


class AuditError(RuntimeError):
    """Raised when audit integrity would be lost."""


class ProjectLockTimeout(AuditError):
    """Raised when a bounded local audit lock cannot be acquired."""


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


@dataclass
class ShellAnalysis:
    paths: list[Path]
    mutating: bool = False
    warnings: list[str] | None = None

    def __post_init__(self) -> None:
        if self.warnings is None:
            self.warnings = []


@dataclass
class ToolPathAnalysis:
    paths: list[Path]
    warnings: list[str]
    mutating: bool = False


@dataclass
class ClaimResult:
    generations: dict[str, str]
    superseded_paths: list[str]


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
        for attempt in range(10):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(10):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_json_if_exists(path: Path, default: Any, *, strict: bool = False) -> Any:
    if not path.is_file():
        return default
    for attempt in range(5):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError as exc:
            if attempt < 4:
                time.sleep(0.02 * (attempt + 1))
                continue
            if strict:
                raise AuditError(f"本地审计状态无法读取：{path}: {exc}") from exc
            return default
        except (OSError, json.JSONDecodeError) as exc:
            if strict:
                raise AuditError(f"本地审计状态无法读取：{path}: {exc}") from exc
            return default
    return default


def quarantine_state_file(path: Path, reason: str) -> str:
    """Preserve unreadable local audit state and return a visible recovery warning."""
    if not path.exists():
        return reason
    suffix = f".corrupt-{int(time.time())}-{os.getpid()}"
    backup = path.with_name(path.name + suffix)
    try:
        os.replace(path, backup)
        return f"{reason}; 已隔离到 {backup.name}"
    except OSError as exc:
        return f"{reason}; 隔离失败：{exc}"


@contextmanager
def project_lock(
    state_root: Path,
    *,
    lock_name: str = ".project.lock",
    wait_seconds: float | None = None,
):
    state_root.mkdir(parents=True, exist_ok=True)
    lock = state_root / lock_name
    deadline = time.monotonic() + (
        PROJECT_LOCK_WAIT_SECONDS if wait_seconds is None else max(0.0, wait_seconds)
    )
    stream = lock.open("a+b")
    acquired = False

    def acquire() -> None:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release() -> None:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    try:
        while True:
            try:
                acquire()
                acquired = True
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise ProjectLockTimeout(f"等待本地历史锁超时：{lock}") from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            if acquired:
                release()
    finally:
        stream.close()


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
    ctx: AuditContext, turn_id: str, event: dict[str, Any], changes: list[dict[str, Any]]
) -> dict[str, Any]:
    result = description.describe(
        turn_root(ctx, turn_id) / description.REPORT_NAME, ctx.session_id, turn_id,
        changes, redact_summary, fallback_change_summary,
    )
    assistant_message = str(event.get('last_assistant_message') or '')
    result['assistantMessageSha256'] = (
        sha256_bytes(assistant_message.encode('utf-8', errors='surrogatepass'))
        if assistant_message
        else None
    )
    return result


def normalize_session_id(value: Any) -> str:
    session_id = str(value or "").strip()
    if not session_id:
        raise HookSkip("Hook 输入缺少 Codex session_id")
    return session_id


def resolve_context(event: dict[str, Any]) -> AuditContext:
    cwd_text = str(event.get("cwd") or os.getcwd()).strip()
    requested_root = Path(cwd_text).resolve()
    if not requested_root.is_dir():
        raise HookSkip(f"Codex cwd 不存在：{requested_root}")
    session_id = normalize_session_id(event.get("session_id"))
    try:
        project_root, p4_context = bridge.resolve_project_p4_context(requested_root)
    except bridge.ActionableP4ContextError as exc:
        raise AuditError(str(exc)) from exc
    except bridge.CodexP4DiffError as exc:
        raise HookSkip(str(exc)) from exc
    paths = bridge.storage_paths(p4_context, project_root)
    paths["state"].mkdir(parents=True, exist_ok=True)
    with project_lock(paths["state"], lock_name=".repo-setup.lock"):
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
        "pendingRecoveryTurnIds": [],
        "turns": {},
        "ownedFiles": [],
    }


def session_structure_is_valid(session: Any) -> bool:
    if not isinstance(session, dict):
        return False
    turns = session.get("turns", {})
    pending = session.get("pendingRecoveryTurnIds", [])
    owned_files = session.get("ownedFiles", [])
    state_warnings = session.get("stateWarnings", [])
    active_turn_id = session.get("activeTurnId")
    next_sequence = session.get("nextSequence")
    if (
        not isinstance(turns, dict)
        or type(next_sequence) is not int
        or next_sequence < 1
        or (active_turn_id is not None and not isinstance(active_turn_id, str))
        or not isinstance(pending, list)
        or any(not isinstance(item, str) or not item.strip() for item in pending)
        or not isinstance(owned_files, list)
        or any(not isinstance(item, str) for item in owned_files)
        or not isinstance(state_warnings, list)
        or any(not isinstance(item, str) for item in state_warnings)
        or ("status" in session and not isinstance(session.get("status"), str))
    ):
        return False
    for turn_id, turn in turns.items():
        if not isinstance(turn_id, str) or not turn_id or not isinstance(turn, dict):
            return False
        retry_count = turn.get("retryCount")
        coverage = turn.get("coverageWarnings", [])
        if (
            ("status" in turn and not isinstance(turn.get("status"), str))
            or (
                retry_count is not None
                and (type(retry_count) is not int or retry_count < 0)
            )
            or not isinstance(coverage, list)
            or any(not isinstance(item, str) for item in coverage)
        ):
            return False
    return True


def load_session(ctx: AuditContext) -> dict[str, Any]:
    path = session_path(ctx)
    try:
        session = read_json_if_exists(path, {}, strict=True)
    except AuditError as exc:
        warning = quarantine_state_file(path, str(exc))
        session = base_session(ctx)
        session["stateWarnings"] = [warning]
    if not session and not path.exists():
        return base_session(ctx)
    if not session_structure_is_valid(session):
        warning = quarantine_state_file(path, f"会话账本结构无效：{path}")
        recovered = base_session(ctx)
        recovered["stateWarnings"] = [warning]
        return recovered
    if not session:
        return base_session(ctx)
    expected = {
        "sessionId": ctx.session_id,
        "projectRoot": str(ctx.project_root),
        "branch": ctx.branch,
    }
    for key, value in expected.items():
        if session.get(key) != value:
            warning = quarantine_state_file(path, f"会话账本上下文不一致：{key}")
            recovered = base_session(ctx)
            recovered["stateWarnings"] = [warning]
            return recovered
    return session


def save_session(ctx: AuditContext, session: dict[str, Any]) -> None:
    session["updatedAt"] = utc_now()
    atomic_write_json(session_path(ctx), session)


def normalize_turn_id(ctx: AuditContext, event: dict[str, Any], session: dict[str, Any]) -> str:
    routed = str(event.get("_codexP4DiffAuditTurnId") or "").strip()
    if routed:
        return routed
    explicit = str(event.get("turn_id") or "").strip()
    if explicit:
        return explicit
    active = str(session.get("activeTurnId") or "").strip()
    if active:
        return active
    return f"turn-{int(time.time() * 1000)}"


def route_writer_turn(
    ctx: AuditContext, event: dict[str, Any], session: dict[str, Any]
) -> str:
    """Keep a child writer's identity while capturing its parent user's earliest before."""
    writer_turn = str(event.get("turn_id") or session.get("activeTurnId") or "").strip()
    agent_id = str(event.get("agent_id") or "").strip()
    writers = session.setdefault("writerTurns", {})
    writer = writers.get(writer_turn)
    active = str(session.get("activeTurnId") or "")
    turns = session.setdefault("turns", {})
    if writer is None and agent_id and writer_turn and writer_turn != active:
        writer = {
            "agentId": agent_id,
            "parentTurnId": active or None,
            "status": "active",
            "startedAt": utc_now(),
        }
        if turns.get(writer_turn, {}).get("auditRole") == "unregistered-writer":
            writer["detachedTurnId"] = writer_turn
        writers[writer_turn] = writer
    if isinstance(writer, dict):
        if agent_id and writer.get("agentId") not in (None, agent_id):
            raise AuditError("子任务回合的 agent_id 与已登记身份不一致")
        parent = str(writer.get("parentTurnId") or "")
        parent_state = turns.get(parent, {})
        call_id = str(event.get("tool_use_id") or "unidentified-tool")
        active_tools = writer.setdefault("activeTools", {})
        pinned = active_tools.pop(call_id, None) if event.get("hook_event_name") == "PostToolUse" else None
        if pinned:
            # A tool that began in an old turn must finish against its original before.
            audit_turn = pinned
        elif (
            parent and parent == active
            and parent_state.get("status") == "active"
            and not parent_state.get("closedForChildWrites")
            and not writer.get("detachedTurnId")
            and not transaction_path(ctx, parent).exists()
        ):
            audit_turn = parent
        else:
            # Never append late writes to a completed transaction or the next user prompt.
            audit_turn = str(writer.get("detachedTurnId") or writer_turn)
            if turns.get(audit_turn, {}).get("status") in {"committed", "no-changes"}:
                audit_turn = f"{writer_turn}-late-{int(time.time() * 1000)}"
            writer["detachedTurnId"] = audit_turn
            turns.setdefault(audit_turn, {
                "status": "active", "startedAt": utc_now(),
                "parentTurnId": parent or None, "auditRole": "late-subagent",
            })
            warnings = event.setdefault("_codexP4DiffWarnings", [])
            if "late-subagent-turn" not in warnings:
                warnings.append("late-subagent-turn")
        if event.get("hook_event_name") == "PreToolUse":
            active_tools[call_id] = audit_turn
        writer["lastAuditTurnId"] = audit_turn
        event["_codexP4DiffWriter"] = {
            "turnId": writer_turn, "agentId": writer.get("agentId"),
        }
    else:
        audit_turn = writer_turn or normalize_turn_id(ctx, event, session)
        event["_codexP4DiffWriter"] = {"turnId": audit_turn, "agentId": None}
        if audit_turn != active and audit_turn not in turns:
            turns[audit_turn] = {
                "status": "active", "startedAt": utc_now(), "auditRole": "unregistered-writer",
            }
            event.setdefault("_codexP4DiffWarnings", []).append("unregistered-writer-turn")
    event["_codexP4DiffAuditTurnId"] = audit_turn
    return audit_turn


def record_subagent_event(ctx: AuditContext, event: dict[str, Any]) -> dict[str, Any]:
    stopping = event.get("hook_event_name") == "SubagentStop"
    with project_lock(ctx.state_root, lock_name=".state.lock"):
        session = load_session(ctx)
        writer_turn = str(event.get("turn_id") or "")
        writer = session.get("writerTurns", {}).get(writer_turn)
        if stopping and isinstance(writer, dict):
            audit_turn = str(writer.get("lastAuditTurnId") or writer.get("parentTurnId") or writer_turn)
            event["_codexP4DiffAuditTurnId"] = audit_turn
        else:
            audit_turn = route_writer_turn(ctx, event, session)
            writer = session.get("writerTurns", {}).get(writer_turn)
        if not isinstance(writer, dict):
            raise AuditError("子任务生命周期事件缺少可登记的独立 turn_id/agent_id")
        writer["status"] = "stopped" if stopping else "active"
        if stopping:
            writer["finishedAt"] = utc_now()
            affected_turns = {*writer.pop("activeTools", {}).values(), audit_turn}
            registry, _ = read_ownership_registry(ctx)
            for record in registry.get("files", {}).values():
                if (record.get("sessionId") == ctx.session_id and
                    record.get("writerTurnId") == writer_turn):
                    record["writerBusy"] = False
            atomic_write_json(ownership_path(ctx), registry)
        save_session(ctx, session)
    if stopping:
        results = []
        for affected in sorted(affected_turns):
            if (writer.get("detachedTurnId") == affected or
                session.get("turns", {}).get(affected, {}).get("status") == "awaiting-child-tools"):
                results.append(finalize_turn(ctx, {**event, "_codexP4DiffAuditTurnId": affected}))
        if results:
            return results[0] if len(results) == 1 else {"status": "subagent-recovery", "results": results}
    return {"status": "subagent-recorded", "parentTurnId": audit_turn}


def add_manifest_writer(entry: dict[str, Any], writer: dict[str, Any] | None) -> None:
    if writer:
        writers = entry.setdefault("writers", [])
        if writer not in writers:
            writers.append(dict(writer))


def end_subagent_tools(ctx: AuditContext) -> None:
    # SessionEnd is also the terminal boundary for children whose Post/Stop was lost.
    # Keep their before snapshots and recovery markers until normal finalization.
    with project_lock(ctx.state_root, lock_name=".state.lock"):
        session = load_session(ctx)
        for writer in session.get("writerTurns", {}).values():
            affected = {
                *writer.pop("activeTools", {}).values(),
                writer.get("detachedTurnId"), writer.get("lastAuditTurnId"),
            }
            writer["status"] = "stopped"
            for turn_id in affected:
                if (turn_id and session.get("turns", {}).get(turn_id, {}).get("status")
                    not in {"committed", "no-changes"}
                    and not recovery_marker_path(ctx, ctx.session_id, turn_id).exists()):
                    # No after snapshot was frozen; recovery must still read live content.
                    queue_turn_recovery(ctx, session, turn_id, "awaiting-child-tools")
        save_session(ctx, session)


def turn_root(ctx: AuditContext, turn_id: str) -> Path:
    return ctx.session_root / "turns" / bridge.sanitize_component(turn_id, 96)


def prompt_record_path(ctx: AuditContext, turn_id: str) -> Path:
    return turn_root(ctx, turn_id) / "prompt.json"


def before_manifest_path(ctx: AuditContext, turn_id: str) -> Path:
    return turn_root(ctx, turn_id) / "before.json"


def coverage_record_path(ctx: AuditContext, turn_id: str) -> Path:
    return turn_root(ctx, turn_id) / "coverage.json"


def transaction_path(ctx: AuditContext, turn_id: str) -> Path:
    return turn_root(ctx, turn_id) / "transaction.json"


def deferred_after_record_path(ctx: AuditContext, turn_id: str) -> Path:
    return turn_root(ctx, turn_id) / "deferred-after.json"


def load_coverage_record(ctx: AuditContext, turn_id: str) -> dict[str, Any]:
    path = coverage_record_path(ctx, turn_id)
    if not path.is_file():
        return {"warnings": []}
    try:
        value = read_json_if_exists(path, {"warnings": []}, strict=True)
    except AuditError as exc:
        detail = quarantine_state_file(path, str(exc))
        replacement = {
            "coverage": "partial",
            "warnings": [{"code": "coverage-state-invalid", "detail": detail}],
        }
        atomic_write_json(path, replacement)
        return replacement
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("warnings", []), list)
        or any(not isinstance(item, dict) for item in value.get("warnings", []))
    ):
        detail = quarantine_state_file(path, f"审计覆盖状态结构无效：{path}")
        replacement = {
            "coverage": "partial",
            "warnings": [{"code": "coverage-state-invalid", "detail": detail}],
        }
        atomic_write_json(path, replacement)
        return replacement
    return value


def load_before_manifest(ctx: AuditContext, turn_id: str) -> dict[str, Any]:
    path = before_manifest_path(ctx, turn_id)
    if not path.is_file():
        return {"files": {}}
    try:
        value = read_json_if_exists(path, {"files": {}}, strict=True)
    except AuditError as exc:
        detail = quarantine_state_file(path, str(exc))
        return {
            "files": {},
            "stateWarnings": [{"code": "before-manifest-invalid", "detail": detail}],
        }
    files = value.get("files") if isinstance(value, dict) else None
    valid = isinstance(files, dict)
    if valid:
        for relative, entry in files.items():
            try:
                candidate = (ctx.project_root / Path(relative)).resolve()
                candidate.relative_to(ctx.project_root)
            except (OSError, TypeError, ValueError):
                valid = False
                break
            if (
                not isinstance(relative, str)
                or not relative
                or Path(relative).is_absolute()
                or not isinstance(entry, dict)
            ):
                valid = False
                break
    if not valid:
        detail = quarantine_state_file(path, f"写入前清单结构或路径无效：{path}")
        return {
            "files": {},
            "stateWarnings": [{"code": "before-manifest-invalid", "detail": detail}],
        }
    warnings = value.get("stateWarnings", [])
    if not isinstance(warnings, list):
        value["stateWarnings"] = [
            {"code": "before-manifest-invalid", "detail": "stateWarnings was invalid"}
        ]
    return value


def coverage_warnings(ctx: AuditContext, turn_id: str) -> list[dict[str, Any]]:
    value = load_coverage_record(ctx, turn_id)
    warnings = value.get("warnings", [])
    return [item for item in warnings if isinstance(item, dict)]


def record_coverage_warnings(
    ctx: AuditContext,
    session: dict[str, Any],
    turn_id: str,
    event: dict[str, Any],
    warnings: Iterable[str],
) -> list[str]:
    codes = sorted({str(item) for item in warnings if str(item)})
    if not codes:
        return []
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    command = str(tool_input.get("command") or tool_input.get("cmd") or "")
    command_sha256 = sha256_bytes(command.encode("utf-8", errors="surrogatepass")) if command else None
    path = coverage_record_path(ctx, turn_id)
    record = load_coverage_record(ctx, turn_id)
    items = record.setdefault("warnings", [])
    existing = {
        (str(item.get("code") or ""), str(item.get("commandSha256") or ""))
        for item in items
        if isinstance(item, dict)
    }
    for code in codes:
        key = (code, command_sha256 or "")
        if key in existing:
            continue
        items.append(
            {
                "code": code,
                "commandSha256": command_sha256,
                "toolName": str(event.get("tool_name") or ""),
                "recordedAt": utc_now(),
            }
        )
        existing.add(key)
    record["coverage"] = "partial"
    atomic_write_json(path, record)
    turn_state = session.setdefault("turns", {}).setdefault(turn_id, {})
    turn_state["auditCoverage"] = "partial"
    turn_state["coverageWarnings"] = sorted(
        {str(item.get("code") or "") for item in items if isinstance(item, dict)}
    )
    return codes


def is_ignored_path(ctx: AuditContext, path: Path) -> bool:
    try:
        relative = path.resolve().relative_to(ctx.project_root).parts
    except (OSError, ValueError):
        return True
    folded = [part.casefold() for part in relative]
    return bool(
        relative
        and (
            folded[0] in {part.casefold() for part in IGNORED_TOP_LEVEL}
            or any(part in {item.casefold() for item in IGNORED_ANY_LEVEL} for part in folded)
        )
    )


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
SHELL_COMMAND_SEPARATORS = {";", "|", "&&", "||", "\n", "\r", "{", "}"}
POWERSHELL_ALIASES = {"rm": "remove-item", "cp": "copy-item", "mv": "move-item"}
POWERSHELL_COMMON_VALUE_FLAGS = {
    "-erroraction",
    "-ea",
    "-warningaction",
    "-wa",
    "-informationaction",
    "-infa",
    "-progressaction",
    "-errorvariable",
    "-ev",
    "-warningvariable",
    "-wv",
    "-informationvariable",
    "-iv",
    "-outvariable",
    "-ov",
    "-outbuffer",
    "-ob",
    "-pipelinevariable",
    "-pv",
}
POWERSHELL_NON_PATH_VALUE_FLAGS = POWERSHELL_COMMON_VALUE_FLAGS | {
    "-value",
    "-encoding",
    "-filter",
    "-include",
    "-inputobject",
    "-exclude",
    "-credential",
    "-stream",
    "-itemtype",
    "-type",
    "-newname",
    "-target",
    "-width",
    "-fromsession",
    "-tosession",
}
POWERSHELL_SWITCH_FLAGS = {
    "-append",
    "-asbytestream",
    "-confirm",
    "-container",
    "-debug",
    "-db",
    "-force",
    "-noclobber",
    "-nonewline",
    "-passthru",
    "-recurse",
    "-usetransaction",
    "-verbose",
    "-vb",
    "-whatif",
}


def shell_tokens(command: str, dialect: str = "powershell") -> list[ShellToken]:
    """Tokenize the write-target subset of PowerShell and POSIX shell syntax."""
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
            if dialect == "powershell" and quote == '"' and char == "`" and index + 1 < len(command):
                value.append(command[index + 1])
                index += 2
                continue
            if dialect == "posix" and quote == '"' and char == "\\" and index + 1 < len(command):
                escaped = command[index + 1]
                if escaped in {'"', "\\", "$", "`", "\n", "\r"}:
                    value.append(escaped)
                    index += 2
                    continue
            value.append(char)
            index += 1
            continue

        if dialect == "powershell" and command.startswith("<#", index):
            flush()
            end = command.find("#>", index + 2)
            index = len(command) if end < 0 else end + 2
            continue
        if char == "#" and not value and (
            index == 0 or command[index - 1].isspace() or command[index - 1] in ";|&{}"
        ):
            end = index
            while end < len(command) and command[end] not in "\r\n":
                end += 1
            index = end
            continue
        if char in {"'", '"'}:
            quote = char
            token_quoted = True
            index += 1
            continue
        if dialect == "powershell" and char == "`" and index + 1 < len(command):
            value.append(command[index + 1])
            index += 2
            continue
        if dialect == "posix" and char == "\\" and index + 1 < len(command):
            value.append(command[index + 1])
            index += 2
            continue
        if char.isspace():
            flush()
            if char in {"\n", "\r"}:
                tokens.append(ShellToken(char, operator=True))
            index += 1
            continue
        paired = command[index : index + 2]
        if paired in {"&&", "||", "[[", "]]", "((", "))"}:
            flush()
            tokens.append(ShellToken(paired, operator=True))
            index += 2
            continue
        if dialect == "powershell" and char == ",":
            flush()
            tokens.append(ShellToken(char, operator=True))
            index += 1
            continue
        if char in {";", "|", "{", "}", "&"}:
            flush()
            tokens.append(ShellToken(char, operator=True))
            index += 1
            continue
        if char == "=" and (
            (value and re.fullmatch(r"\$[A-Za-z_][\w:.-]*", "".join(value)))
            or (not value and tokens and re.fullmatch(r"\$[A-Za-z_][\w:.-]*", tokens[-1].value))
        ):
            flush()
            tokens.append(ShellToken("=", operator=True))
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


def shell_command_segments(
    tokens: list[ShellToken], dialect: str = "powershell"
) -> list[list[ShellToken]]:
    segments: list[list[ShellToken]] = []
    current: list[ShellToken] = []
    for token in tokens:
        if token.operator and (
            token.value in SHELL_COMMAND_SEPARATORS
            or (dialect == "posix" and token.value == "&")
        ):
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def normalized_shell_command(token: ShellToken, dialect: str = "powershell") -> str:
    value = token.value.strip("&() ")
    value = re.split(r"[\\/]", value)[-1].casefold()
    native_suffix = False
    for suffix in (".exe", ".cmd", ".bat"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            native_suffix = True
            break
    if dialect == "powershell" and not native_suffix:
        value = POWERSHELL_ALIASES.get(value, value)
    return value


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


def mutating_command_indexes(
    segment: list[ShellToken], dialect: str = "powershell"
) -> list[int]:
    if (
        dialect == "powershell"
        and len(segment) >= 2
        and segment[0].value.startswith("$")
        and segment[1].value == "="
    ):
        return []
    starts = [0]
    indexes: list[int] = []
    for start in starts:
        while (
            dialect == "posix"
            and start < len(segment)
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", segment[start].value)
        ):
            start += 1
        while start < len(segment) and segment[start].value.casefold() in {"then", "do", "else"}:
            start += 1
        called = False
        while start < len(segment) and segment[start].value == "&":
            called = True
            start += 1
        if start < len(segment) and normalized_shell_command(segment[start], dialect) in {
            "command",
            "sudo",
        }:
            start += 1
            while (
                dialect == "posix"
                and start < len(segment)
                and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", segment[start].value)
            ):
                start += 1
        if (
            start < len(segment)
            and (not segment[start].quoted or called)
            and normalized_shell_command(segment[start], dialect) in SHELL_MUTATING_COMMANDS
        ):
            indexes.append(start)
    return indexes


def likely_mutating_shell(command: str, dialect: str = "powershell") -> bool:
    tokens = shell_tokens(command, dialect)
    if shell_redirection_targets(tokens):
        return True
    return any(
        mutating_command_indexes(segment, dialect)
        for segment in shell_command_segments(tokens, dialect)
    )


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
    if cleaned.startswith("$") and re.fullmatch(r"\$\{?[A-Za-z_][\w:.-]*\}?", cleaned):
        key = "$" + cleaned.lstrip("$").strip("{}").casefold()
        return variables.get(key), index
    if "$" in cleaned:
        unresolved = False

        def replace_variable(match: re.Match[str]) -> str:
            nonlocal unresolved
            key = "$" + (match.group(1) or match.group(2) or "").casefold()
            if key not in variables:
                unresolved = True
                return match.group(0)
            return variables[key]

        expanded = re.sub(r"\$\{([^}]+)\}|\$([A-Za-z_][\w:.-]*)", replace_variable, cleaned)
        return (None if unresolved else expanded), index
    if not cleaned:
        return None, index
    return cleaned, index


def shell_argument_values(
    tokens: list[ShellToken], index: int, variables: dict[str, str]
) -> tuple[list[str], int]:
    values: list[str] = []
    resolved, end = shell_argument_value(tokens, index, variables)
    if resolved is not None:
        values.append(resolved)
    while end + 2 < len(tokens) and tokens[end + 1].operator and tokens[end + 1].value == ",":
        candidate = tokens[end + 2]
        if candidate.operator or candidate.value.startswith("-"):
            break
        resolved, end = shell_argument_value(tokens, end + 2, variables)
        if resolved is not None:
            values.append(resolved)
    return values, end


def shell_variable_values(tokens: list[ShellToken], dialect: str = "powershell") -> dict[str, str]:
    variables: dict[str, str] = {}
    for segment in shell_command_segments(tokens, dialect):
        if len(segment) >= 3 and segment[0].value.startswith("$") and segment[1].value == "=":
            resolved, _ = shell_argument_value(segment, 2, variables)
            if resolved is not None:
                variables[segment[0].value.casefold()] = resolved
            continue
        if segment:
            match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=(.+)", segment[0].value)
            if match:
                variables["$" + match.group(1).casefold()] = match.group(2)
    return variables


def split_shell_option(token: ShellToken) -> tuple[str, str | None]:
    value = token.value
    if not value.startswith("-"):
        return value.casefold(), None
    for separator in (":", "="):
        if separator in value:
            name, inline = value.split(separator, 1)
            return name.casefold(), inline
    return value.casefold(), None


def canonical_shell_option(name: str, known: set[str]) -> str:
    if name in known or not name.startswith("-") or len(name) <= 2:
        return name
    matches = {candidate for candidate in known if candidate.startswith(name)}
    return next(iter(matches)) if len(matches) == 1 else name


def named_shell_argument_items(
    segment: list[ShellToken], flags: set[str], variables: dict[str, str]
) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    index = 0
    while index < len(segment):
        name, inline = split_shell_option(segment[index])
        name = canonical_shell_option(name, flags)
        if name in flags:
            if inline is not None:
                resolved_values, end = [inline], index
                while (
                    end + 2 < len(segment)
                    and segment[end + 1].operator
                    and segment[end + 1].value == ","
                ):
                    candidate = segment[end + 2]
                    if candidate.operator or candidate.value.startswith("-"):
                        break
                    resolved, end = shell_argument_value(segment, end + 2, variables)
                    if resolved is not None:
                        resolved_values.append(resolved)
            else:
                resolved_values, end = shell_argument_values(segment, index + 1, variables)
            values.extend((name, resolved) for resolved in resolved_values)
            index = max(index + 1, end)
        index += 1
    return values


def named_shell_arguments(
    segment: list[ShellToken], flags: set[str], variables: dict[str, str]
) -> list[str]:
    return [value for _, value in named_shell_argument_items(segment, flags, variables)]


def positional_shell_argument_specs(
    segment: list[ShellToken],
    command_index: int,
    variables: dict[str, str],
    value_flags: set[str] | None = None,
    switch_flags: set[str] | None = None,
) -> tuple[list[tuple[str, bool]], list[str]]:
    values: list[tuple[str, bool]] = []
    warnings: list[str] = []
    value_flags = value_flags or set()
    switch_flags = switch_flags or set()
    index = command_index + 1
    while index < len(segment):
        token = segment[index]
        if token.operator:
            index += 1
            continue
        if token.value.startswith("-"):
            name, inline = split_shell_option(token)
            name = canonical_shell_option(name, value_flags | switch_flags)
            if inline is not None:
                if name not in value_flags and name not in switch_flags:
                    warning = f"shell-option-unresolved:{name}"
                    if warning not in warnings:
                        warnings.append(warning)
                index += 1
                continue
            if inline is None and name in value_flags:
                _, end = shell_argument_values(segment, index + 1, variables)
                index = max(index + 1, end) + 1
                continue
            if inline is None and name not in switch_flags:
                warning = f"shell-option-unresolved:{name}"
                if warning not in warnings:
                    warnings.append(warning)
                if index + 1 < len(segment):
                    candidate = segment[index + 1]
                    if not candidate.operator and not candidate.value.startswith("-"):
                        index += 2
                        continue
            index += 1
            continue
        resolved, end = shell_argument_value(segment, index, variables)
        if resolved is not None:
            values.append((resolved, token.quoted))
        index = max(index + 1, end + 1)
    return values, warnings


def positional_shell_arguments(
    segment: list[ShellToken],
    command_index: int,
    variables: dict[str, str],
    value_flags: set[str] | None = None,
    switch_flags: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    specs, warnings = positional_shell_argument_specs(
        segment,
        command_index,
        variables,
        value_flags,
        switch_flags,
    )
    return [value for value, _ in specs], warnings


def shell_flag_present(tokens: list[ShellToken], flags: set[str]) -> bool:
    return any(split_shell_option(token)[0] in flags for token in tokens)


def path_is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    try:
        attributes = int(getattr(os.lstat(path), "st_file_attributes", 0))
    except OSError:
        return False
    return bool(attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)))


def shell_candidate_entry(ctx: AuditContext, value: str, cwd: Path) -> Path | None:
    cleaned = value.strip().strip("'\"` ,;()")
    if not cleaned or cleaned.startswith("//") or cleaned.startswith("-"):
        return None
    cleaned = cleaned.replace("/", os.sep)
    candidate = Path(cleaned)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    if path_is_link_like(candidate):
        return None
    try:
        resolved = candidate.resolve()
        resolved.relative_to(ctx.project_root)
    except (OSError, ValueError):
        return None
    if is_ignored_path(ctx, resolved):
        return None
    return resolved


def expand_shell_entries(
    ctx: AuditContext, value: str, cwd: Path, *, literal: bool
) -> tuple[list[Path], list[str]]:
    cleaned = value.strip().strip("'\"` ,;()")
    if not cleaned:
        return [], []
    raw = Path(cleaned.replace("/", os.sep))
    if not raw.is_absolute():
        raw = cwd / raw
    values = [str(raw)]
    warnings: list[str] = []
    if not literal and glob.has_magic(str(raw)):
        if "**" in str(raw):
            return [], ["shell-recursive-wildcard-unresolved"]
        values = sorted(glob.glob(str(raw), recursive=False))
        if not values:
            return [], []
    entries: list[Path] = []
    for item in values:
        raw_entry = Path(item)
        if path_is_link_like(raw_entry):
            warnings.append("shell-link-skipped")
            continue
        entry = shell_candidate_entry(ctx, item, cwd)
        if entry is not None and entry not in entries:
            entries.append(entry)
            if len(entries) >= MAX_EXPANDED_SHELL_PATHS:
                warnings.append("shell-path-limit")
                break
    return entries, warnings


def project_files_under(ctx: AuditContext, entry: Path) -> tuple[list[Path], list[str]]:
    if not entry.is_dir():
        return ([entry] if not entry.is_dir() else []), []
    files: list[Path] = []
    warnings: list[str] = []
    for current_root, directories, names in os.walk(entry, followlinks=False):
        root = Path(current_root)
        retained_directories: list[str] = []
        for name in directories:
            child = root / name
            if path_is_link_like(child):
                if "shell-link-skipped" not in warnings:
                    warnings.append("shell-link-skipped")
                continue
            if not is_ignored_path(ctx, child):
                retained_directories.append(name)
        directories[:] = retained_directories
        for name in sorted(names):
            raw_candidate = root / name
            if path_is_link_like(raw_candidate):
                warnings.append("shell-link-skipped")
                continue
            candidate = raw_candidate.resolve()
            if is_ignored_path(ctx, candidate):
                continue
            files.append(candidate)
            if len(files) >= MAX_EXPANDED_SHELL_PATHS:
                warnings.append("shell-path-limit")
                return files, warnings
    return files, warnings


def resolve_shell_specs(
    ctx: AuditContext,
    specs: list[tuple[str, bool]],
    cwd: Path,
    *,
    expand_directories: bool,
) -> tuple[list[Path], list[Path], list[str]]:
    entries: list[Path] = []
    files: list[Path] = []
    warnings: list[str] = []
    for value, literal in specs:
        expanded, current_warnings = expand_shell_entries(ctx, value, cwd, literal=literal)
        warnings.extend(current_warnings)
        for entry in expanded:
            if entry not in entries:
                entries.append(entry)
            if entry.is_dir():
                if expand_directories:
                    nested, nested_warnings = project_files_under(ctx, entry)
                    warnings.extend(nested_warnings)
                    for nested_path in nested:
                        if nested_path not in files:
                            files.append(nested_path)
                            if len(files) >= MAX_EXPANDED_SHELL_PATHS:
                                if "shell-path-limit" not in warnings:
                                    warnings.append("shell-path-limit")
                                return entries, files, warnings
                continue
            if entry not in files:
                files.append(entry)
                if len(files) >= MAX_EXPANDED_SHELL_PATHS:
                    if "shell-path-limit" not in warnings:
                        warnings.append("shell-path-limit")
                    return entries, files, warnings
    return entries, files, warnings


def map_copy_move_paths(
    ctx: AuditContext,
    cwd: Path,
    source_specs: list[tuple[str, bool]],
    destination_value: str | None,
    *,
    move: bool,
) -> ShellAnalysis:
    if not source_specs or not destination_value:
        return ShellAnalysis([], mutating=True, warnings=["shell-copy-move-target-unresolved"])
    source_entries, source_files, warnings = resolve_shell_specs(
        ctx, source_specs, cwd, expand_directories=True
    )
    destination = shell_candidate_entry(ctx, destination_value, cwd)
    if destination is None:
        return ShellAnalysis(
            source_files if move else [],
            mutating=True,
            warnings=warnings + ["shell-copy-move-destination-unresolved"],
        )
    if len(source_entries) > 1 and not destination.is_dir():
        return ShellAnalysis(
            source_files if move else [],
            mutating=True,
            warnings=warnings + ["shell-copy-move-multi-source-destination"],
        )
    targets: list[Path] = list(source_files) if move else []
    destination_is_directory = destination.is_dir() or len(source_entries) > 1
    for source in source_entries:
        base = destination / source.name if destination_is_directory else destination
        if source.is_dir():
            for source_file in source_files:
                try:
                    relative = source_file.relative_to(source)
                except ValueError:
                    continue
                target = shell_candidate_entry(ctx, str(base / relative), cwd)
                if target is not None and target not in targets:
                    targets.append(target)
        else:
            target = shell_candidate_entry(ctx, str(base), cwd)
            if target is not None and target not in targets:
                targets.append(target)
    if len(targets) > MAX_EXPANDED_SHELL_PATHS:
        targets = targets[:MAX_EXPANDED_SHELL_PATHS]
        if "shell-path-limit" not in warnings:
            warnings.append("shell-path-limit")
    return ShellAnalysis(targets, mutating=True, warnings=warnings)


def mutating_segment_analysis(
    ctx: AuditContext,
    segment: list[ShellToken],
    variables: dict[str, str],
    cwd: Path,
    dialect: str,
) -> ShellAnalysis:
    paths: list[Path] = []
    warnings: list[str] = []
    mutating = False
    for command_index in mutating_command_indexes(segment, dialect):
        command_token = segment[command_index]
        command = normalized_shell_command(command_token, dialect)
        raw_command = re.split(r"[\\/]", command_token.value.strip("&() "))[-1].casefold()
        native_invocation = dialect == "posix" or raw_command.endswith((".exe", ".cmd", ".bat"))
        tail = segment[command_index + 1 :]
        sed_edit_options: list[tuple[str, str | None, str]] = []
        if command == "sed":
            for item in tail:
                name, inline = split_shell_option(item)
                if name in {"-i", "--in-place"} or (
                    item.value.startswith("-i") and not item.value.startswith("--")
                ):
                    sed_edit_options.append((name, inline, item.value))
            if not sed_edit_options:
                continue
        elif command == "clang-format" and not shell_flag_present(tail, {"-i"}):
            continue
        elif command == "prettier" and not shell_flag_present(tail, {"--write"}):
            continue
        mutating = True
        path_flags = {"-path", "-literalpath", "-filepath"}
        destination_flags = {"-destination"}
        common_value_flags = POWERSHELL_NON_PATH_VALUE_FLAGS | path_flags | destination_flags
        positional_value_flags = set(common_value_flags)
        positional_switch_flags = set(POWERSHELL_SWITCH_FLAGS)
        if command == "sed":
            positional_value_flags |= {"-e", "--expression", "-f", "--file"}
        elif command == "clang-format":
            positional_value_flags |= {
                "-style",
                "--style",
                "-fallback-style",
                "--fallback-style",
                "-assume-filename",
                "--assume-filename",
            }
        elif command == "prettier":
            positional_value_flags |= {
                "--config",
                "--parser",
                "--plugin",
                "--plugin-search-dir",
                "--ignore-path",
                "--stdin-filepath",
            }
        positional_switch_flags |= {"-i", "--in-place", "--write"}
        positional_switch_flags |= {
            split_shell_option(item)[0]
            for item in tail
            if item.value.startswith("-i")
        }
        if native_invocation:
            positional_switch_flags |= {"--", "-r", "-f", "-rf", "-fr", "-a", "-v", "-p"}
        named_paths = named_shell_argument_items(tail, path_flags, variables)
        positional_items, positional_warnings = positional_shell_argument_specs(
            segment,
            command_index,
            variables,
            positional_value_flags,
            positional_switch_flags,
        )
        warnings.extend(positional_warnings)
        positionals = [value for value, _ in positional_items]
        positional_specs = [
            (value, dialect == "posix" and quoted) for value, quoted in positional_items
        ]

        if command in {"set-content", "add-content", "clear-content", "out-file"}:
            specs = [
                (value, flag in {"-literalpath", "-filepath"}) for flag, value in named_paths
            ]
            if not specs and positionals:
                specs = [(positionals[0], True)]
            _, current, current_warnings = resolve_shell_specs(
                ctx, specs, cwd, expand_directories=False
            )
            paths.extend(path for path in current if path not in paths)
            warnings.extend(current_warnings)
        elif command == "new-item":
            item_types = named_shell_arguments(tail, {"-itemtype", "-type"}, variables)
            if item_types and item_types[-1].casefold() in {
                "directory",
                "junction",
                "symboliclink",
            }:
                continue
            specs = [
                (value, flag == "-literalpath") for flag, value in named_paths
            ] or ([(positionals[0], True)] if positionals else [])
            _, current, current_warnings = resolve_shell_specs(
                ctx, specs, cwd, expand_directories=False
            )
            paths.extend(path for path in current if path not in paths)
            warnings.extend(current_warnings)
        elif command in {"remove-item", "rm"}:
            specs = [
                (value, flag == "-literalpath") for flag, value in named_paths
            ] or positional_specs
            _, current, current_warnings = resolve_shell_specs(
                ctx, specs, cwd, expand_directories=True
            )
            paths.extend(path for path in current if path not in paths)
            warnings.extend(current_warnings)
        elif command in {"copy-item", "move-item", "cp", "mv"}:
            named_destinations = named_shell_arguments(tail, destination_flags, variables)
            source_specs = [
                (value, flag == "-literalpath") for flag, value in named_paths
            ]
            destination = named_destinations[-1] if named_destinations else None
            if not source_specs:
                if destination is not None:
                    source_specs = positional_specs
                elif len(positionals) >= 2:
                    source_specs = positional_specs[:-1]
                    destination = positionals[-1]
            elif destination is None and positionals:
                destination = positionals[-1]
            mapped = map_copy_move_paths(
                ctx,
                cwd,
                source_specs,
                destination,
                move=command in {"move-item", "mv"},
            )
            paths.extend(path for path in mapped.paths if path not in paths)
            warnings.extend(mapped.warnings or [])
        elif command == "rename-item":
            new_names = named_shell_arguments(tail, {"-newname"}, variables)
            source_specs = [
                (value, flag == "-literalpath") for flag, value in named_paths
            ]
            if not source_specs and len(positionals) >= 2:
                source_specs = [positional_specs[0]]
                new_names = [positionals[1]]
            source_entries, source_files, current_warnings = resolve_shell_specs(
                ctx, source_specs, cwd, expand_directories=True
            )
            paths.extend(path for path in source_files if path not in paths)
            warnings.extend(current_warnings)
            if source_entries and new_names:
                source = source_entries[-1]
                new_path = Path(new_names[-1])
                destination = new_path if new_path.is_absolute() else source.parent / new_path
                mapped = map_copy_move_paths(
                    ctx, cwd, [(str(source), True)], str(destination), move=False
                )
                paths.extend(path for path in mapped.paths if path not in paths)
                warnings.extend(mapped.warnings or [])
        else:
            value_specs = positional_specs
            backup_suffix = ""
            if command == "sed":
                if not shell_flag_present(tail, {"-e", "--expression"}) and value_specs:
                    value_specs = value_specs[1:]
                name, inline, raw_edit = sed_edit_options[0]
                if inline is not None:
                    backup_suffix = inline
                elif name not in {"-i", "--in-place"} and raw_edit.startswith("-i"):
                    backup_suffix = raw_edit[2:]
            elif command == "apply_patch":
                warnings.append("shell-apply-patch-content-unresolved")
                continue
            specs = [spec for spec in value_specs if not spec[0].startswith("@")]
            if any(value.startswith("@") for value, _ in value_specs):
                warnings.append("shell-response-file-unresolved")
            _, current, expand_warnings = resolve_shell_specs(
                ctx, specs, cwd, expand_directories=command in {"rm", "mv"}
            )
            paths.extend(path for path in current if path not in paths)
            if backup_suffix:
                for path in current:
                    backup = shell_candidate_entry(ctx, str(path) + backup_suffix, cwd)
                    if backup is not None and backup not in paths:
                        paths.append(backup)
            warnings.extend(expand_warnings)
    if mutating and not paths and not warnings:
        warnings.append("shell-write-target-unresolved")
    if len(paths) > MAX_EXPANDED_SHELL_PATHS:
        paths = paths[:MAX_EXPANDED_SHELL_PATHS]
        if "shell-path-limit" not in warnings:
            warnings.append("shell-path-limit")
    return ShellAnalysis(paths, mutating=mutating, warnings=warnings)


def analyze_shell_command(
    ctx: AuditContext, command: str, cwd: Path, dialect: str = "powershell"
) -> ShellAnalysis:
    tokens = shell_tokens(command, dialect)
    variables = shell_variable_values(tokens, dialect)
    array_assignment = dialect == "powershell" and any(
        len(segment) >= 3
        and segment[0].value.startswith("$")
        and segment[1].value == "="
        and any(token.operator and token.value == "," for token in segment[2:])
        for segment in shell_command_segments(tokens, dialect)
    )
    paths: list[Path] = []
    warnings: list[str] = []
    mutating = False
    for segment in shell_command_segments(tokens, dialect):
        current = mutating_segment_analysis(ctx, segment, variables, cwd, dialect)
        mutating = mutating or current.mutating
        for path in current.paths:
            if path in paths:
                continue
            if len(paths) >= MAX_EXPANDED_SHELL_PATHS:
                if "shell-path-limit" not in warnings:
                    warnings.append("shell-path-limit")
                break
            paths.append(path)
        warnings.extend(item for item in (current.warnings or []) if item not in warnings)
    redirect_tokens = shell_redirection_targets(tokens)
    mutating = mutating or bool(redirect_tokens)
    for token in redirect_tokens:
        resolved, _ = shell_argument_value([token], 0, variables)
        if resolved is None:
            warnings.append("shell-redirection-target-unresolved")
            continue
        redirect_entries, redirect_warnings = expand_shell_entries(
            ctx, resolved, cwd, literal=True
        )
        warnings.extend(item for item in redirect_warnings if item not in warnings)
        path = redirect_entries[0] if redirect_entries else None
        if path is not None and not path.is_dir() and path not in paths:
            if len(paths) >= MAX_EXPANDED_SHELL_PATHS:
                if "shell-path-limit" not in warnings:
                    warnings.append("shell-path-limit")
            else:
                paths.append(path)
    if mutating and not paths and not warnings:
        warnings.append("shell-write-target-unresolved")
    if mutating and array_assignment and "shell-variable-array-partial" not in warnings:
        warnings.append("shell-variable-array-partial")
    return ShellAnalysis(paths, mutating=mutating, warnings=warnings)


def mutating_segment_paths(
    segment: list[ShellToken], variables: dict[str, str]
) -> list[str]:
    """Compatibility helper retained for callers that only need lexical operands."""
    values: list[str] = []
    for command_index in mutating_command_indexes(segment):
        positional, _ = positional_shell_arguments(
            segment,
            command_index,
            variables,
            POWERSHELL_NON_PATH_VALUE_FLAGS,
            POWERSHELL_SWITCH_FLAGS,
        )
        values.extend(positional)
    return values


def extract_shell_paths(
    ctx: AuditContext, command: str, cwd: Path, dialect: str = "powershell"
) -> list[Path]:
    return analyze_shell_command(ctx, command, cwd, dialect).paths


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


def analyze_tool_paths(ctx: AuditContext, event: dict[str, Any]) -> ToolPathAnalysis:
    tool_name = str(event.get("tool_name") or "")
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    cwd = Path(str(tool_input.get("workdir") or event.get("cwd") or ctx.project_root)).resolve()
    paths: list[Path] = []
    warnings: list[str] = []
    mutating = tool_name in MUTATING_TOOLS
    if tool_name in {"apply_patch", "Edit", "Write"}:
        metadata_paths = list(walk_path_values(tool_input))
        if tool_name == "apply_patch":
            patch = str(tool_input.get("patch") or tool_input.get("input") or tool_input.get("command") or "")
            metadata_paths.extend(re.findall(
                r"^\*\*\* (?:(?:Update|Add|Delete) File|Move to):\s*(.+?)\s*$", patch, re.M))
        if metadata_paths and all(Path(value).name == description.REPORT_NAME for value in metadata_paths):
            metadata_session = read_json_if_exists(session_path(ctx), {})
            active_turn = metadata_session.get("activeTurnId") if isinstance(metadata_session, dict) else None
            if (active_turn and session_structure_is_valid(metadata_session)
                    and metadata_session.get("sessionId") == ctx.session_id
                    and metadata_session.get("turns", {}).get(active_turn, {}).get("status") == "active"
                    and str(event.get("turn_id") or active_turn) == active_turn
                    and not transaction_path(ctx, active_turn).exists()):
                report_path = turn_root(ctx, active_turn) / description.REPORT_NAME
                # Only this turn's report is metadata; mixed patches and other State writes remain audited.
                redirected = any(part.is_symlink() or (part.exists() and
                    getattr(part.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
                    for part in (report_path, *report_path.parents))
                if not redirected and all(
                        (cwd / value).resolve() == report_path.resolve() for value in metadata_paths):
                    return ToolPathAnalysis([], [], mutating=False)
    if tool_name == "apply_patch":
        patch_text = str(
            tool_input.get("patch") or tool_input.get("input") or tool_input.get("command") or ""
        )
        paths.extend(parse_apply_patch_paths(patch_text, cwd, ctx.project_root))
        if patch_text and not paths:
            warnings.append("apply-patch-target-unresolved")
    for value in walk_path_values(tool_input):
        path = resolve_candidate_path(ctx, value, cwd)
        if path is not None and path not in paths:
            paths.append(path)
    if tool_name in {"Bash", "shell_command", "exec_command"}:
        command = str(tool_input.get("command") or tool_input.get("cmd") or "")
        shell_name = Path(str(tool_input.get("shell") or "")).stem.lower()
        dialect = "posix" if tool_name == "Bash" or shell_name in {"bash", "sh", "zsh"} else "powershell"
        shell = analyze_shell_command(ctx, command, cwd, dialect)
        mutating = shell.mutating
        for path in shell.paths:
            if path not in paths:
                paths.append(path)
        warnings.extend(shell.warnings or [])
        dynamic_markers = ("@\"", "@'", "@(", "$(", "${env:", "--%")
        if shell.mutating and any(marker in command for marker in dynamic_markers):
            warnings.append("shell-dynamic-syntax-partial")
    filtered = [path for path in paths if not is_ignored_path(ctx, path)]
    if mutating and not filtered and not warnings:
        warnings.append("write-target-unresolved")
    return ToolPathAnalysis(filtered, sorted(set(warnings)), mutating=mutating)


def extract_tool_paths(ctx: AuditContext, event: dict[str, Any]) -> list[Path]:
    return analyze_tool_paths(ctx, event).paths


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


def ownership_key(relative: str) -> str:
    normalized = relative.replace("\\", "/")
    return normalized.casefold() if CASE_INSENSITIVE_OWNERSHIP_PATHS else normalized


def uncertainty_marker_directory(ctx: AuditContext, relative: str | None) -> Path:
    key = "global" if relative is None else sha256_bytes(ownership_key(relative).encode("utf-8"))
    return ctx.state_root / "uncertainty" / key


def uncertainty_writer(marker: dict[str, Any]) -> tuple[str, str]:
    return str(marker.get("sessionId") or ""), str(marker.get("turnId") or "")


def normalize_uncertainty_marker(value: Any, fallback_epoch: float) -> dict[str, Any]:
    if isinstance(value, dict):
        try:
            recorded_epoch = float(value.get("recordedEpoch") or 0)
        except (TypeError, ValueError, OverflowError):
            recorded_epoch = 0
        if math.isfinite(recorded_epoch) and recorded_epoch > 0:
            return {
                "sessionId": str(value.get("sessionId") or ""),
                "turnId": str(value.get("turnId") or ""),
                "recordedAt": str(value.get("recordedAt") or ""),
                "recordedEpoch": recorded_epoch,
            }
    return {
        "sessionId": "",
        "turnId": "",
        "recordedAt": utc_now(),
        "recordedEpoch": fallback_epoch,
    }


def reduce_uncertainty_markers(markers: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the newest events from at most two distinct writers.

    For any querying writer, the newest overall event and the newest event from
    another writer are sufficient to decide whether a foreign event happened
    after its before snapshot.
    """
    latest_by_writer: dict[tuple[str, str], dict[str, Any]] = {}
    for marker in markers:
        writer = uncertainty_writer(marker)
        current = latest_by_writer.get(writer)
        if current is None or float(marker["recordedEpoch"]) > float(current["recordedEpoch"]):
            latest_by_writer[writer] = marker
    return sorted(
        latest_by_writer.values(),
        key=lambda item: float(item["recordedEpoch"]),
        reverse=True,
    )[:2]


def uncertainty_summary_path(directory: Path) -> Path:
    return directory / "summary.json"


def load_uncertainty_markers_locked(
    directory: Path,
) -> tuple[list[dict[str, Any]], list[Path]]:
    fallback_epoch = time.time()
    candidates: list[dict[str, Any]] = []
    cleanup: list[Path] = []
    high_water_paths: set[Path] = set()
    summary_path = uncertainty_summary_path(directory)
    if summary_path.is_file():
        summary = read_json_if_exists(summary_path, None)
        if (
            isinstance(summary, dict)
            and summary.get("schemaVersion") == UNCERTAINTY_SUMMARY_SCHEMA_VERSION
            and isinstance(summary.get("markers"), list)
        ):
            candidates.extend(
                normalize_uncertainty_marker(item, fallback_epoch)
                for item in summary["markers"]
            )
        else:
            candidates.append(normalize_uncertainty_marker(None, fallback_epoch))

    pending_path = directory / "pending.json"
    if pending_path.is_file():
        ingest_path = directory / (
            f"pending-merge-{os.getpid()}-{time.time_ns()}-{os.urandom(4).hex()}.json"
        )
        try:
            os.replace(pending_path, ingest_path)
            high_water_paths.add(ingest_path)
        except FileNotFoundError:
            pass

    legacy_or_pending = [directory / "latest.json"]
    legacy_or_pending.extend(directory.glob("timeout-*.json"))
    legacy_or_pending.extend(directory.glob("pending-*.json"))
    for path in legacy_or_pending:
        if not path.is_file():
            continue
        marker = normalize_uncertainty_marker(read_json_if_exists(path, None), fallback_epoch)
        if path in high_water_paths or path.name.startswith("pending-merge-"):
            marker = {
                "sessionId": "",
                "turnId": "",
                "recordedAt": utc_now(),
                "recordedEpoch": max(float(marker["recordedEpoch"]), fallback_epoch),
            }
        candidates.append(marker)
        cleanup.append(path)
    return reduce_uncertainty_markers(candidates), cleanup


def persist_uncertainty_markers_locked(
    directory: Path,
    markers: Iterable[dict[str, Any]],
    cleanup: Iterable[Path],
) -> list[dict[str, Any]]:
    reduced = reduce_uncertainty_markers(markers)
    atomic_write_json(
        uncertainty_summary_path(directory),
        {
            "schemaVersion": UNCERTAINTY_SUMMARY_SCHEMA_VERSION,
            "updatedAt": utc_now(),
            "markers": reduced,
        },
    )
    for path in cleanup:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # The summary is already durable; a later merge can retry cleanup.
            pass
    return reduced


def write_uncertainty_marker(directory: Path, marker: dict[str, Any]) -> None:
    try:
        with project_lock(
            directory,
            lock_name=".scope.lock",
            wait_seconds=TOOL_LOCK_WAIT_SECONDS,
        ):
            markers, cleanup = load_uncertainty_markers_locked(directory)
            persist_uncertainty_markers_locked(directory, [*markers, marker], cleanup)
    except ProjectLockTimeout:
        # Keep normal timeout state bounded. The compactor atomically renames this
        # file and promotes its anonymous timestamp to compaction time, so writer
        # overwrite order cannot move the observed high-water mark backwards.
        atomic_write_json(
            directory / "pending.json",
            {
                **marker,
                "sessionId": "",
                "turnId": "",
            },
        )


def write_uncertainty_markers(
    ctx: AuditContext,
    turn_id: str,
    paths: Iterable[Path],
    *,
    global_scope: bool = False,
) -> None:
    relatives = sorted({relative_path(ctx, path) for path in paths})
    scopes: list[str | None] = [None] if global_scope else relatives
    recorded_epoch = time.time()
    for relative in scopes:
        marker = {
            "schemaVersion": bridge.SCHEMA_VERSION,
            "sessionId": ctx.session_id,
            "turnId": turn_id,
            "path": relative,
            "scope": "global" if relative is None else "path",
            "recordedAt": utc_now(),
            "recordedEpoch": recorded_epoch,
        }
        write_uncertainty_marker(uncertainty_marker_directory(ctx, relative), marker)


def has_foreign_uncertainty_since(
    ctx: AuditContext, turn_id: str, relative: str, recorded_epoch: float
) -> bool:
    for directory in (
        uncertainty_marker_directory(ctx, None),
        uncertainty_marker_directory(ctx, relative),
    ):
        if not directory.is_dir():
            continue
        try:
            with project_lock(
                directory,
                lock_name=".scope.lock",
                wait_seconds=TOOL_LOCK_WAIT_SECONDS,
            ):
                markers, cleanup = load_uncertainty_markers_locked(directory)
                markers = persist_uncertainty_markers_locked(directory, markers, cleanup)
        except ProjectLockTimeout:
            # A concurrent marker update is itself enough to make attribution uncertain.
            return True
        for marker in markers:
            try:
                marker_epoch = float(marker.get("recordedEpoch") or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if marker_epoch <= recorded_epoch:
                continue
            if (
                str(marker.get("sessionId") or "") == ctx.session_id
                and str(marker.get("turnId") or "") == turn_id
            ):
                continue
            return True
    return False


def manifest_file_key(files: dict[str, Any], relative: str) -> str | None:
    if relative in files:
        return relative
    target = ownership_key(relative)
    return next((key for key in files if ownership_key(str(key)) == target), None)


def reserve_before_path_locked(
    ctx: AuditContext,
    turn_id: str,
    path: Path,
    ownership_generation: str | int | None = None,
    writer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Publish a lightweight manifest entry before releasing the ownership lock."""
    relative = relative_path(ctx, path)
    manifest_path = before_manifest_path(ctx, turn_id)
    manifest = load_before_manifest(ctx, turn_id)
    files = manifest.setdefault("files", {})
    existing_key = manifest_file_key(files, relative)
    if existing_key is not None:
        existing = files[existing_key]
        add_manifest_writer(existing, writer)
        if ownership_generation is not None and "ownershipGeneration" not in existing:
            existing["ownershipGeneration"] = ownership_generation
        atomic_write_json(manifest_path, manifest)
        return existing
    reserved = {
        "path": relative,
        "exists": None,
        "size": None,
        "sha256": None,
        "binary": None,
        "captured": False,
        "snapshotPath": None,
        "snapshotPending": True,
        "recordedAt": utc_now(),
        "recordedEpoch": time.time(),
    }
    if ownership_generation is not None:
        reserved["ownershipGeneration"] = ownership_generation
    add_manifest_writer(reserved, writer)
    files[relative] = reserved
    atomic_write_json(manifest_path, manifest)
    return reserved


def capture_reserved_before_path(
    ctx: AuditContext,
    turn_id: str,
    path: Path,
    ownership_generation: str | int | None = None,
) -> dict[str, Any]:
    """Capture one path without holding the project-wide state lock."""
    relative = relative_path(ctx, path)
    path_key = hashlib.sha256(ownership_key(relative).encode("utf-8")).hexdigest()
    lock_root = turn_root(ctx, turn_id) / "before-locks"
    with project_lock(
        lock_root,
        lock_name=f"{path_key}.lock",
        wait_seconds=PROJECT_LOCK_WAIT_SECONDS,
    ):
        with project_lock(
            ctx.state_root,
            lock_name=".state.lock",
            wait_seconds=PROJECT_LOCK_WAIT_SECONDS,
        ):
            manifest = load_before_manifest(ctx, turn_id)
            files = manifest.setdefault("files", {})
            existing_key = manifest_file_key(files, relative)
            if existing_key is None:
                existing = reserve_before_path_locked(
                    ctx, turn_id, path, ownership_generation
                )
            else:
                existing = files[existing_key]
            if existing.get("exists") is not None:
                return existing
            reserved_generation = existing.get("ownershipGeneration")
            reserved_epoch = existing.get("recordedEpoch")

        blob_name = hashlib.sha256(relative.encode("utf-8")).hexdigest() + ".bin"
        snapshot_path = turn_root(ctx, turn_id) / "before-blobs" / blob_name
        metadata = snapshot_metadata(path, snapshot_path)
        metadata["path"] = relative
        metadata["recordedAt"] = utc_now()
        metadata["recordedEpoch"] = reserved_epoch or time.time()
        if reserved_generation is not None:
            metadata["ownershipGeneration"] = reserved_generation

        with project_lock(
            ctx.state_root,
            lock_name=".state.lock",
            wait_seconds=PROJECT_LOCK_WAIT_SECONDS,
        ):
            manifest_path = before_manifest_path(ctx, turn_id)
            manifest = load_before_manifest(ctx, turn_id)
            files = manifest.setdefault("files", {})
            existing_key = manifest_file_key(files, relative)
            if existing_key is not None:
                current = files[existing_key]
                if current.get("exists") is not None:
                    return current
                if current.get("ownershipGeneration") != reserved_generation:
                    return current
                if current.get("writers"):
                    metadata["writers"] = current["writers"]
                if existing_key != relative:
                    del files[existing_key]
            files[relative] = metadata
            atomic_write_json(manifest_path, manifest)
            return metadata


def record_before_path(
    ctx: AuditContext,
    turn_id: str,
    path: Path,
    ownership_generation: str | int | None = None,
) -> dict[str, Any]:
    with project_lock(
        ctx.state_root,
        lock_name=".state.lock",
        wait_seconds=PROJECT_LOCK_WAIT_SECONDS,
    ):
        reserve_before_path_locked(ctx, turn_id, path, ownership_generation)
    return capture_reserved_before_path(ctx, turn_id, path, ownership_generation)


def ownership_path(ctx: AuditContext) -> Path:
    return ctx.state_root / "ownership.json"


def legacy_session_root(ctx: AuditContext, session_id: str) -> Path:
    return ctx.state_root / "sessions" / bridge.sanitize_component(session_id, 80)


def owner_manifest_entry(
    ctx: AuditContext, relative: str, current: dict[str, Any]
) -> dict[str, Any] | None:
    session_id = str(current.get("sessionId") or "")
    turn_id = str(current.get("turnId") or "")
    if not session_id or not turn_id:
        return None
    manifest = read_json_if_exists(
        legacy_session_root(ctx, session_id)
        / "turns"
        / bridge.sanitize_component(turn_id, 96)
        / "before.json",
        {"files": {}},
    )
    files = manifest.get("files", {}) if isinstance(manifest, dict) else {}
    if not isinstance(files, dict):
        return None
    key = manifest_file_key(files, relative)
    value = files.get(key) if key is not None else None
    return value if isinstance(value, dict) else None


def owner_record_is_active_turn(
    ctx: AuditContext, relative: str, current: dict[str, Any]
) -> bool:
    session_id = str(current.get("sessionId") or "")
    turn_id = str(current.get("turnId") or "")
    if not session_id or not turn_id or owner_manifest_entry(ctx, relative, current) is None:
        return False
    session = read_json_if_exists(
        legacy_session_root(ctx, session_id) / "session.json", {}
    )
    if not isinstance(session, dict) or str(session.get("status") or "") == "ended":
        return False
    turns = session.get("turns", {})
    turn_state = turns.get(turn_id, {}) if isinstance(turns, dict) else {}
    if (str(session.get("activeTurnId") or "") != turn_id
        and turn_state.get("status") != "awaiting-child-tools"
        and turn_state.get("auditRole") not in {
        "late-subagent", "unregistered-writer",
    }):
        return False
    return isinstance(turn_state, dict) and str(turn_state.get("status") or "active") in {
        "active",
        "commit-deferred",
        "commit-failed",
        "awaiting-child-tools",
    }


def attach_manifest_generation(
    ctx: AuditContext, relative: str, current: dict[str, Any], generation: str
) -> None:
    session_id = str(current.get("sessionId") or "")
    turn_id = str(current.get("turnId") or "")
    if not session_id or not turn_id:
        return
    path = (
        legacy_session_root(ctx, session_id)
        / "turns"
        / bridge.sanitize_component(turn_id, 96)
        / "before.json"
    )
    manifest = read_json_if_exists(path, {"files": {}})
    files = manifest.get("files", {}) if isinstance(manifest, dict) else {}
    if not isinstance(files, dict):
        return
    key = manifest_file_key(files, relative)
    entry = files.get(key) if key is not None else None
    if isinstance(entry, dict) and entry.get("ownershipGeneration") != generation:
        entry["ownershipGeneration"] = generation
        atomic_write_json(path, manifest)


def new_ownership_epoch() -> str:
    return os.urandom(16).hex()


def ownership_generation_token(epoch: str) -> str:
    return f"{epoch}:{os.urandom(16).hex()}"


def normalize_ownership_registry(
    ctx: AuditContext, registry: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    records = registry.setdefault("files", {})
    generations = registry.get("generations", {})
    if not isinstance(records, dict) or not isinstance(generations, dict):
        raise AuditError("本地所有权记录或 generation 表结构无效")
    normalized_records: dict[str, Any] = {}
    source_schema = int(registry.get("schemaVersion") or 0)
    changed = source_schema != OWNERSHIP_SCHEMA_VERSION
    epoch = registry.get("epoch")
    if source_schema >= OWNERSHIP_SCHEMA_VERSION:
        if not isinstance(epoch, str) or not re.fullmatch(r"[0-9a-f]{32}", epoch):
            raise AuditError("本地所有权 epoch 无效")
    else:
        epoch = new_ownership_epoch()
        changed = True
    for stored_path, current in records.items():
        if not isinstance(current, dict):
            changed = True
            continue
        if not isinstance(current.get("sessionId"), str) or not current.get("sessionId"):
            raise AuditError(f"本地所有权 sessionId 无效：{stored_path}")
        if "path" in current and not isinstance(current.get("path"), str):
            raise AuditError(f"本地所有权 path 无效：{stored_path}")
        if "turnId" in current and not isinstance(current.get("turnId"), str):
            raise AuditError(f"本地所有权 turnId 无效：{stored_path}")
        for field in ("updatedEpoch", "leaseUntilEpoch"):
            if field not in current:
                continue
            value = current.get(field)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
            ):
                raise AuditError(f"本地所有权 {field} 无效：{stored_path}")
        relative = str(current.get("path") or stored_path).replace("\\", "/")
        try:
            candidate = (ctx.project_root / Path(relative)).resolve()
            candidate.relative_to(ctx.project_root.resolve())
        except (OSError, ValueError):
            changed = True
            continue
        session_id = str(current.get("sessionId") or "")
        session_root = legacy_session_root(ctx, session_id)
        legacy_session = read_json_if_exists(session_root / "session.json", {})
        active_turn_id = (
            str(legacy_session.get("activeTurnId") or "")
            if isinstance(legacy_session, dict)
            else ""
        )
        turn_id = str(current.get("turnId") or active_turn_id)
        current["turnId"] = turn_id
        current["path"] = relative
        if "leaseUntilEpoch" not in current:
            current["leaseUntilEpoch"] = (
                float(current.get("updatedEpoch") or 0) + OWNERSHIP_LEASE_SECONDS
            )
            changed = True
        if not turn_id or owner_manifest_entry(ctx, relative, current) is None:
            changed = True
            continue
        key = ownership_key(relative)
        generation = current.get("generation")
        if (
            source_schema >= OWNERSHIP_SCHEMA_VERSION
            and "generation" in current
            and (
                not isinstance(generation, str)
                or not re.fullmatch(r"[0-9a-f]{32}:[0-9a-f]{32}", generation)
            )
        ):
            raise AuditError(f"本地所有权 generation 无效：{stored_path}")
        if (
            not isinstance(generation, str)
            or not generation.startswith(f"{epoch}:")
        ):
            generation = ownership_generation_token(epoch)
            current["generation"] = generation
            current["source"] = "legacy-v8"
            attach_manifest_generation(ctx, relative, current, generation)
            changed = True
        existing = normalized_records.get(key)
        if isinstance(existing, dict):
            same_owner = (
                existing.get("sessionId") == current.get("sessionId")
                and existing.get("turnId") == current.get("turnId")
            )
            if same_owner:
                if float(current.get("updatedEpoch") or 0) > float(
                    existing.get("updatedEpoch") or 0
                ):
                    normalized_records[key] = current
                changed = True
                continue
            winner = max(
                (existing, current),
                key=lambda item: float(item.get("updatedEpoch") or 0),
            )
            generation = ownership_generation_token(epoch)
            winner["generation"] = generation
            attach_manifest_generation(
                ctx, str(winner.get("path") or stored_path), winner, generation
            )
            normalized_records[key] = winner
            changed = True
            continue
        normalized_records[key] = current
        changed = changed or key != stored_path
    registry["files"] = normalized_records
    changed = changed or "generations" in registry
    registry.pop("generations", None)
    registry["epoch"] = epoch
    registry["schemaVersion"] = OWNERSHIP_SCHEMA_VERSION
    if changed:
        registry["updatedAt"] = utc_now()
    return registry, changed


def read_ownership_registry(ctx: AuditContext) -> tuple[dict[str, Any], bool]:
    path = ownership_path(ctx)
    try:
        value = read_json_if_exists(path, {"files": {}}, strict=True)
    except AuditError as exc:
        warning = quarantine_state_file(path, str(exc))
        return {
            "schemaVersion": OWNERSHIP_SCHEMA_VERSION,
            "epoch": new_ownership_epoch(),
            "files": {},
            "recoveryWarnings": [warning],
            "updatedAt": utc_now(),
        }, True
    if not isinstance(value, dict) or not isinstance(value.get("files", {}), dict):
        warning = quarantine_state_file(path, f"本地所有权状态结构无效：{path}")
        return {
            "schemaVersion": OWNERSHIP_SCHEMA_VERSION,
            "epoch": new_ownership_epoch(),
            "files": {},
            "recoveryWarnings": [warning],
            "updatedAt": utc_now(),
        }, True
    if "generations" in value and not isinstance(value.get("generations"), dict):
        warning = quarantine_state_file(path, f"本地所有权 generation 表结构无效：{path}")
        return {
            "schemaVersion": OWNERSHIP_SCHEMA_VERSION,
            "epoch": new_ownership_epoch(),
            "files": {},
            "recoveryWarnings": [warning],
            "updatedAt": utc_now(),
        }, True
    try:
        return normalize_ownership_registry(ctx, value)
    except (AuditError, TypeError, ValueError, OverflowError) as exc:
        warning = quarantine_state_file(path, f"本地所有权状态字段无效：{exc}")
        return {
            "schemaVersion": OWNERSHIP_SCHEMA_VERSION,
            "epoch": new_ownership_epoch(),
            "files": {},
            "recoveryWarnings": [warning],
            "updatedAt": utc_now(),
        }, True


def owner_record_is_live(
    ctx: AuditContext, relative: str, current: dict[str, Any], now_epoch: float
) -> bool:
    lease_until = float(
        current.get("leaseUntilEpoch")
        or float(current.get("updatedEpoch") or 0) + OWNERSHIP_LEASE_SECONDS
    )
    if lease_until <= now_epoch:
        return False
    return owner_record_is_active_turn(ctx, relative, current)


def refresh_owned_files(
    session: dict[str, Any], records: dict[str, Any], session_id: str
) -> None:
    session["ownedFiles"] = sorted(
        str(current.get("path") or relative)
        for relative, current in records.items()
        if isinstance(current, dict) and current.get("sessionId") == session_id
    )


def claim_paths(
    ctx: AuditContext,
    session: dict[str, Any],
    turn_id: str,
    paths: list[Path],
    *,
    allow_live_supersede: bool = False,
    writer: dict[str, Any] | None = None,
    tool_use_id: str | None = None,
) -> ClaimResult:
    registry, _ = read_ownership_registry(ctx)
    for warning in registry.get("recoveryWarnings", []):
        warnings = session.setdefault("stateWarnings", [])
        if warning not in warnings:
            warnings.append(warning)
    if registry.get("recoveryWarnings"):
        write_uncertainty_markers(ctx, turn_id, (), global_scope=True)
    records = registry.setdefault("files", {})
    epoch = str(registry.get("epoch") or "")
    if not re.fullmatch(r"[0-9a-f]{32}", epoch):
        raise AuditError("本地所有权 epoch 无效")
    now_epoch = time.time()
    lease_until = now_epoch + OWNERSHIP_LEASE_SECONDS
    for current in records.values():
        if (
            isinstance(current, dict)
            and current.get("sessionId") == ctx.session_id
            and current.get("turnId") == turn_id
        ):
            current["updatedAt"] = utc_now()
            current["updatedEpoch"] = now_epoch
            current["leaseUntilEpoch"] = lease_until
    claimed: dict[str, str] = {}
    superseded_paths: list[str] = []
    for path in paths:
        relative = relative_path(ctx, path)
        key = ownership_key(relative)
        current = records.get(key)
        writer_conflict = False
        if isinstance(current, dict) and owner_record_is_live(ctx, relative, current, now_epoch):
            current_session = current.get("sessionId")
            current_turn = current.get("turnId")
            foreign_claim = current_session != ctx.session_id or (
                current_turn and current_turn != turn_id
            )
            if foreign_claim and allow_live_supersede:
                superseded_paths.append(relative)
            elif current_session != ctx.session_id:
                raise OwnershipConflict(
                    f"文件正由另一个 Codex 会话记录：{relative} "
                    f"(session {current_session}, turn {current_turn or '-'})"
                )
            elif (
                current_session == ctx.session_id
                and current_turn
                and current_turn != turn_id
            ):
                raise OwnershipConflict(
                    f"文件仍由本 Codex 会话的上一回合记录：{relative} "
                    f"(turn {current_turn})"
                )
            if writer and current_session == ctx.session_id and current_turn == turn_id:
                writer_conflict = bool(current.get("writerBusy")) and (
                    current.get("writerTurnId", current_turn) != writer.get("turnId")
                    or (tool_use_id and current.get("writerToolUseId") and
                        current.get("writerToolUseId") != tool_use_id)
                )
                if writer_conflict and not allow_live_supersede:
                    raise OwnershipConflict(
                        f"文件仍由同一用户回合中的另一项工具写入：{relative} "
                        f"(writer turn {current.get('writerTurnId')}, agent {current.get('writerAgentId') or '-'})"
                    )
                if writer_conflict:
                    superseded_paths.append(relative)
        same_claim = (
            isinstance(current, dict)
            and current.get("sessionId") == ctx.session_id
            and current.get("turnId") == turn_id
            and isinstance(current.get("generation"), str)
            and not writer_conflict
        )
        if same_claim:
            generation = str(current["generation"])
        else:
            generation = ownership_generation_token(epoch)
        records[key] = {
            "sessionId": ctx.session_id,
            "turnId": turn_id,
            "path": relative,
            "generation": generation,
            "branch": ctx.branch,
            "source": "tool-hook",
            "updatedAt": utc_now(),
            "updatedEpoch": now_epoch,
            "leaseUntilEpoch": lease_until,
        }
        if writer:
            records[key].update({
                "writerTurnId": writer.get("turnId"),
                "writerAgentId": writer.get("agentId"),
                "writerToolUseId": tool_use_id,
                "writerBusy": not allow_live_supersede,
            })
        claimed[relative] = generation
    refresh_owned_files(session, records, ctx.session_id)
    registry["schemaVersion"] = OWNERSHIP_SCHEMA_VERSION
    registry["updatedAt"] = utc_now()
    atomic_write_json(ownership_path(ctx), registry)
    return ClaimResult(claimed, superseded_paths)


def release_turn_paths(ctx: AuditContext, session: dict[str, Any], turn_id: str) -> None:
    with project_lock(
        ctx.state_root, lock_name=".state.lock", wait_seconds=TOOL_LOCK_WAIT_SECONDS
    ):
        release_turn_paths_locked(ctx, session, turn_id)


def release_turn_paths_locked(
    ctx: AuditContext, session: dict[str, Any], turn_id: str
) -> None:
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
    with project_lock(
        ctx.state_root, lock_name=".state.lock", wait_seconds=TOOL_LOCK_WAIT_SECONDS
    ):
        release_paths_locked(ctx, session)


def release_paths_locked(
    ctx: AuditContext, session: dict[str, Any] | None = None
) -> None:
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


def renew_turn_ownership(ctx: AuditContext, turn_id: str) -> dict[str, str]:
    with project_lock(
        ctx.state_root, lock_name=".state.lock", wait_seconds=PROJECT_LOCK_WAIT_SECONDS
    ):
        registry, normalized = read_ownership_registry(ctx)
        records = registry.setdefault("files", {})
        now_epoch = time.time()
        changed = normalized
        for current in records.values():
            if (
                isinstance(current, dict)
                and current.get("sessionId") == ctx.session_id
                and current.get("turnId") == turn_id
            ):
                current["updatedAt"] = utc_now()
                current["updatedEpoch"] = now_epoch
                current["leaseUntilEpoch"] = now_epoch + OWNERSHIP_LEASE_SECONDS
                changed = True
        if changed:
            registry["updatedAt"] = utc_now()
            atomic_write_json(ownership_path(ctx), registry)
        return {
            str(relative): str(current.get("generation") or "")
            for relative, current in records.items()
            if isinstance(current, dict) and current.get("generation")
        }


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


def load_deferred_after_record(
    ctx: AuditContext, turn_id: str
) -> dict[str, Any] | None:
    path = deferred_after_record_path(ctx, turn_id)
    if not path.is_file():
        return None
    try:
        record = read_json_if_exists(path, {}, strict=True)
    except AuditError as exc:
        quarantine_state_file(path, str(exc))
        return None
    if (
        not isinstance(record, dict)
        or record.get("schemaVersion") != DEFERRED_AFTER_SCHEMA_VERSION
        or record.get("sessionId") != ctx.session_id
        or record.get("turnId") != turn_id
        or not isinstance(record.get("files"), dict)
        or not isinstance(record.get("warnings", []), list)
        or (
            record.get("ownershipSealedAt") is not None
            and not isinstance(record.get("ownershipSealedAt"), str)
        )
    ):
        quarantine_state_file(path, f"延迟收尾快照结构或上下文无效：{path}")
        return None
    for relative, entry in record["files"].items():
        try:
            candidate = (ctx.project_root / Path(relative)).resolve()
            candidate.relative_to(ctx.project_root)
        except (OSError, TypeError, ValueError):
            quarantine_state_file(path, f"延迟收尾快照路径无效：{path}")
            return None
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
            or not isinstance(entry, dict)
            or not isinstance(entry.get("after"), dict)
            or entry.get("ownershipAttribution")
            not in {None, "exact", "superseded"}
        ):
            quarantine_state_file(path, f"延迟收尾快照条目无效：{path}")
            return None
    return record


def capture_deferred_after(ctx: AuditContext, turn_id: str) -> list[str]:
    with project_lock(
        turn_root(ctx, turn_id),
        lock_name=".deferred-after.lock",
        wait_seconds=PROJECT_LOCK_WAIT_SECONDS,
    ):
        existing = load_deferred_after_record(ctx, turn_id)
        if existing is not None:
            return [str(item) for item in existing.get("warnings", []) if str(item)]
        manifest = load_before_manifest(ctx, turn_id)
        warnings = [
            str(item.get("code") or "")
            for item in manifest.get("stateWarnings", [])
            if isinstance(item, dict) and str(item.get("code") or "")
        ]
        files: dict[str, Any] = {}
        snapshot_root = turn_root(ctx, turn_id) / "deferred-after"
        for relative in sorted(manifest.get("files", {})):
            path = ctx.project_root / Path(relative)
            try:
                after, data = after_snapshot(path)
                if data is not None:
                    snapshot_path = (
                        snapshot_root / f"{sha256_bytes(relative.encode('utf-8'))}.bin"
                    )
                    atomic_write_bytes(snapshot_path, data)
                    after = {**after, "snapshotPath": str(snapshot_path)}
                files[relative] = {"after": after}
            except (AuditError, OSError):
                files[relative] = {"after": unknown_after_snapshot()}
                warnings.append("deferred-after-snapshot-unavailable")
        warnings = sorted(set(warnings))
        atomic_write_json(
            deferred_after_record_path(ctx, turn_id),
            {
                "schemaVersion": DEFERRED_AFTER_SCHEMA_VERSION,
                "sessionId": ctx.session_id,
                "turnId": turn_id,
                "capturedAt": utc_now(),
                "files": files,
                "warnings": warnings,
            },
        )
        return warnings


def unknown_after_snapshot() -> dict[str, Any]:
    return {
        "exists": None,
        "size": None,
        "sha256": None,
        "binary": None,
        "captured": False,
        "snapshotPath": None,
    }


def seal_deferred_after_ownership_locked(ctx: AuditContext, turn_id: str) -> bool:
    record = load_deferred_after_record(ctx, turn_id)
    if record is None:
        return False
    if record.get("ownershipSealedAt"):
        return True
    manifest = load_before_manifest(ctx, turn_id)
    registry, _ = read_ownership_registry(ctx)
    owners = registry.setdefault("files", {})
    files = record.setdefault("files", {})
    for relative, before in manifest.get("files", {}).items():
        entry = files.setdefault(relative, {"after": unknown_after_snapshot()})
        generation = before.get("ownershipGeneration") if isinstance(before, dict) else None
        current = owners.get(ownership_key(relative))
        ownership_current = generation is None or (
            isinstance(generation, (str, int))
            and not isinstance(generation, bool)
            and isinstance(current, dict)
            and current.get("sessionId") == ctx.session_id
            and current.get("turnId") == turn_id
            and current.get("generation") == generation
        )
        entry["ownershipAttribution"] = "exact" if ownership_current else "superseded"
    record["ownershipSealedAt"] = utc_now()
    atomic_write_json(deferred_after_record_path(ctx, turn_id), record)
    return True


def deferred_or_live_after_snapshot(
    ctx: AuditContext,
    turn_id: str,
    relative: str,
    path: Path,
    deferred: dict[str, Any] | None,
    *,
    deferred_required: bool,
) -> tuple[dict[str, Any], bytes | None, bool]:
    unknown_after = unknown_after_snapshot()

    def unavailable() -> tuple[dict[str, Any], bytes | None, bool]:
        if deferred_required:
            return unknown_after, None, False
        after, data = after_snapshot(path)
        return after, data, True

    entry = deferred.get("files", {}).get(relative) if deferred is not None else None
    if not isinstance(entry, dict):
        return unavailable()
    after = dict(entry["after"])
    if not isinstance(after.get("exists"), bool):
        return unavailable()
    data: bytes | None = None
    if after.get("exists") and after.get("captured"):
        snapshot_path = Path(str(after.get("snapshotPath") or ""))
        try:
            snapshot_path.resolve().relative_to(turn_root(ctx, turn_id).resolve())
        except (OSError, ValueError):
            return unavailable()
        if not snapshot_path.is_file():
            return unavailable()
        try:
            data = snapshot_path.read_bytes()
        except OSError:
            return unavailable()
        if sha256_bytes(data) != after.get("sha256"):
            return unavailable()
    return after, data, True


def action_for(before: dict[str, Any], after: dict[str, Any]) -> str:
    if before.get("exists") is None:
        return "unknown"
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


def staged_turn_paths(ctx: AuditContext, parent: str, paths: list[str]) -> set[str]:
    if not paths:
        return set()
    completed = bridge.run_command(
        [
            ctx.git_exe,
            "-C",
            str(ctx.history_root),
            "diff",
            "--cached",
            "--name-only",
            "-z",
            parent,
            "--",
            *paths,
        ],
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise AuditError(f"无法核对本回合 Git 暂存状态：{message}")
    return {
        item.decode("utf-8", errors="surrogateescape")
        for item in completed.stdout.split(b"\0")
        if item
    }


def index_entries(
    ctx: AuditContext,
    paths: Iterable[str],
    env: dict[str, str] | None = None,
) -> dict[str, str | None]:
    entries: dict[str, str | None] = {}
    for path in paths:
        completed = bridge.run_command(
            [ctx.git_exe, "-C", str(ctx.history_root), "rev-parse", f":{path}"],
            env=env,
            check=False,
        )
        entries[path] = (
            completed.stdout.decode("ascii", errors="replace").strip()
            if completed.returncode == 0
            else None
        )
    return entries


def commit_tree_from_entries(
    ctx: AuditContext,
    parent: str,
    entries: dict[str, str | None],
    message: str,
) -> str:
    ctx.session_root.mkdir(parents=True, exist_ok=True)
    handle, index_name = tempfile.mkstemp(prefix="git-index-recovery-", dir=str(ctx.session_root))
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
                    env=env,
                    check=False,
                )
            else:
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
        tree = bridge.run_command(
            [ctx.git_exe, "-C", str(ctx.history_root), "write-tree"], env=env
        ).stdout.decode("ascii").strip()
        return bridge.run_command(
            [ctx.git_exe, "-C", str(ctx.history_root), "commit-tree", tree, "-p", parent],
            input_bytes=message.encode("utf-8"),
            env=env,
        ).stdout.decode("ascii").strip()
    finally:
        try:
            os.unlink(index_name)
        except FileNotFoundError:
            pass


def sync_main_index(
    ctx: AuditContext,
    entries: dict[str, str | None],
    index_file: Path | None = None,
) -> None:
    env = {**os.environ, "GIT_INDEX_FILE": str(index_file)} if index_file else None
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
                env=env,
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
            ],
            env=env,
        )


def index_lock_identity(path: Path) -> dict[str, int] | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or not info.st_ino:
        return None
    return {"device": info.st_dev, "inode": info.st_ino, "ctimeNs": info.st_ctime_ns}


def index_lock_owner_exited(pid: int) -> bool:
    # A live/reused PID or an inaccessible process is never evidence of a stale lock.
    if not 0 < pid <= 0x7FFFFFFF or pid == os.getpid():
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return ctypes.get_last_error() == 87  # ERROR_INVALID_PARAMETER: PID no longer exists.
        try:
            return kernel.WaitForSingleObject(handle, 0) == 0
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False
    return False


def recover_owned_index_lock(ctx: AuditContext, lock_path: Path, owner_path: Path) -> bool:
    """Called only under the index-owner OS lease; unknown Git locks stay untouched."""
    owner = read_json_if_exists(owner_path, {})
    if not isinstance(owner, dict) or owner.get("schemaVersion") != 1:
        return False
    if (
        owner.get("historyRoot") != os.path.normcase(str(ctx.history_root.resolve()))
        or owner.get("lockPath") != os.path.normcase(str(lock_path.resolve()))
        or not isinstance(owner.get("token"), str)
        or not re.fullmatch(r"[0-9a-f]{32}", owner["token"])
        or type(owner.get("pid")) is not int
    ):
        return False
    identity = index_lock_identity(lock_path)
    if identity is None or owner.get("identity") != identity or not index_lock_owner_exited(owner["pid"]):
        return False
    # Recheck after the process query so an externally replaced lock is not removed.
    if index_lock_identity(lock_path) != identity:
        return False
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        return False
    owner_path.unlink(missing_ok=True)
    return True


def sync_main_index_safely(
    ctx: AuditContext,
    target_entries: dict[str, str | None],
    expected_entries: dict[str, str | None],
    candidate_entries: dict[str, str | None] | None = None,
) -> list[str]:
    # Unlike index.lock, this kernel lease is released even when a hook is killed.
    # It is separate from the caller's .project.lock to avoid reentrant locking.
    with project_lock(ctx.state_root, lock_name=".index-owner.lock"):
        return sync_main_index_with_owner(ctx, target_entries, expected_entries, candidate_entries)


def sync_main_index_with_owner(
    ctx: AuditContext,
    target_entries: dict[str, str | None],
    expected_entries: dict[str, str | None],
    candidate_entries: dict[str, str | None],
) -> list[str]:
    git_dir = ctx.history_root / ".git"
    index_path = git_dir / "index"
    lock_path = git_dir / "index.lock"
    owner_path = ctx.state_root / "index-lock-owner.json"
    deadline = time.monotonic() + PROJECT_LOCK_WAIT_SECONDS
    while True:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            if recover_owned_index_lock(ctx, lock_path, owner_path):
                continue
            if time.monotonic() >= deadline:
                raise AuditError(f"等待 Git index 锁超时：{lock_path}") from exc
            time.sleep(0.05)
            continue
        else:
            break

    temporary_index: Path | None = None
    committed = False
    identity = index_lock_identity(lock_path)
    owner = {
        "schemaVersion": 1,
        "token": os.urandom(16).hex(),
        "pid": os.getpid(),
        "historyRoot": os.path.normcase(str(ctx.history_root.resolve())),
        "lockPath": os.path.normcase(str(lock_path.resolve())),
        "identity": identity,
    }
    try:
        os.close(descriptor)
        descriptor = None
        atomic_write_json(owner_path, owner)
        ctx.session_root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="git-main-index-", dir=str(ctx.session_root)
        )
        temporary_index = Path(temporary_name)
        os.close(descriptor)
        descriptor = None
        if index_path.is_file():
            shutil.copyfile(index_path, temporary_index)
        else:
            temporary_index.unlink()
        temporary_env = {**os.environ, "GIT_INDEX_FILE": str(temporary_index)}
        current = index_entries(ctx, target_entries, env=temporary_env)
        safe: dict[str, str | None] = {}
        skipped: list[str] = []
        for path, target in target_entries.items():
            unchanged_after_candidate = (
                candidate_entries is None or candidate_entries.get(path) == target
            )
            if current.get(path) == target or (
                current.get(path) == expected_entries.get(path) and unchanged_after_candidate
            ):
                safe[path] = target
            else:
                skipped.append(path)
        if not safe:
            return skipped
        sync_main_index(ctx, safe, temporary_index)
        shutil.copyfile(temporary_index, lock_path)
        owner["identity"] = index_lock_identity(lock_path)
        atomic_write_json(owner_path, owner)
        replace_deadline = time.monotonic() + PROJECT_LOCK_WAIT_SECONDS
        while True:
            try:
                os.replace(lock_path, index_path)
                break
            except PermissionError:
                if time.monotonic() >= replace_deadline:
                    raise
                time.sleep(0.05)
        committed = True
        return skipped
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_index is not None:
            try:
                temporary_index.unlink()
            except FileNotFoundError:
                pass
        if not committed:
            current = index_lock_identity(lock_path)
            if current is not None and identity is not None and (
                current["device"], current["inode"]
            ) == (identity["device"], identity["inode"]):
                lock_path.unlink(missing_ok=True)
        remaining_owner = read_json_if_exists(owner_path, {})
        if isinstance(remaining_owner, dict) and remaining_owner.get("token") == owner["token"]:
            owner_path.unlink(missing_ok=True)


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


def commit_is_ancestor(ctx: AuditContext, ancestor: str, descendant: str) -> bool:
    completed = bridge.run_command(
        [
            ctx.git_exe,
            "-C",
            str(ctx.history_root),
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
        ],
        check=False,
    )
    return completed.returncode == 0


def turn_commit_message(ctx: AuditContext, event_record: dict[str, Any]) -> str:
    change_summary = str(event_record["changeSummary"])
    subject = f"[Codex] {change_summary}"[:120]
    if event_record.get("descriptionVersion") == 1:
        return "\n".join([
            subject, "", *description.body_lines(event_record),
            f"Request: {event_record.get('requestText') or event_record.get('promptSummary') or 'Codex file changes'}",
            f"Request-Capture: {event_record.get('requestCapture') or 'legacy-summary-only'}",
            f"Prompt-SHA256: {event_record.get('promptSha256') or '-'}",
            f"Change-Summary-Source: {event_record['changeSummarySource']}",
            f"Description-Status: {event_record['descriptionStatus']}",
            f"Diff-Digest: {event_record['descriptionDiffDigest']}",
            f"Session: {event_record['sessionId']}", f"Turn: {event_record['turnId']}",
            f"Recorded-At: {event_record['recordedAt']}",
            f"Audit-Coverage: {event_record.get('auditCoverage') or 'exact'}",
            "P4 mode: read-only", "Audit-Notes-Ref: refs/notes/codex-p4-diff", "",
        ])
    # Prepared legacy transactions retain their original description format.
    body = [
        subject,
        "",
        f"Change: {change_summary}",
        f"Change-Summary-Source: {event_record['changeSummarySource']}",
        f"Request: {event_record.get('promptSummary') or 'Codex file changes'}",
        f"Session: {event_record['sessionId']}",
        f"Turn: {event_record['turnId']}",
        f"Recorded-At: {event_record['recordedAt']}",
        f"Prompt-Recorded-At: {event_record.get('promptRecordedAt') or '-'}",
        f"Project: {ctx.project_root}",
        f"Audit-Coverage: {event_record.get('auditCoverage') or 'exact'}",
        "P4 mode: read-only",
        "Files:",
    ]
    for item in event_record.get("files", []):
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
    return "\n".join(body)


def find_existing_turn_commit(ctx: AuditContext, turn_id: str) -> str | None:
    completed = bridge.run_command(
        [
            ctx.git_exe,
            "-C",
            str(ctx.history_root),
            "log",
            ctx.branch,
            "-n",
            "500",
            "--format=%H%x1f%B%x1e",
        ],
        check=False,
    )
    if completed.returncode != 0:
        return None
    for chunk in completed.stdout.decode("utf-8", errors="replace").split("\x1e"):
        if "\x1f" not in chunk:
            continue
        commit, message = chunk.strip().split("\x1f", 1)
        lines = {line.strip() for line in message.splitlines()}
        if f"Session: {ctx.session_id}" in lines and f"Turn: {turn_id}" in lines:
            return commit.strip()
    return None


def prepare_turn_transaction(
    ctx: AuditContext,
    session: dict[str, Any],
    turn_id: str,
    prompt: dict[str, Any],
    changes: list[dict[str, Any]],
    patches: list[str],
    stop_event: dict[str, Any],
) -> dict[str, Any]:
    parent = current_history_head(ctx)
    turn_paths = sorted(str(change["path"]) for change in changes)
    staged = staged_turn_paths(ctx, parent, turn_paths)
    updates: dict[str, bytes | None] = {}

    for change in changes:
        relative = str(change["path"])
        before = change["before"]
        after = change["after"]
        before_data = change.pop("_beforeData", None)
        after_data = change.pop("_afterData", None)
        parent_data = git_blob_at(ctx, parent, relative)
        history_state = "receipt-only"
        if change.get("ownershipAttribution") == "deferred-after-unavailable":
            history_state = "deferred-after-unavailable-receipt-only"
        elif change.get("ownershipAttribution") == "uncertain":
            history_state = "concurrent-uncertainty-receipt-only"
        elif change.get("ownershipAttribution") == "superseded":
            history_state = "ownership-superseded-receipt-only"
        elif before.get("exists") is None:
            history_state = "after-only-receipt"
        elif relative in staged:
            history_state = "staged-receipt-only"
        elif after.get("captured") and after_data is not None:
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

    summary_record = build_change_summary(ctx, turn_id, stop_event, changes)
    if summary_record["changeSummarySource"] == "structured-ai" and (
        re.sub(r"[\s。.!！]+", "", summary_record["changeSummary"]).casefold()
        == re.sub(r"[\s。.!！]+", "", str(prompt.get("summary") or "")).casefold()
    ):
        summary_record.update(changeSummary=fallback_change_summary(changes),
                              changeSummarySource="files", descriptionStatus="request-copy",
                              changeDescription=None)
    warning_records = coverage_warnings(ctx, turn_id)
    warning_codes = sorted(
        {str(item.get("code") or "") for item in warning_records if item.get("code")}
    )
    summary_record["auditCoverage"] = "partial" if warning_codes else "exact"
    summary_record["coverageWarnings"] = warning_codes
    event_record = {
        **summary_record,
        "schemaVersion": bridge.SCHEMA_VERSION,
        "generatedBy": bridge.TOOL_NAME,
        "sessionId": ctx.session_id,
        "turnId": turn_id,
        "sessionSequence": int(session.get("nextSequence") or 1),
        "parentTurnId": session.get("turns", {}).get(turn_id, {}).get("parentTurnId"),
        "branch": ctx.branch,
        "projectRoot": str(ctx.project_root),
        "promptSummary": prompt.get("summary"),
        "requestText": prompt.get("requestText"),
        "requestCapture": prompt.get("requestCapture"),
        "promptSha256": prompt.get("sha256"),
        "promptRecordedAt": prompt.get("recordedAt"),
        "recordedAt": utc_now(),
        "p4Mode": "read-only",
        "auditCoverage": "partial" if warning_codes else "exact",
        "coverageWarnings": warning_records,
        "stateWarnings": list(session.get("stateWarnings") or []),
        "files": changes,
        "textPatch": "\n".join(patches) if patches else None,
    }
    message = turn_commit_message(ctx, event_record)
    existing = find_existing_turn_commit(ctx, turn_id)
    if existing:
        existing_parent = bridge.run_command(
            [ctx.git_exe, "-C", str(ctx.history_root), "rev-parse", f"{existing}^"]
        ).stdout.decode("ascii").strip()
        parent_entries = entries_at_commit(ctx, existing_parent, turn_paths)
        candidate_entries = entries_at_commit(ctx, existing, turn_paths)
        entries = {
            path: candidate_entries[path]
            for path in turn_paths
            if candidate_entries[path] != parent_entries[path]
        }
        parent = existing_parent
        commit = existing
    else:
        commit, entries = commit_tree(ctx, parent, updates, message)
    expected_index = entries_at_commit(ctx, parent, entries)
    transaction = {
        "schemaVersion": TRANSACTION_SCHEMA_VERSION,
        "transactionId": sha256_bytes(f"{ctx.session_id}\0{turn_id}".encode("utf-8")),
        "sessionId": ctx.session_id,
        "turnId": turn_id,
        "branch": ctx.branch,
        "phase": "prepared",
        "parent": parent,
        "candidateCommit": commit,
        "message": message,
        "updateEntries": entries,
        "indexBefore": expected_index,
        "auditNotesRef": "refs/notes/codex-p4-diff",
        "auditRecord": event_record,
        "summaryRecord": summary_record,
        "attempts": 0,
        "createdAt": utc_now(),
        "updatedAt": utc_now(),
    }
    queue_turn_recovery(ctx, session, turn_id, "transaction-prepared")
    atomic_write_json(transaction_path(ctx, turn_id), transaction)
    return transaction


def record_transaction_warning(
    transaction: dict[str, Any], code: str, paths: Iterable[str] = ()
) -> None:
    audit_record = transaction.setdefault("auditRecord", {})
    transaction_warnings = audit_record.setdefault("transactionWarnings", [])
    if code not in transaction_warnings:
        transaction_warnings.append(code)
    audit_record["auditCoverage"] = "partial"
    coverage = audit_record.setdefault("coverageWarnings", [])
    if not any(
        (item.get("code") if isinstance(item, dict) else item) == code
        for item in coverage
    ):
        warning: dict[str, Any] = {
            "code": code,
            "source": "git-transaction",
            "recordedAt": utc_now(),
        }
        normalized_paths = sorted({str(path) for path in paths if str(path)})
        if normalized_paths:
            warning["paths"] = normalized_paths
        coverage.append(warning)
    summary = transaction.setdefault("summaryRecord", {})
    summary["auditCoverage"] = "partial"
    summary_warnings = summary.setdefault("coverageWarnings", [])
    if code not in summary_warnings:
        summary_warnings.append(code)


def rebase_prepared_transaction(
    ctx: AuditContext, turn_id: str, transaction: dict[str, Any], head: str
) -> dict[str, Any]:
    old_parent = str(transaction["parent"])
    original_entries = dict(transaction.get("updateEntries") or {})
    old_parent_entries = entries_at_commit(ctx, old_parent, original_entries)
    head_entries = entries_at_commit(ctx, head, original_entries)
    current_index = index_entries(ctx, original_entries)
    safe_entries: dict[str, str | None] = {}
    drifted: set[str] = set()
    for path, target in original_entries.items():
        if head_entries.get(path) == old_parent_entries.get(path) and current_index.get(path) == head_entries.get(path):
            safe_entries[path] = target
        else:
            drifted.add(path)
    audit_record = transaction["auditRecord"]
    for change in audit_record.get("files", []):
        if str(change.get("path")) in drifted:
            change["historyState"] = "recovery-drifted-receipt-only"
    if drifted:
        record_transaction_warning(
            transaction, "branch-advanced-paths-left-receipt-only", drifted
        )
        description.invalidate(audit_record, "recovery-drifted", fallback_change_summary)
        if audit_record.get("descriptionVersion") == 1:
            for key in ("changeSummary", "changeSummarySource", "descriptionStatus", "changeDescription"):
                transaction["summaryRecord"][key] = audit_record[key]
    message = turn_commit_message(ctx, audit_record)
    candidate = commit_tree_from_entries(ctx, head, safe_entries, message)
    transaction.update(
        {
            "phase": "prepared",
            "parent": head,
            "candidateCommit": candidate,
            "message": message,
            "updateEntries": safe_entries,
            "indexBefore": {path: current_index.get(path) for path in safe_entries},
            "auditRecord": audit_record,
            "updatedAt": utc_now(),
        }
    )
    atomic_write_json(transaction_path(ctx, turn_id), transaction)
    return transaction


def resume_turn_transaction(
    ctx: AuditContext, turn_id: str, transaction: dict[str, Any]
) -> tuple[str, str, dict[str, Any]]:
    candidate = str(transaction["candidateCommit"])
    if transaction.get("phase") == "complete":
        return candidate, str(transaction["auditNotesRef"]), dict(transaction["summaryRecord"])

    transaction["attempts"] = int(transaction.get("attempts") or 0) + 1
    transaction["updatedAt"] = utc_now()
    atomic_write_json(transaction_path(ctx, turn_id), transaction)

    if transaction.get("phase") == "prepared":
        parent = str(transaction["parent"])
        head = current_history_head(ctx)
        if head == parent:
            bridge.run_command(
                [
                    ctx.git_exe,
                    "-C",
                    str(ctx.history_root),
                    "update-ref",
                    f"refs/heads/{ctx.branch}",
                    candidate,
                    parent,
                ]
            )
        elif head != candidate and not commit_is_ancestor(ctx, candidate, head):
            transaction = rebase_prepared_transaction(ctx, turn_id, transaction, head)
            candidate = str(transaction["candidateCommit"])
            parent = str(transaction["parent"])
            bridge.run_command(
                [
                    ctx.git_exe,
                    "-C",
                    str(ctx.history_root),
                    "update-ref",
                    f"refs/heads/{ctx.branch}",
                    candidate,
                    parent,
                ]
            )
        transaction["phase"] = "ref-updated"
        transaction["updatedAt"] = utc_now()
        atomic_write_json(transaction_path(ctx, turn_id), transaction)

    if transaction.get("phase") == "ref-updated":
        attach_audit_note(ctx, candidate, transaction["auditRecord"])
        transaction["phase"] = "note-updated"
        transaction["updatedAt"] = utc_now()
        atomic_write_json(transaction_path(ctx, turn_id), transaction)

    if transaction.get("phase") == "note-updated":
        update_paths = list((transaction.get("updateEntries") or {}).keys())
        current_head = current_history_head(ctx)
        target_entries = entries_at_commit(ctx, current_head, update_paths)
        candidate_entries = entries_at_commit(ctx, candidate, update_paths)
        skipped = sync_main_index_safely(
            ctx,
            target_entries,
            dict(transaction.get("indexBefore") or {}),
            candidate_entries,
        )
        if skipped:
            record_transaction_warning(
                transaction, "index-changed-paths-left-untouched", skipped
            )
            transaction["indexSkippedPaths"] = skipped
            attach_audit_note(ctx, candidate, transaction["auditRecord"])
        transaction["phase"] = "index-synced"
        transaction["updatedAt"] = utc_now()
        atomic_write_json(transaction_path(ctx, turn_id), transaction)

    if transaction.get("phase") == "index-synced":
        transaction["phase"] = "complete"
        transaction["completedAt"] = utc_now()
        transaction["updatedAt"] = utc_now()
        atomic_write_json(transaction_path(ctx, turn_id), transaction)
    return candidate, str(transaction["auditNotesRef"]), dict(transaction["summaryRecord"])


def transaction_is_valid(
    ctx: AuditContext, turn_id: str, transaction: Any
) -> bool:
    if not isinstance(transaction, dict):
        return False
    expected_id = sha256_bytes(f"{ctx.session_id}\0{turn_id}".encode("utf-8"))
    if transaction.get("schemaVersion") != TRANSACTION_SCHEMA_VERSION:
        return False
    if transaction.get("transactionId") != expected_id:
        return False
    if transaction.get("sessionId") != ctx.session_id:
        return False
    if transaction.get("turnId") != turn_id or transaction.get("branch") != ctx.branch:
        return False
    if transaction.get("phase") not in {
        "prepared",
        "ref-updated",
        "note-updated",
        "index-synced",
        "complete",
    }:
        return False
    for key in ("parent", "candidateCommit", "message", "auditNotesRef"):
        if not isinstance(transaction.get(key), str) or not transaction[key]:
            return False
    if not all(
        re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(transaction[key]))
        for key in ("parent", "candidateCommit")
    ):
        return False
    if transaction.get("auditNotesRef") != "refs/notes/codex-p4-diff":
        return False
    for key in ("updateEntries", "indexBefore"):
        entries = transaction.get(key)
        if not isinstance(entries, dict) or any(
            not isinstance(path, str) or (blob is not None and not isinstance(blob, str))
            for path, blob in entries.items()
        ):
            return False
        for path, blob in entries.items():
            try:
                (ctx.project_root / Path(path)).resolve().relative_to(ctx.project_root)
            except (OSError, ValueError):
                return False
            if not path or Path(path).is_absolute():
                return False
            if blob is not None and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", blob):
                return False
    audit_record = transaction.get("auditRecord")
    summary_record = transaction.get("summaryRecord")
    if not isinstance(audit_record, dict) or not isinstance(audit_record.get("files"), list):
        return False
    if any(
        audit_record.get(key) != value
        for key, value in (
            ("sessionId", ctx.session_id),
            ("turnId", turn_id),
            ("branch", ctx.branch),
        )
    ):
        return False
    if not isinstance(summary_record, dict):
        return False
    if any(
        not isinstance(summary_record.get(key), str) or not summary_record[key]
        for key in ("changeSummary", "changeSummarySource")
    ):
        return False
    if not isinstance(summary_record.get("coverageWarnings", []), list) or any(
        not isinstance(item, str)
        for item in summary_record.get("coverageWarnings", [])
    ):
        return False
    try:
        return int(transaction.get("attempts") or 0) >= 0
    except (TypeError, ValueError, OverflowError):
        return False


def load_turn_transaction(
    ctx: AuditContext,
    session: dict[str, Any],
    turn_id: str,
) -> dict[str, Any] | None:
    path = transaction_path(ctx, turn_id)
    if not path.is_file():
        return None
    try:
        transaction = read_json_if_exists(path, {}, strict=True)
    except AuditError as exc:
        warning = quarantine_state_file(path, str(exc))
        session.setdefault("stateWarnings", []).append(warning)
        return None
    if not transaction_is_valid(ctx, turn_id, transaction):
        warning = quarantine_state_file(path, f"Git 收尾事务结构或上下文无效：{path}")
        session.setdefault("stateWarnings", []).append(warning)
        return None
    return transaction


def pending_recovery_turn_ids(session: dict[str, Any]) -> list[str]:
    pending = session.setdefault("pendingRecoveryTurnIds", [])
    if not isinstance(pending, list):
        pending = []
        session["pendingRecoveryTurnIds"] = pending
    normalized: list[str] = []
    for value in pending:
        turn_id = str(value or "").strip()
        if turn_id and turn_id not in normalized:
            normalized.append(turn_id)
    session["pendingRecoveryTurnIds"] = normalized
    return normalized


def recovery_marker_path(ctx: AuditContext, session_id: str, turn_id: str) -> Path:
    key = sha256_bytes(f"{session_id}\0{turn_id}".encode("utf-8"))
    return ctx.state_root / "recovery" / f"{key}.json"


def recovery_context(ctx: AuditContext, session_id: str) -> AuditContext:
    return AuditContext(
        p4=ctx.p4,
        project_root=ctx.project_root,
        session_id=session_id,
        branch=ctx.branch,
        history_root=ctx.history_root,
        state_root=ctx.state_root,
        session_root=ctx.state_root
        / "sessions"
        / bridge.sanitize_component(session_id, 80),
        git_exe=ctx.git_exe,
    )


def queue_turn_recovery(
    ctx: AuditContext,
    session: dict[str, Any],
    turn_id: str,
    reason: str = "pending-finalization",
) -> None:
    pending = pending_recovery_turn_ids(session)
    if turn_id not in pending:
        pending.append(turn_id)
    marker_path = recovery_marker_path(ctx, ctx.session_id, turn_id)
    existing = read_json_if_exists(marker_path, {})
    created_at = (
        str(existing.get("createdAt") or "") if isinstance(existing, dict) else ""
    )
    atomic_write_json(
        marker_path,
        {
            "schemaVersion": RECOVERY_MARKER_SCHEMA_VERSION,
            "sessionId": ctx.session_id,
            "turnId": turn_id,
            "reason": reason,
            "createdAt": created_at or utc_now(),
            "updatedAt": utc_now(),
        },
    )


def clear_turn_recovery(
    ctx: AuditContext, session: dict[str, Any], turn_id: str
) -> None:
    pending = pending_recovery_turn_ids(session)
    session["pendingRecoveryTurnIds"] = [item for item in pending if item != turn_id]


def delete_recovery_marker(ctx: AuditContext, turn_id: str) -> None:
    try:
        recovery_marker_path(ctx, ctx.session_id, turn_id).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        # A stale marker is safe: the next recovery pass observes committed state.
        pass


def global_recovery_markers(ctx: AuditContext) -> list[tuple[AuditContext, str]]:
    root = ctx.state_root / "recovery"
    if not root.is_dir():
        return []
    candidates: list[tuple[str, AuditContext, str]] = []
    for path in sorted(root.glob("*.json")):
        try:
            marker = read_json_if_exists(path, {}, strict=True)
        except AuditError as exc:
            quarantine_state_file(path, str(exc))
            continue
        if not isinstance(marker, dict):
            quarantine_state_file(path, f"恢复标记结构无效：{path}")
            continue
        session_id = str(marker.get("sessionId") or "").strip()
        turn_id = str(marker.get("turnId") or "").strip()
        expected = (
            recovery_marker_path(ctx, session_id, turn_id)
            if session_id and turn_id
            else None
        )
        if (
            marker.get("schemaVersion") != RECOVERY_MARKER_SCHEMA_VERSION
            or expected is None
            or expected.resolve() != path.resolve()
        ):
            quarantine_state_file(path, f"恢复标记上下文无效：{path}")
            continue
        candidates.append(
            (
                str(marker.get("createdAt") or ""),
                recovery_context(ctx, session_id),
                turn_id,
            )
        )
    return [(candidate_ctx, turn_id) for _, candidate_ctx, turn_id in sorted(candidates)]


def discover_turn_transactions(
    ctx: AuditContext, session: dict[str, Any]
) -> list[str]:
    discovered: list[tuple[int, str, str]] = []
    turns_root = ctx.session_root / "turns"
    if not turns_root.is_dir():
        return []
    for path in sorted(turns_root.glob("*/transaction.json")):
        try:
            transaction = read_json_if_exists(path, {}, strict=True)
        except AuditError as exc:
            warning = quarantine_state_file(path, str(exc))
            session.setdefault("stateWarnings", []).append(warning)
            continue
        turn_id = str(transaction.get("turnId") or "") if isinstance(transaction, dict) else ""
        expected_path = transaction_path(ctx, turn_id) if turn_id else None
        valid_path = expected_path is not None and expected_path.resolve() == path.resolve()
        if not valid_path or not transaction_is_valid(ctx, turn_id, transaction):
            warning = quarantine_state_file(
                path, f"发现无法恢复的 Git 收尾事务：{path}"
            )
            session.setdefault("stateWarnings", []).append(warning)
            continue
        audit_record = transaction.get("auditRecord", {})
        try:
            sequence = int(audit_record.get("sessionSequence") or 0)
        except (TypeError, ValueError, OverflowError):
            sequence = 0
        discovered.append((sequence, str(transaction.get("createdAt") or ""), turn_id))
    ordered: list[str] = []
    for _, _, turn_id in sorted(discovered):
        if turn_id in ordered:
            continue
        ordered.append(turn_id)
        turn_state = session.setdefault("turns", {}).setdefault(turn_id, {})
        if turn_state.get("status") != "committed":
            turn_state.setdefault("status", "commit-failed")
            turn_state.setdefault("recoveredFromTransactionAt", utc_now())
            queue_turn_recovery(ctx, session, turn_id, "transaction-discovered")
    return ordered


def create_turn_commit(
    ctx: AuditContext,
    session: dict[str, Any],
    turn_id: str,
    prompt: dict[str, Any],
    changes: list[dict[str, Any]],
    patches: list[str],
    stop_event: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    transaction = load_turn_transaction(ctx, session, turn_id)
    if transaction is None:
        transaction = prepare_turn_transaction(
            ctx, session, turn_id, prompt, changes, patches, stop_event
        )
    return resume_turn_transaction(ctx, turn_id, transaction)


def release_failed_turn(
    ctx: AuditContext,
    turn_id: str,
    error: BaseException,
    status: str = "audit-failed-released",
    *,
    keep_active: bool = False,
) -> None:
    with project_lock(
        ctx.state_root, lock_name=".state.lock", wait_seconds=PROJECT_LOCK_WAIT_SECONDS
    ):
        session = load_session(ctx)
        turn_state = session.setdefault("turns", {}).setdefault(turn_id, {})
        turn_state.update(
            {
                "status": status,
                "error": str(error),
                "retryCount": int(turn_state.get("retryCount") or 0) + 1,
                "finishedAt": utc_now(),
            }
        )
        if not keep_active and session.get("activeTurnId") == turn_id:
            session["activeTurnId"] = None
        if transaction_path(ctx, turn_id).is_file():
            queue_turn_recovery(ctx, session, turn_id, "finalization-failed")
        release_turn_paths_locked(ctx, session, turn_id)
        save_session(ctx, session)


def retry_previous_turn(ctx: AuditContext, next_turn_id: str | None) -> dict[str, Any] | None:
    with project_lock(
        ctx.state_root, lock_name=".state.lock", wait_seconds=TOOL_LOCK_WAIT_SECONDS
    ):
        session = load_session(ctx)
        excluded = str(next_turn_id or "")
        discover_turn_transactions(ctx, session)
        candidates: dict[tuple[str, str], tuple[AuditContext, str]] = {}

        def add_candidate(candidate_ctx: AuditContext, turn_id: str) -> None:
            if (
                candidate_ctx.session_id == ctx.session_id
                and turn_id == excluded
            ):
                return
            candidates.setdefault(
                (candidate_ctx.session_id, turn_id), (candidate_ctx, turn_id)
            )

        for turn_id in pending_recovery_turn_ids(session):
            add_candidate(ctx, turn_id)
        active = str(session.get("activeTurnId") or "")
        active_status = str(
            session.get("turns", {}).get(active, {}).get("status") or "active"
        )
        if (
            active
            and active != excluded
            and active_status in {"active", "commit-deferred", "commit-failed"}
        ):
            add_candidate(ctx, active)
        for turn_id, turn_state in session.get("turns", {}).items():
            normalized = str(turn_id or "")
            if (
                normalized
                and normalized != excluded
                and str(turn_state.get("status") or "") != "committed"
                and (
                    transaction_path(ctx, normalized).is_file()
                    or recovery_marker_path(ctx, ctx.session_id, normalized).is_file()
                )
            ):
                add_candidate(ctx, normalized)
        for candidate_ctx, turn_id in global_recovery_markers(ctx):
            add_candidate(candidate_ctx, turn_id)
        save_session(ctx, session)
    if not candidates:
        return None
    results: list[dict[str, Any]] = []
    for candidate_ctx, previous in candidates.values():
        try:
            result = finalize_turn(
                candidate_ctx,
                {
                    "hook_event_name": "Stop",
                    "session_id": candidate_ctx.session_id,
                    "turn_id": previous,
                    "_historyLockWaitSeconds": TOOL_LOCK_WAIT_SECONDS,
                },
            )
        except ProjectLockTimeout as exc:
            result = {
                "status": "recovery-deferred-history-busy",
                "sessionId": candidate_ctx.session_id,
                "turnId": previous,
                "error": str(exc),
            }
        except (AuditError, bridge.CodexP4DiffError, OSError) as exc:
            release_failed_turn(candidate_ctx, previous, exc)
            result = {
                "status": "audit-failed-released",
                "sessionId": candidate_ctx.session_id,
                "turnId": previous,
                "error": str(exc),
            }
        results.append(result)
    if len(results) == 1:
        return results[0]
    return {"status": "recovery-batch", "results": results}


def record_prompt(ctx: AuditContext, event: dict[str, Any]) -> str:
    explicit = str(event.get("turn_id") or "").strip()
    turn_id = explicit or f"turn-{int(time.time() * 1000)}"
    try:
        recovery = retry_previous_turn(ctx, turn_id)
    except ProjectLockTimeout as exc:
        recovery = {
            "status": "recovery-deferred-state-busy",
            "error": str(exc),
        }
    if recovery is not None:
        event["_codexP4DiffRecovery"] = recovery
    with project_lock(
        ctx.state_root, lock_name=".state.lock", wait_seconds=PROJECT_LOCK_WAIT_SECONDS
    ):
        session = load_session(ctx)
        prompt = str(event.get("prompt") or event.get("user_prompt") or "")
        request_text = redact_summary(prompt, 12_001)
        record = {
            "turnId": turn_id,
            "summary": redact_summary(prompt),
            "requestText": request_text[:12_000],
            "requestCapture": "truncated-at-12000" if len(request_text) > 12_000 else "redacted-whitespace-normalized",
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
    analysis = analyze_tool_paths(ctx, event)
    event["_codexP4DiffWarnings"] = analysis.warnings
    paths = analysis.paths
    if not paths and not analysis.warnings:
        return 0
    try:
        with project_lock(
            ctx.state_root, lock_name=".state.lock", wait_seconds=TOOL_LOCK_WAIT_SECONDS
        ):
            session = load_session(ctx)
            turn_id = route_writer_turn(ctx, event, session)
            if analysis.mutating and not paths:
                write_uncertainty_markers(ctx, turn_id, (), global_scope=True)
            record_coverage_warnings(ctx, session, turn_id, event, analysis.warnings)
            if paths:
                claim = claim_paths(
                    ctx, session, turn_id, paths,
                    writer=event.get("_codexP4DiffWriter"),
                    tool_use_id=str(event.get("tool_use_id") or "") or None,
                )
                for path in paths:
                    relative = relative_path(ctx, path)
                    reserve_before_path_locked(
                        ctx, turn_id, path, claim.generations.get(relative),
                        writer=event.get("_codexP4DiffWriter"),
                    )
            save_session(ctx, session)
    except ProjectLockTimeout:
        session = load_session(ctx)
        turn_id = normalize_turn_id(ctx, event, session)
        warning = "state-lock-timeout-unowned"
        warnings = sorted(set([*analysis.warnings, warning]))
        event["_codexP4DiffWarnings"] = warnings
        write_uncertainty_markers(
            ctx,
            turn_id,
            paths,
            global_scope=analysis.mutating and not paths,
        )
        record_coverage_warnings(ctx, session, turn_id, event, warnings)
        manifest_path = before_manifest_path(ctx, turn_id)
        manifest = load_before_manifest(ctx, turn_id)
        known = manifest.setdefault("files", {})
        for path in paths:
            relative = relative_path(ctx, path)
            if manifest_file_key(known, relative) is None:
                known[relative] = {
                    "path": relative,
                    "exists": None,
                    "size": None,
                    "sha256": None,
                    "binary": None,
                    "captured": False,
                    "snapshotPath": None,
                    "ownershipGeneration": -1,
                    "recordedAt": utc_now(),
                    "recordedEpoch": time.time(),
                    "warning": "state lock unavailable; exact before content unavailable",
                }
        atomic_write_json(manifest_path, manifest)
        return len(paths)

    snapshot_failures: list[Path] = []
    for path in paths:
        relative = relative_path(ctx, path)
        try:
            capture_reserved_before_path(
                ctx, turn_id, path, claim.generations.get(relative)
            )
        except (ProjectLockTimeout, OSError):
            snapshot_failures.append(path)
    if snapshot_failures:
        warning = "before-snapshot-unavailable"
        warnings = sorted(set([*analysis.warnings, warning]))
        event["_codexP4DiffWarnings"] = warnings
        write_uncertainty_markers(ctx, turn_id, snapshot_failures)
        try:
            with project_lock(
                ctx.state_root,
                lock_name=".state.lock",
                wait_seconds=TOOL_LOCK_WAIT_SECONDS,
            ):
                session = load_session(ctx)
                record_coverage_warnings(ctx, session, turn_id, event, warnings)
                save_session(ctx, session)
        except ProjectLockTimeout:
            record_coverage_warnings(ctx, session, turn_id, event, warnings)
    return len(paths)


def finish_tool(ctx: AuditContext, event: dict[str, Any]) -> int:
    analysis = analyze_tool_paths(ctx, event)
    warnings = list(analysis.warnings)
    event["_codexP4DiffWarnings"] = warnings
    paths = analysis.paths
    if not paths and not analysis.warnings:
        return 0
    try:
        with project_lock(
            ctx.state_root, lock_name=".state.lock", wait_seconds=PROJECT_LOCK_WAIT_SECONDS
        ):
            session = load_session(ctx)
            turn_id = route_writer_turn(ctx, event, session)
            if not paths:
                record_coverage_warnings(ctx, session, turn_id, event, warnings)
                save_session(ctx, session)
                return 0
            manifest_path = before_manifest_path(ctx, turn_id)
            manifest = load_before_manifest(ctx, turn_id)
            known = manifest.setdefault("files", {})
            claim = claim_paths(
                ctx, session, turn_id, paths, allow_live_supersede=True,
                writer=event.get("_codexP4DiffWriter"),
                tool_use_id=str(event.get("tool_use_id") or "") or None,
            )
            if claim.superseded_paths:
                warnings.append("posttool-ownership-conflict-after-execution")
            discovered_after_execution = False
            discovered_paths: list[Path] = []
            for path in paths:
                relative = relative_path(ctx, path)
                if manifest_file_key(known, relative) is None:
                    discovered_after_execution = True
                    discovered_paths.append(path)
                    known[relative] = {
                        "path": relative,
                        "exists": None,
                        "size": None,
                        "sha256": None,
                        "binary": None,
                        "captured": False,
                        "snapshotPath": None,
                        "ownershipGeneration": claim.generations.get(relative),
                        "recordedAt": utc_now(),
                        "recordedEpoch": time.time(),
                        "warning": "path discovered after tool execution; exact before content unavailable",
                    }
                entry_key = manifest_file_key(known, relative)
                if entry_key is not None:
                    add_manifest_writer(known[entry_key], event.get("_codexP4DiffWriter"))
            if discovered_after_execution:
                warnings.append("posttool-before-snapshot-unavailable")
            uncertain_relatives = set(claim.superseded_paths)
            uncertain_paths = [
                path
                for path in [*discovered_paths, *paths]
                if relative_path(ctx, path) in uncertain_relatives or path in discovered_paths
            ]
            if uncertain_paths:
                write_uncertainty_markers(ctx, turn_id, uncertain_paths)
            warnings = sorted(set(warnings))
            event["_codexP4DiffWarnings"] = warnings
            record_coverage_warnings(ctx, session, turn_id, event, warnings)
            atomic_write_json(manifest_path, manifest)
            save_session(ctx, session)
    except ProjectLockTimeout:
        session = load_session(ctx)
        turn_id = normalize_turn_id(ctx, event, session)
        warnings = sorted(
            set(
                [
                    *warnings,
                    "posttool-before-snapshot-unavailable",
                    "state-lock-timeout-unowned",
                ]
            )
        )
        event["_codexP4DiffWarnings"] = warnings
        write_uncertainty_markers(
            ctx,
            turn_id,
            paths,
            global_scope=analysis.mutating and not paths,
        )
        record_coverage_warnings(ctx, session, turn_id, event, warnings)
        manifest_path = before_manifest_path(ctx, turn_id)
        manifest = load_before_manifest(ctx, turn_id)
        known = manifest.setdefault("files", {})
        for path in paths:
            relative = relative_path(ctx, path)
            if manifest_file_key(known, relative) is None:
                known[relative] = {
                    "path": relative,
                    "exists": None,
                    "size": None,
                    "sha256": None,
                    "binary": None,
                    "captured": False,
                    "snapshotPath": None,
                    "ownershipGeneration": -1,
                    "recordedAt": utc_now(),
                    "recordedEpoch": time.time(),
                    "warning": "state lock unavailable after execution; exact before content unavailable",
                }
        atomic_write_json(manifest_path, manifest)
    return len(paths)


def finish_committed_turn(
    ctx: AuditContext,
    session: dict[str, Any],
    turn_id: str,
    commit: str,
    audit_path: str,
    summary_record: dict[str, Any],
    *,
    file_count: int,
    request_summary: Any,
) -> dict[str, Any]:
    audit_coverage = str(summary_record.get("auditCoverage") or "exact")
    final_coverage_warnings = [
        str(item)
        for item in summary_record.get("coverageWarnings", [])
        if str(item)
    ]
    with project_lock(
        ctx.state_root, lock_name=".state.lock", wait_seconds=PROJECT_LOCK_WAIT_SECONDS
    ):
        latest = load_session(ctx)
        turn_state = latest.setdefault("turns", {}).setdefault(turn_id, {})
        already_recorded = (
            turn_state.get("status") == "committed" and turn_state.get("commit") == commit
        )
        turn_state.update(
            {
                "status": "committed",
                "commit": commit,
                "auditNotesRef": audit_path,
                "fileCount": file_count,
                "summary": summary_record["changeSummary"],
                "summarySource": summary_record["changeSummarySource"],
                "requestSummary": request_summary,
                "auditCoverage": audit_coverage,
                "coverageWarnings": final_coverage_warnings,
                "finishedAt": utc_now(),
            }
        )
        if not already_recorded:
            latest["nextSequence"] = max(
                int(latest.get("nextSequence") or 1),
                int(session.get("nextSequence") or 1) + 1,
            )
        if latest.get("activeTurnId") == turn_id:
            latest["activeTurnId"] = None
        clear_turn_recovery(ctx, latest, turn_id)
        release_turn_paths_locked(ctx, latest, turn_id)
        observed_warnings = [str(item) for item in session.get("stateWarnings", []) if str(item)]
        if observed_warnings:
            latest["stateWarnings"] = list(
                dict.fromkeys([*latest.get("stateWarnings", []), *observed_warnings])
            )
        save_session(ctx, latest)
    delete_recovery_marker(ctx, turn_id)
    return {
        "status": "committed",
        "commit": commit,
        "branch": ctx.branch,
        "files": file_count,
        "summary": summary_record["changeSummary"],
        "summarySource": summary_record["changeSummarySource"],
        "historyRoot": str(ctx.history_root),
        "auditCoverage": audit_coverage,
        "coverageWarnings": final_coverage_warnings,
    }


def finish_already_committed_turn(ctx: AuditContext, turn_id: str) -> dict[str, Any]:
    with project_lock(
        ctx.state_root, lock_name=".state.lock", wait_seconds=PROJECT_LOCK_WAIT_SECONDS
    ):
        latest = load_session(ctx)
        turn_state = latest.setdefault("turns", {}).setdefault(turn_id, {})
        clear_turn_recovery(ctx, latest, turn_id)
        release_turn_paths_locked(ctx, latest, turn_id)
        save_session(ctx, latest)
        result = {
            "status": "already-committed",
            "commit": turn_state.get("commit"),
            "branch": ctx.branch,
            "auditCoverage": turn_state.get("auditCoverage") or "exact",
            "coverageWarnings": list(turn_state.get("coverageWarnings") or []),
        }
    delete_recovery_marker(ctx, turn_id)
    return result


def finish_no_changes_turn(
    ctx: AuditContext,
    session: dict[str, Any],
    turn_id: str,
    audit_coverage: str,
    turn_coverage_warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    warning_codes = [
        str(item.get("code") or "")
        for item in turn_coverage_warnings
        if item.get("code")
    ]
    with project_lock(
        ctx.state_root, lock_name=".state.lock", wait_seconds=PROJECT_LOCK_WAIT_SECONDS
    ):
        latest = load_session(ctx)
        turn_state = latest.setdefault("turns", {}).setdefault(turn_id, {})
        turn_state.update(
            {
                "status": "no-changes",
                "auditCoverage": audit_coverage,
                "coverageWarnings": warning_codes,
                "finishedAt": utc_now(),
            }
        )
        if latest.get("activeTurnId") == turn_id:
            latest["activeTurnId"] = None
        clear_turn_recovery(ctx, latest, turn_id)
        release_turn_paths_locked(ctx, latest, turn_id)
        observed_warnings = [str(item) for item in session.get("stateWarnings", []) if str(item)]
        if observed_warnings:
            latest["stateWarnings"] = list(
                dict.fromkeys([*latest.get("stateWarnings", []), *observed_warnings])
            )
        save_session(ctx, latest)
    delete_recovery_marker(ctx, turn_id)
    return {
        "status": "no-changes",
        "branch": ctx.branch,
        "historyRoot": str(ctx.history_root),
        "auditCoverage": audit_coverage,
        "coverageWarnings": turn_coverage_warnings,
    }


def seal_child_admission(
    ctx: AuditContext, event: dict[str, Any]
) -> tuple[dict[str, Any], str, bool]:
    with project_lock(ctx.state_root, lock_name=".state.lock"):
        session = load_session(ctx)
        turn_id = normalize_turn_id(ctx, event, session)
        turn_state = session.setdefault("turns", {}).setdefault(turn_id, {})
        # New tools cannot enter this turn after its after snapshot begins.
        turn_state["closedForChildWrites"] = True
        waiting = any(turn_id in writer.get("activeTools", {}).values()
                      for writer in session.get("writerTurns", {}).values())
        if waiting:
            turn_state["status"] = "awaiting-child-tools"
            queue_turn_recovery(ctx, session, turn_id, "awaiting-child-tools")
        save_session(ctx, session)
        return session, turn_id, waiting


def _finalize_turn(ctx: AuditContext, event: dict[str, Any]) -> dict[str, Any]:
    requested_wait = event.get("_historyLockWaitSeconds")
    wait_seconds = (
        float(requested_wait)
        if isinstance(requested_wait, (int, float)) and not isinstance(requested_wait, bool)
        else None
    )
    with project_lock(ctx.state_root, wait_seconds=wait_seconds):
        session, turn_id, waiting = seal_child_admission(ctx, event)
        if waiting:
            return {"status": "awaiting-child-tools", "turnId": turn_id}
        turn_state = session["turns"][turn_id]
        if turn_state.get("status") == "committed":
            return finish_already_committed_turn(ctx, turn_id)
        transaction = load_turn_transaction(ctx, session, turn_id)
        if transaction is not None:
            commit, audit_path, summary_record = resume_turn_transaction(
                ctx, turn_id, transaction
            )
            audit_record = transaction.get("auditRecord", {})
            return finish_committed_turn(
                ctx,
                session,
                turn_id,
                commit,
                audit_path,
                summary_record,
                file_count=len(audit_record.get("files") or []),
                request_summary=audit_record.get("promptSummary"),
            )
        prompt = read_json_if_exists(
            prompt_record_path(ctx, turn_id),
            {"summary": "Codex file changes", "sha256": None},
        )
        ownership_generations = renew_turn_ownership(ctx, turn_id)
        manifest = load_before_manifest(ctx, turn_id)
        manifest_warning_codes = [
            str(item.get("code") or "")
            for item in manifest.get("stateWarnings", [])
            if isinstance(item, dict) and str(item.get("code") or "")
        ]
        if manifest_warning_codes:
            record_coverage_warnings(
                ctx, session, turn_id, event, manifest_warning_codes
            )
        deferred_after = load_deferred_after_record(ctx, turn_id)
        recovery_marker = read_json_if_exists(recovery_marker_path(ctx, ctx.session_id, turn_id), {})
        deferred_required = bool(
            deferred_after is not None
            or turn_state.get("status") == "commit-deferred"
            or (recovery_marker and recovery_marker.get("reason") != "awaiting-child-tools")
        )
        deferred_warning_codes = (
            [str(item) for item in deferred_after.get("warnings", []) if str(item)]
            if deferred_after is not None
            else []
        )
        if deferred_warning_codes:
            record_coverage_warnings(
                ctx, session, turn_id, event, deferred_warning_codes
            )
        ownership_current: dict[str, bool] = {}
        for relative, before in manifest.get("files", {}).items():
            generation = before.get("ownershipGeneration") if isinstance(before, dict) else None
            deferred_entry = (
                deferred_after.get("files", {}).get(relative)
                if deferred_after is not None
                else None
            )
            sealed_attribution = (
                deferred_entry.get("ownershipAttribution")
                if isinstance(deferred_entry, dict)
                else None
            )
            if deferred_required and sealed_attribution in {"exact", "superseded"}:
                ownership_current[relative] = sealed_attribution == "exact"
            else:
                ownership_current[relative] = generation is None or (
                    isinstance(generation, (str, int))
                    and not isinstance(generation, bool)
                    and ownership_generations.get(ownership_key(relative)) == generation
                )
        if any(not current for current in ownership_current.values()):
            record_coverage_warnings(
                ctx,
                session,
                turn_id,
                event,
                ["ownership-superseded-before-finalize"],
            )
        turn_coverage_warnings = coverage_warnings(ctx, turn_id)
        audit_coverage = "partial" if turn_coverage_warnings else "exact"
        changes: list[dict[str, Any]] = []
        patches: list[str] = []
        uncertainty_detected = False
        deferred_after_unavailable = False
        for relative, before in sorted(manifest.get("files", {}).items()):
            path = ctx.project_root / Path(relative)
            after, after_data, after_exact = deferred_or_live_after_snapshot(
                ctx,
                turn_id,
                relative,
                path,
                deferred_after,
                deferred_required=deferred_required,
            )
            deferred_after_unavailable = deferred_after_unavailable or not after_exact
            before_available = before.get("exists") is not None
            recorded_epoch = before.get("recordedEpoch")
            try:
                before_epoch = float(recorded_epoch or 0)
            except (TypeError, ValueError, OverflowError):
                before_epoch = 0
            uncertain = has_foreign_uncertainty_since(
                ctx, turn_id, relative, before_epoch
            )
            uncertainty_detected = uncertainty_detected or uncertain
            attribution_current = (
                ownership_current.get(relative, True) and not uncertain and after_exact
            )
            changed = (
                True
                if not after_exact or not before_available
                else snapshot_changed(before, after)
            )
            if not changed:
                continue
            before_data: bytes | None = None
            snapshot_path = before.get("snapshotPath")
            if before.get("captured") and before.get("exists") and snapshot_path:
                candidate = Path(str(snapshot_path))
                if candidate.is_file():
                    before_data = candidate.read_bytes()
            patch = ""
            if before_available and attribution_current:
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
                "action": action_for(before, after) if attribution_current else "unknown",
                "ownershipAttribution": (
                    "deferred-after-unavailable"
                    if not after_exact
                    else "uncertain"
                    if uncertain
                    else "exact"
                    if attribution_current and before_available
                    else "after-only"
                    if attribution_current
                    else "superseded"
                ),
                "before": {
                    key: before.get(key)
                    for key in (
                        "exists",
                        "size",
                        "sha256",
                        "binary",
                        "captured",
                        "ownershipGeneration",
                        "recordedEpoch",
                    )
                },
                "after": {
                    key: after.get(key)
                    for key in ("exists", "size", "sha256", "binary", "captured")
                },
                "p4": p4_record,
                "textPatch": bool(patch),
                "writers": before.get("writers", []),
                "_beforeData": before_data,
                "_afterData": after_data,
            }
            changes.append(change)

        if deferred_after_unavailable:
            record_coverage_warnings(
                ctx,
                session,
                turn_id,
                event,
                ["deferred-after-snapshot-unavailable"],
            )
            turn_coverage_warnings = coverage_warnings(ctx, turn_id)
            audit_coverage = "partial"

        if uncertainty_detected:
            record_coverage_warnings(
                ctx,
                session,
                turn_id,
                event,
                ["concurrent-write-uncertainty-before-finalize"],
            )
            turn_coverage_warnings = coverage_warnings(ctx, turn_id)
            audit_coverage = "partial"

        if not changes:
            return finish_no_changes_turn(
                ctx,
                session,
                turn_id,
                audit_coverage,
                turn_coverage_warnings,
            )

        commit, audit_path, summary_record = create_turn_commit(
            ctx, session, turn_id, prompt, changes, patches, event
        )
        return finish_committed_turn(
            ctx,
            session,
            turn_id,
            commit,
            audit_path,
            summary_record,
            file_count=len(changes),
            request_summary=prompt.get("summary"),
        )


def finalize_turn(ctx: AuditContext, event: dict[str, Any]) -> dict[str, Any]:
    try:
        return _finalize_turn(ctx, event)
    except ProjectLockTimeout as exc:
        turn_id = str(event.get("_codexP4DiffAuditTurnId") or event.get("turn_id") or "").strip()
        if not turn_id:
            turn_id = str(load_session(ctx).get("activeTurnId") or "").strip()
        if turn_id:
            try:
                _, turn_id, waiting = seal_child_admission(ctx, event)
            except ProjectLockTimeout:
                # Without the state lock, absence of running children is unproven.
                # Preserve claims and retry later; never freeze or seal an early after.
                if not recovery_marker_path(ctx, ctx.session_id, turn_id).exists():
                    queue_turn_recovery(ctx, {}, turn_id, "awaiting-child-tools")
                raise
            if waiting:
                return {"status": "awaiting-child-tools", "turnId": turn_id}
            deferred_warnings: list[str] = []
            if not transaction_path(ctx, turn_id).is_file():
                try:
                    deferred_warnings = capture_deferred_after(ctx, turn_id)
                except (AuditError, OSError):
                    deferred_warnings = ["deferred-after-snapshot-unavailable"]
            try:
                with project_lock(
                    ctx.state_root,
                    lock_name=".state.lock",
                    wait_seconds=PROJECT_LOCK_WAIT_SECONDS,
                ):
                    session = load_session(ctx)
                    turn_state = session.setdefault("turns", {}).setdefault(turn_id, {})
                    turn_state.update(
                        {
                            "status": "commit-deferred",
                            "error": str(exc),
                            "deferredAt": utc_now(),
                        }
                    )
                    if deferred_warnings:
                        record_coverage_warnings(
                            ctx, session, turn_id, event, deferred_warnings
                        )
                    queue_turn_recovery(ctx, session, turn_id, "history-lock-timeout")
                    if seal_deferred_after_ownership_locked(ctx, turn_id):
                        release_turn_paths_locked(ctx, session, turn_id)
                    save_session(ctx, session)
            except (AuditError, OSError):
                queue_turn_recovery(ctx, {}, turn_id, "history-lock-timeout-unlocked")
        raise
    except (AuditError, bridge.CodexP4DiffError, OSError) as exc:
        turn_id = str(event.get("_codexP4DiffAuditTurnId") or event.get("turn_id") or "").strip()
        if not turn_id:
            try:
                with project_lock(
                    ctx.state_root,
                    lock_name=".state.lock",
                    wait_seconds=TOOL_LOCK_WAIT_SECONDS,
                ):
                    turn_id = str(load_session(ctx).get("activeTurnId") or "")
            except (AuditError, OSError):
                turn_id = ""
        if turn_id:
            try:
                release_failed_turn(
                    ctx,
                    turn_id,
                    exc,
                    status="commit-failed",
                    keep_active=True,
                )
            except (AuditError, OSError):
                pass
        raise AuditError(
            f"本地 Git 回合提交失败；P4 工作区文件保持原样，将自动重试且不会保留文件锁：{exc}"
        ) from exc


def mark_session(ctx: AuditContext, status: str) -> None:
    with project_lock(
        ctx.state_root, lock_name=".state.lock", wait_seconds=PROJECT_LOCK_WAIT_SECONDS
    ):
        session = load_session(ctx)
        registry, migrated = read_ownership_registry(ctx)
        if migrated:
            atomic_write_json(ownership_path(ctx), registry)
        session["status"] = status
        if status == "ended":
            session["endedAt"] = utc_now()
            release_paths_locked(ctx, session)
        save_session(ctx, session)


def additional_context(event_name: str, text: str) -> dict[str, Any]:
    # Only context-bearing events accept this tagged union; Stop uses common output.
    if event_name in {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse"}:
        return {
            "hookSpecificOutput": {
                "hookEventName": event_name,
                "additionalContext": text,
            }
        }
    return {"systemMessage": text}


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
    if event_name in {"SubagentStart", "SubagentStop"}:
        result = record_subagent_event(ctx, event)
        return {"systemMessage": (
            f"子任务写入归属已记录：{result.get('parentTurnId') or result.get('commit') or result['status']}"
        )}
    if event_name == "SessionStart":
        mark_session(ctx, "active")
        return additional_context(
            event_name,
            f"Codex 本地历史已启用：{ctx.branch}。"
            "P4 只读约束仅适用于 codex-p4-diff 插件自身；"
            "不限制插件之外经用户授权的 P4 操作，其他独立权限与审批要求仍须遵守。"
            f"历史仓库：{ctx.history_root}。"
            "原生“上一轮”卡由 Codex 单独记录；默认由子任务分析和给补丁，"
            "最终文件写入由主会话执行。并行直接写入后应检查完整 Git 审阅，"
            "交付前打开覆盖所有 writer 的 Git 审阅，并说明原生卡限制。"
            "新提交不再从聊天首句猜标题。收尾前按插件 references/commit-descriptions.md "
            "读取最终差异并填写本回合专用变更报告；缺失或过期时只记录文件事实。"
        )
    if event_name == "UserPromptSubmit":
        turn_id = record_prompt(ctx, event)
        recovery = event.get("_codexP4DiffRecovery")
        suffix = ""
        if isinstance(recovery, dict):
            suffix = f"；上一回合恢复结果：{recovery.get('status')}"
        guide = Path(__file__).resolve().parent.parent / "references" / "commit-descriptions.md"
        command = (f'python -X utf8 -B "{Path(__file__).with_name("codex_p4_description.py")}" prepare '
                   f'--workspace "{ctx.project_root}" --session "{ctx.session_id}" --turn "{turn_id}"')
        return additional_context(event_name,
            f"本回合将在有文件变化时自动提交本地 Git：{turn_id}{suffix}。"
            f"最终写入和子任务结束后、最终回复前，阅读 {guide}，执行 {command}，"
            "读取输出的差异上下文并填写 reportPath（不改已有 sessionId/turnId/diffDigest）。"
            "标题写实际行为变化；原始需求另存；构建、自动测试、人工验证分别填写结果和证据。"
            "随后若继续改文件，重新 prepare 并更新报告。不要把普通最终回复当提交描述。")
    if event_name == "PreToolUse":
        count = prepare_tool(ctx, event)
        warnings = list(event.get("_codexP4DiffWarnings") or [])
        if warnings:
            return additional_context(
                event_name,
                f"已记录 {count} 个明确写入目标；审计覆盖不完整（{', '.join(warnings)}），"
                "命令不会因此被阻断。"
            )
        return additional_context(event_name, f"已为本回合记录 {count} 个文件的写入前状态") if count else None
    if event_name == "PostToolUse":
        finish_tool(ctx, event)
        warnings = list(event.get("_codexP4DiffWarnings") or [])
        return (
            post_feedback(
                f"codex-p4-diff 已保留部分覆盖警告：{', '.join(warnings)}；工具结果不受影响。"
            )
            if warnings
            else None
        )
    if event_name == "Stop":
        result = finalize_turn(ctx, event)
        if result["status"] == "awaiting-child-tools":
            return additional_context(
                event_name, "本回合仍有子任务写入尚未结束；已保留原始快照，待子任务结束后完成本地 Git 审计。"
            )
        if result["status"] == "committed":
            warning_codes = [
                str(item.get("code") if isinstance(item, dict) else item)
                for item in result.get("coverageWarnings", [])
            ]
            coverage_suffix = (
                f"；审计覆盖为 partial（{', '.join(warning_codes)}）"
                if result.get("auditCoverage") == "partial"
                else ""
            )
            return additional_context(
                event_name,
                f"本回合已提交本地 Git {result['commit'][:12]} 到 {result['branch']}，"
                f"共 {result['files']} 个文件；P4 未被修改{coverage_suffix}。"
            )
        if result["status"] == "already-committed":
            commit = str(result.get("commit") or "")
            warning_codes = [str(item) for item in result.get("coverageWarnings", [])]
            coverage_suffix = (
                f"；审计覆盖为 partial（{', '.join(warning_codes)}）"
                if result.get("auditCoverage") == "partial"
                else ""
            )
            return additional_context(
                event_name,
                f"本回合本地 Git 已提交{f'：{commit[:12]}' if commit else ''}；"
                f"未重复创建 commit{coverage_suffix}。"
            )
        if result.get("auditCoverage") == "partial":
            return additional_context(
                event_name,
                "本回合未识别到可提交的文件变化，但存在审计覆盖缺口；未创建空 Git commit。"
            )
        return additional_context(event_name, "本回合没有文件变化，因此未创建空 Git commit。")
    if event_name == "SessionEnd":
        end_subagent_tools(ctx)
        retry_previous_turn(ctx, None)
        mark_session(ctx, "ended")
        return None
    return None


def hook_main() -> int:
    event_name = ""
    raw = sys.stdin.buffer.read()
    try:
        event = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        if isinstance(event, dict):
            event_name = str(event.get("hook_event_name") or "")
        result = handle_event(event)
        if result is not None:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    except HookSkip:
        return 0
    except OwnershipConflict as exc:
        result = deny_pretool(str(exc)) if event_name == "PreToolUse" else additional_context(event_name, str(exc))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (AuditError, bridge.CodexP4DiffError, OSError, json.JSONDecodeError) as exc:
        if event_name == "PostToolUse":
            result = post_feedback(f"codex-p4-diff 审计警告：{exc}")
        else:
            result = additional_context(
                event_name,
                f"codex-p4-diff 审计警告（正常工具操作未被阻断）：{exc}"
            )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                additional_context(
                    event_name,
                    f"codex-p4-diff 审计警告（正常工具操作未被阻断）：{exc}"
                ),
                ensure_ascii=False,
            )
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(hook_main())
