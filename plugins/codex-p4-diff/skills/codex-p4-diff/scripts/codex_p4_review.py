"""Build exact, detached Git review pairs from trusted turn snapshots."""

from __future__ import annotations

import os
import re
from typing import Any, Callable

import codex_p4_diff as bridge


def _ref_component(value: str) -> str:
    # Preserve normal IDs, but avoid invalid refs and sanitization collisions.
    if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        return value
    label = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")[:60] or "id"
    return f"{label}-{bridge.sha256_text(value)[:16]}"


def create_review_refs(
    ctx: Any,
    turn_id: str,
    candidate: str,
    before_overrides: dict[str, bytes | None],
    *,
    commit_tree: Callable[..., tuple[str, dict[str, str | None]]],
) -> dict[str, str]:
    """Return immutable before/after refs without changing HEAD or the main index.

    The caller must supply only trustworthy before snapshots: bytes mean that
    the path existed; None means it was absent. Unknown/receipt-only paths must
    be omitted. All omitted paths retain the candidate tree's contents.

    The after commit has the candidate's exact tree and the before commit as
    its parent, so both direct diffs and merge-base comparisons are exact.
    These refs do not update Codex's native Last turn diff.
    """
    for path, data in before_overrides.items():
        if (
            not path
            or "\\" in path
            or "\0" in path
            or re.match(r"^[A-Za-z]:", path)
            or any(part in ("", ".", "..") for part in path.split("/"))
        ):
            raise ValueError(f"Review path must be repository-relative: {path!r}")
        if data is not None and not isinstance(data, bytes):
            raise TypeError(f"Review before snapshot must be bytes or None: {path!r}")

    def git(*args: str, **kwargs: Any) -> Any:
        return bridge.run_command(
            [ctx.git_exe, "-C", str(ctx.history_root), *args], **kwargs
        )

    def output(*args: str) -> str:
        return git(*args).stdout.decode("ascii").strip()

    candidate = output("rev-parse", "--verify", "--end-of-options", f"{candidate}^{{commit}}")
    prefix = (
        "refs/codex-p4-diff/review/"
        f"{_ref_component(ctx.session_id)}/{_ref_component(turn_id)}"
    )
    before_ref, after_ref = f"{prefix}/before", f"{prefix}/after"
    candidate_tree = output("rev-parse", f"{candidate}^{{tree}}")
    parents = output("rev-list", "--parents", "-n", "1", candidate).split()[1:]
    timestamp = output("show", "-s", "--format=%ct", candidate)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Codex P4 Diff",
        "GIT_AUTHOR_EMAIL": "codex-p4-diff@local.invalid",
        "GIT_COMMITTER_NAME": "Codex P4 Diff",
        "GIT_COMMITTER_EMAIL": "codex-p4-diff@local.invalid",
        "GIT_AUTHOR_DATE": f"@{timestamp} +0000",
        "GIT_COMMITTER_DATE": f"@{timestamp} +0000",
    }
    # Reuse the audit writer's isolated index. The temporary commit is unreferenced.
    snapshot, _ = commit_tree(
        ctx, candidate, before_overrides, f"Prepare review baseline for {prefix}\n"
    )
    before_tree = output("rev-parse", f"{snapshot}^{{tree}}")

    def write_commit(tree: str, commit_parents: list[str], kind: str) -> str:
        args = ["commit-tree", tree]
        for parent in commit_parents:
            args.extend(["-p", parent])
        return git(
            *args,
            env=env,
            input_bytes=f"Codex review {kind}: {prefix}\n\nCandidate: {candidate}\n".encode("utf-8"),
        ).stdout.decode("ascii").strip()

    before_commit = write_commit(before_tree, parents, "before")
    after_commit = write_commit(candidate_tree, [before_commit], "after")

    def read_ref(ref: str) -> str | None:
        completed = git("show-ref", "--verify", "--hash", ref, check=False)
        return completed.stdout.decode("ascii").strip() if completed.returncode == 0 else None

    expected = (before_commit, after_commit)
    current = (read_ref(before_ref), read_ref(after_ref))
    if current != expected:
        if current != (None, None):
            raise bridge.CodexP4DiffError(f"Review refs already exist with different contents: {prefix}")
        transaction = (
            f"start\ncreate {before_ref} {before_commit}\n"
            f"create {after_ref} {after_commit}\nprepare\ncommit\n"
        )
        completed = git("update-ref", "--stdin", input_bytes=transaction.encode("ascii"), check=False)
        # A concurrent identical call may have created the same immutable pair.
        if completed.returncode and (read_ref(before_ref), read_ref(after_ref)) != expected:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise bridge.CodexP4DiffError(f"Cannot create review refs {prefix}: {detail}")
    return {
        "beforeRef": before_ref,
        "afterRef": after_ref,
        "beforeCommit": before_commit,
        "afterCommit": after_commit,
    }
