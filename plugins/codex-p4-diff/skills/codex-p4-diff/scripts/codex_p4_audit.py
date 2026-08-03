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
    base_commit: str


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
        bridge.detach_legacy_git_pointer(project_root)
        history = bridge.ensure_history_repo(p4_context, project_root)
    session_key = bridge.sanitize_component(session_id, 80)
    return AuditContext(
        p4=p4_context,
        project_root=project_root,
        session_id=session_id,
        branch=f"{bridge.SESSION_BRANCH_PREFIX}{session_key}",
        history_root=Path(history["historyRoot"]),
        state_root=Path(history["stateRoot"]),
        session_root=Path(history["stateRoot"]) / "sessions" / session_key,
        git_exe=str(history["gitExe"]),
        base_commit=str(history["baseCommit"]),
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


def likely_mutating_shell(command: str) -> bool:
    lowered = command.casefold()
    read_only = (
        "get-content",
        "select-string",
        "rg ",
        "git status",
        "git diff",
        "p4 info",
        "p4 where",
        "p4 fstat",
        "p4 diff",
        "p4 opened",
    )
    if any(item in lowered for item in read_only) and not re.search(r"(?<![12])>(?!&)", command):
        return False
    mutating = (
        "apply_patch",
        "set-content",
        "add-content",
        "out-file",
        "new-item",
        "remove-item",
        "move-item",
        "copy-item",
        "write_text",
        "write_bytes",
        "clang-format",
        "prettier",
        "sed -i",
        "tee ",
    )
    return any(item in lowered for item in mutating) or bool(re.search(r"(?<![12])>(?!&)", command))


def extract_shell_paths(ctx: AuditContext, command: str, cwd: Path) -> list[Path]:
    values: list[str] = []
    values.extend(match.group(1) for match in re.finditer(r"['\"]([^'\"]+)['\"]", command))
    values.extend(
        match.group(0)
        for match in re.finditer(
            r"(?:[A-Za-z]:[\\/][^\s'\"|;]+|(?:\.{0,2}[\\/])?[A-Za-z0-9_.-]+(?:[\\/][A-Za-z0-9_. @+()-]+)+)",
            command,
        )
    )
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


def claim_paths(ctx: AuditContext, session: dict[str, Any], paths: list[Path]) -> None:
    registry = read_json_if_exists(ownership_path(ctx), {"files": {}})
    records = registry.setdefault("files", {})
    now_epoch = time.time()
    owned = set(str(item) for item in session.get("ownedFiles", []))
    for path in paths:
        relative = relative_path(ctx, path)
        current = records.get(relative)
        if isinstance(current, dict) and current.get("sessionId") != ctx.session_id:
            age = now_epoch - float(current.get("updatedEpoch") or 0)
            if age < LOCK_STALE_SECONDS:
                raise OwnershipConflict(
                    f"文件正由另一个 Codex 会话记录：{relative} "
                    f"(session {current.get('sessionId')})"
                )
        records[relative] = {
            "sessionId": ctx.session_id,
            "branch": ctx.branch,
            "updatedAt": utc_now(),
            "updatedEpoch": now_epoch,
        }
        owned.add(relative)
    session["ownedFiles"] = sorted(owned)
    registry["updatedAt"] = utc_now()
    atomic_write_json(ownership_path(ctx), registry)


def release_paths(ctx: AuditContext) -> None:
    registry = read_json_if_exists(ownership_path(ctx), {"files": {}})
    records = registry.setdefault("files", {})
    for relative in list(records):
        current = records.get(relative)
        if isinstance(current, dict) and current.get("sessionId") == ctx.session_id:
            del records[relative]
    registry["updatedAt"] = utc_now()
    atomic_write_json(ownership_path(ctx), registry)


def ensure_session_branch(ctx: AuditContext) -> str:
    ref = f"refs/heads/{ctx.branch}"
    completed = bridge.run_command(
        [ctx.git_exe, "-C", str(ctx.history_root), "rev-parse", "--verify", ref], check=False
    )
    if completed.returncode == 0:
        return completed.stdout.decode("ascii", errors="replace").strip()
    bridge.run_command(
        [ctx.git_exe, "-C", str(ctx.history_root), "update-ref", ref, ctx.base_commit]
    )
    return ctx.base_commit


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
) -> str:
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
        tree = bridge.run_command(
            [ctx.git_exe, "-C", str(ctx.history_root), "write-tree"], env=env
        ).stdout.decode("ascii").strip()
        commit = bridge.run_command(
            [ctx.git_exe, "-C", str(ctx.history_root), "commit-tree", tree, "-p", parent],
            input_bytes=message.encode("utf-8"),
            env=env,
        ).stdout.decode("ascii").strip()
        ref = f"refs/heads/{ctx.branch}"
        bridge.run_command(
            [ctx.git_exe, "-C", str(ctx.history_root), "update-ref", ref, commit, parent]
        )
        return commit
    finally:
        try:
            os.unlink(index_name)
        except FileNotFoundError:
            pass


def create_turn_commit(
    ctx: AuditContext,
    session: dict[str, Any],
    turn_id: str,
    prompt: dict[str, Any],
    changes: list[dict[str, Any]],
    patches: list[str],
) -> tuple[str, str]:
    parent = ensure_session_branch(ctx)
    sequence = int(session.get("nextSequence") or 1)
    turn_key = bridge.sanitize_component(turn_id, 64)
    commit_root = f"turns/{sequence:04d}-{turn_key}"
    updates: dict[str, bytes | None] = {}

    for change in changes:
        relative = str(change["path"])
        mirror_path = f"files/{relative}"
        before = change["before"]
        after = change["after"]
        before_data = change.pop("_beforeData", None)
        after_data = change.pop("_afterData", None)
        parent_data = git_blob_at(ctx, parent, mirror_path)
        mirror_state = "receipt-only"
        if after.get("captured") and after_data is not None:
            if parent_data is None:
                updates[mirror_path] = after_data
                mirror_state = "first-touch"
            elif before.get("captured") and before_data is not None and parent_data == before_data:
                updates[mirror_path] = after_data
                mirror_state = "exact"
            else:
                mirror_state = "drifted-receipt-only"
        elif not after.get("exists"):
            if parent_data is not None and before.get("captured") and parent_data == before_data:
                updates[mirror_path] = None
                mirror_state = "exact-delete"
            else:
                mirror_state = "delete-receipt-only"
        change["mirrorState"] = mirror_state

    event_record = {
        "schemaVersion": bridge.SCHEMA_VERSION,
        "generatedBy": bridge.TOOL_NAME,
        "sessionId": ctx.session_id,
        "turnId": turn_id,
        "sequence": sequence,
        "branch": ctx.branch,
        "projectRoot": str(ctx.project_root),
        "promptSummary": prompt.get("summary"),
        "promptSha256": prompt.get("sha256"),
        "recordedAt": utc_now(),
        "p4Mode": "read-only",
        "files": changes,
    }
    updates[f"{commit_root}/event.json"] = (
        json.dumps(event_record, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    if patches:
        updates[f"{commit_root}/changes.patch"] = "\n".join(patches).encode("utf-8")

    summary = str(prompt.get("summary") or "Codex file changes")
    subject = f"[Codex] {summary}"[:120]
    body = [
        subject,
        "",
        f"Session: {ctx.session_id}",
        f"Turn: {turn_id}",
        f"Project: {ctx.project_root}",
        "P4 mode: read-only",
        "Files:",
    ]
    body.extend(f"- {item['action']} {item['path']}" for item in changes)
    body.extend(["", f"Audit: {commit_root}/event.json", ""])
    commit = commit_tree(ctx, parent, updates, "\n".join(body))
    return commit, f"{commit_root}/event.json"


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
        claim_paths(ctx, session, paths)
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
        claim_paths(ctx, session, paths)
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
            save_session(ctx, session)
            return {"status": "no-changes", "branch": ctx.branch, "historyRoot": str(ctx.history_root)}

        try:
            commit, audit_path = create_turn_commit(
                ctx, session, turn_id, prompt, changes, patches
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
                "auditPath": audit_path,
                "fileCount": len(changes),
                "summary": prompt.get("summary"),
                "finishedAt": utc_now(),
            }
        )
        session["nextSequence"] = int(session.get("nextSequence") or 1) + 1
        session["activeTurnId"] = None
        save_session(ctx, session)
        return {
            "status": "committed",
            "commit": commit,
            "branch": ctx.branch,
            "files": len(changes),
            "historyRoot": str(ctx.history_root),
        }


def mark_session(ctx: AuditContext, status: str) -> None:
    with project_lock(ctx.state_root):
        session = load_session(ctx)
        session["status"] = status
        if status == "ended":
            session["endedAt"] = utc_now()
            release_paths(ctx)
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
