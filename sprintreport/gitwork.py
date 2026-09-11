"""Collect the user's git commits/branches/PRs for the repos touched by the sessions."""
from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .util import find_tickets, parse_ts, uniq

PR_RE = re.compile(r"pull request #(\d+)|\(#(\d+)\)")
GIT_TIMEOUT = 60


@dataclass
class Commit:
    repo: str
    sha: str
    short: str
    authored_at: str
    author: str
    email: str
    subject: str
    refs: str
    is_merge: bool
    files_changed: int
    insertions: int
    deletions: int
    files: List[str]
    tickets: List[str]
    prs: List[str]


@dataclass
class RepoWork:
    root: str
    name: str
    author: str
    commits: List[Commit] = field(default_factory=list)
    branches: List[Dict[str, Any]] = field(default_factory=list)
    prs: List[str] = field(default_factory=list)
    done_targets: List[str] = field(default_factory=list)
    done_rule: str = ""
    error: str = ""

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)


def _git(args: List[str], cwd: Optional[str] = None) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                           timeout=GIT_TIMEOUT, check=False)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def find_repo(cwd: str) -> Optional[Tuple[str, str]]:
    """Return (worktree root, common git dir) for a path inside a repo, else None."""
    if not cwd or not os.path.isdir(cwd):
        return None
    rc, out, _ = _git(["rev-parse", "--show-toplevel", "--git-common-dir"], cwd=cwd)
    if rc != 0:
        return None
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    if len(lines) < 2:
        return None
    root, common = lines[0], lines[1]
    if not os.path.isabs(common):
        common = os.path.normpath(os.path.join(root, common))
    return root, os.path.realpath(common)


def resolve_repos(cwds: Iterable[str], extra: Iterable[str] = (), log=lambda *_: None) -> List[str]:
    """Dedupe by common git dir so worktrees of one repo count once; prefer the main worktree."""
    by_common: Dict[str, str] = {}
    for c in list(cwds) + list(extra):
        r = find_repo(c)
        if not r:
            continue
        root, common = r
        is_main = os.path.realpath(os.path.join(root, ".git")) == common
        if common not in by_common or is_main:
            by_common[common] = root
    roots = sorted(set(by_common.values()))
    for r in roots:
        log(f"  repo: {r}")
    return roots


def default_author(root: str) -> str:
    rc, out, _ = _git(["config", "user.email"], cwd=root)
    email = out.strip() if rc == 0 else ""
    if not email:
        rc, out, _ = _git(["config", "--global", "user.email"])
        email = out.strip() if rc == 0 else ""
    return email


def collect_commits(root: str, since: dt.datetime, until: dt.datetime, author: str,
                    max_files_per_commit: int = 25) -> List[Commit]:
    fmt = "%x1e%H%x1f%h%x1f%aI%x1f%an%x1f%ae%x1f%P%x1f%D%x1f%s"
    args = ["log", "--exclude=refs/stash", "--all", "--date-order",
            f"--since={since.isoformat()}", f"--until={until.isoformat()}",
            "--numstat", f"--format={fmt}"]
    if author:
        args.append(f"--author={re.escape(author)}")
    rc, out, err = _git(args, cwd=root)
    if rc != 0:
        raise RuntimeError(err.strip() or f"git log failed in {root}")
    name = os.path.basename(root.rstrip("/"))
    commits: List[Commit] = []
    for rec in out.split("\x1e"):
        rec = rec.strip("\n")
        if not rec.strip():
            continue
        lines = rec.split("\n")
        head = lines[0].split("\x1f")
        if len(head) < 8:
            continue
        sha, short, at, an, ae, parents, refs, subject = head[:8]
        files: List[str] = []
        ins = dele = nfiles = 0
        for l in lines[1:]:
            parts = l.split("\t")
            if len(parts) != 3:
                continue
            a, d, path = parts
            nfiles += 1
            ins += int(a) if a.isdigit() else 0
            dele += int(d) if d.isdigit() else 0
            if len(files) < max_files_per_commit:
                files.append(path)
        prs = uniq([g1 or g2 for g1, g2 in PR_RE.findall(subject)])
        # Show the human-readable ref names only (strip 'HEAD -> ' noise).
        refs_clean = ", ".join(r.strip().replace("HEAD -> ", "") for r in refs.split(",") if r.strip())
        commits.append(Commit(
            repo=name, sha=sha, short=short, authored_at=at, author=an, email=ae, subject=subject,
            refs=refs_clean, is_merge=len(parents.split()) > 1, files_changed=nfiles,
            insertions=ins, deletions=dele, files=files,
            tickets=find_tickets(subject, refs_clean), prs=[f"#{p}" for p in prs],
        ))
    # Dedupe identical commits reachable from several refs (git already does, but be safe).
    seen = set()
    unique: List[Commit] = []
    for c in commits:
        if c.sha in seen:
            continue
        seen.add(c.sha)
        unique.append(c)
    unique.sort(key=lambda c: c.authored_at)
    return unique


def merged_into(root: str, sha: str, own_name: str) -> List[str]:
    """Remote branches (other than the branch's own upstream) whose history contains `sha`.

    A non-empty list is hard evidence the work was merged: for a feature branch it names the
    integration branch(es); for an integration branch it may name release refs.
    """
    rc, out, _ = _git(["for-each-ref", "--format=%(refname:short)", "--contains", sha, "refs/remotes"], cwd=root)
    if rc != 0:
        return []
    res = []
    for line in out.splitlines():
        ref = line.strip()
        if not ref or ref.endswith("/HEAD"):
            continue
        remote_own = ref.split("/", 1)[1] if "/" in ref else ref
        if remote_own == own_name:
            continue  # origin/<same branch> is just the push target
        res.append(ref)
    return res


MAINLINE_RE = re.compile(r"(^|/)(main|master|develop|trunk|integration)$")
RELEASE_RE = re.compile(r"(^|/)(release[^/]*|rel-[^/]*)$")
PR_MERGE_RE = re.compile(r"pull request #\d+|^Merged in |^Merge pull request|\(#\d+\)$")


def _is_ancestor(root: str, rev: str, target: str) -> bool:
    """True when `rev` is reachable from `target`, i.e. rev's work is contained in target."""
    rc, _, _ = _git(["merge-base", "--is-ancestor", rev, target], cwd=root)
    return rc == 0


def _ref_exists(root: str, ref: str) -> bool:
    rc, _, _ = _git(["rev-parse", "--verify", "--quiet", ref + "^{commit}"], cwd=root)
    return rc == 0


def _remote_refs(root: str) -> List[str]:
    rc, out, _ = _git(["for-each-ref", "--format=%(refname:short)", "refs/remotes"], cwd=root)
    if rc != 0:
        return []
    return [l.strip() for l in out.splitlines() if l.strip() and not l.strip().endswith("/HEAD")]


def resolve_done_targets(root: str, integration: List[str]) -> Tuple[List[str], str]:
    """Return (refs that mean 'merged', human description of the rule).

    `integration` is what the user told us: the branches their team merges finished work into.
    Each name is matched locally and as its remote counterpart, since a local branch can lag a push.
    With nothing specified, fall back to the repo's mainline and release refs, which is stated
    plainly in the report rather than guessed at.
    """
    remotes = _remote_refs(root)
    if integration:
        targets: List[str] = []
        missing: List[str] = []
        for name in integration:
            found = [r for r in ([name] + [f"{rm}/{name}" for rm in ("origin", "upstream")])
                     if _ref_exists(root, r)]
            targets.extend(found)
            if not found:
                missing.append(name)
        desc = "merged into " + ", ".join(integration) if integration else ""
        if missing:
            desc += f" (not found in this repo: {', '.join(missing)})"
        return uniq(targets), desc
    mainline = [r for r in remotes if MAINLINE_RE.search(r)][:4]
    releases = [r for r in remotes if RELEASE_RE.search(r)][:6]
    targets = mainline + releases
    if not targets:
        return [], ("merged by a pull request (no integration branch was given and this repo has no "
                    "mainline branch; pass --integration-branch to be explicit)")
    parts = ", ".join(mainline[:2]) if mainline else ""
    if releases:
        parts += (" or " if parts else "") + f"a release branch ({len(releases)} found)"
    return uniq(targets), (f"merged into {parts} — no integration branch was given, so this is the "
                           "fallback; pass --integration-branch if your team merges elsewhere")


def pr_merge_parents(root: str, since: dt.datetime, until: dt.datetime) -> List[Dict[str, str]]:
    """Pull-request-style merge commits in the window, with the side they merged IN.

    `git log --merges --parents` prints "merge p1 p2 …": p1 is the branch the merge landed on and
    p2… are the merged-in tips. A branch whose tip is an ancestor of some p2, but not already of
    p1, was brought in by that merge.
    """
    rc, out, _ = _git(["log", "--all", "--merges", "--parents", "--date-order",
                       f"--since={since.isoformat()}", f"--until={until.isoformat()}",
                       "--format=%x1e%H %P%x1f%s"], cwd=root)
    if rc != 0:
        return []
    res: List[Dict[str, str]] = []
    for rec in out.split("\x1e"):
        if not rec.strip():
            continue
        shas, _, subject = rec.strip().partition("\x1f")
        parts = shas.split()
        if len(parts) < 3 or not PR_MERGE_RE.search(subject.strip()):
            continue
        for side in parts[2:]:
            res.append({"merge": parts[0], "onto": parts[1], "side": side, "subject": subject.strip()})
    return res


def collect_branches(root: str, since: dt.datetime, until: dt.datetime,
                     done_targets: List[str]) -> List[Dict[str, Any]]:
    """Local branches with commits in the window, each annotated with hard merge evidence.

    A branch counts as merged when its tip is contained in one of `done_targets`, or when a
    pull-request merge in the window pulled it in. Reachability from a sibling feature branch
    never counts on its own.
    """
    fmt = "%(committerdate:iso-strict)%1f%(refname:short)%1f%(objectname:short)%1f%(upstream:short)"
    rc, out, _ = _git(["for-each-ref", "--sort=-committerdate", f"--format={fmt}", "refs/heads"], cwd=root)
    if rc != 0:
        return []
    res: List[Dict[str, Any]] = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) < 3:
            continue
        t = parse_ts(parts[0])
        if not t or not (since <= t < until):
            continue
        res.append({"name": parts[1], "last_commit_at": parts[0], "sha": parts[2],
                    "upstream": parts[3] if len(parts) > 3 else "", "tickets": find_tickets(parts[1])})
    if not res:
        return res

    target_names = {t.split("/", 1)[1] if t.startswith(("origin/", "upstream/")) else t for t in done_targets}
    pr_sides: Dict[str, Dict[str, str]] = {}
    for m in pr_merge_parents(root, since, until):
        pr_sides.setdefault(m["side"], m)

    for b in res:
        sha, name = b["sha"], b["name"]
        b["is_integration"] = name in target_names
        targets = [] if b["is_integration"] else done_targets
        b["merged_into"] = [t for t in targets if _is_ancestor(root, sha, t)]
        b["merged"] = bool(b["merged_into"])
        # Name the merge that actually brought this branch in: its tip must be on the merged-in
        # side and not already on the branch the merge landed on, otherwise every later pull
        # request in the repo would claim credit for it.
        labels: List[str] = []
        for side, m in pr_sides.items():
            if len(labels) >= 4:
                break
            if _is_ancestor(root, sha, side) and not _is_ancestor(root, sha, m["onto"]):
                labels.append(m["subject"])
        labels.sort(key=lambda subj: 0 if name in subj or any(t in subj for t in b["tickets"]) else 1)
        b["merged_by_pr"] = uniq(labels)[:2]
        if b["merged_by_pr"] and not b["is_integration"]:
            b["merged"] = True
    return res


def collect_git_work(roots: List[str], since: dt.datetime, until: dt.datetime,
                     author_override: str = "", integration: Optional[List[str]] = None,
                     log=lambda *_: None) -> List[RepoWork]:
    out: List[RepoWork] = []
    for root in roots:
        author = author_override or default_author(root)
        rw = RepoWork(root=root, name=os.path.basename(root.rstrip("/")), author=author)
        try:
            rw.done_targets, rw.done_rule = resolve_done_targets(root, integration or [])
            log(f"  {rw.name}: \"done\" = {rw.done_rule}")
            rw.commits = collect_commits(root, since, until, author)
            rw.branches = collect_branches(root, since, until, rw.done_targets)
            rw.prs = uniq(p for c in rw.commits for p in c.prs)
        except Exception as e:
            rw.error = str(e)
            log(f"  ! git collection failed for {root}: {e}")
        log(f"  {rw.name}: {len(rw.commits)} commits, {len(rw.branches)} active branches, "
            f"{len(rw.prs)} PRs (author={author or 'any'})")
        out.append(rw)
    return out


def render_git_text(repos: List[RepoWork], tz: dt.tzinfo) -> str:
    lines: List[str] = []
    for rw in repos:
        lines.append(f"REPO {rw.name}  ({rw.root})  author filter: {rw.author or 'none'}")
        lines.append(f"  DEFINITION OF DONE for this repo: work is done when it is {rw.done_rule}.")
        if rw.error:
            lines.append(f"  ! error: {rw.error}")
        if rw.branches:
            lines.append("  branches with commits in window (merge state below is computed from git and is authoritative):")
            for b in rw.branches[:40]:
                t = parse_ts(b["last_commit_at"])
                bits: List[str] = []
                if b.get("merged_by_pr"):
                    bits.append("merged by " + "; ".join(b["merged_by_pr"]))
                if b.get("merged_into"):
                    bits.append("contained in " + ", ".join(b["merged_into"][:3]))
                if b.get("is_integration"):
                    state = "INTEGRATION BRANCH (finished work lands here)"
                elif b.get("merged"):
                    state = "MERGED: " + "; ".join(bits)
                else:
                    state = "NOT MERGED (no pull-request merge pulled it in, and it is not contained in " +                             (", ".join(rw.done_targets[:3]) if rw.done_targets else "any integration branch") + ")"
                lines.append(f"    {t.astimezone(tz).strftime('%Y-%m-%d') if t else '?'}  {b['name']}  -> {state}")
        if rw.prs:
            lines.append(f"  pull requests referenced in merge commits: {', '.join(rw.prs)}")
        lines.append(f"  commits ({len(rw.commits)}):")
        for c in rw.commits:
            t = parse_ts(c.authored_at)
            day = t.astimezone(tz).strftime("%Y-%m-%d") if t else "?"
            kind = "merge " if c.is_merge else ""
            stat = f"+{c.insertions}/-{c.deletions} in {c.files_changed} files" if c.files_changed else ""
            refs = f" [{c.refs}]" if c.refs else ""
            lines.append(f"    {day} {c.short} {kind}{c.subject}{refs} {stat}".rstrip())
            if c.files and not c.is_merge:
                lines.append("        files: " + ", ".join(c.files[:8]) + (" ..." if len(c.files) > 8 else ""))
        lines.append("")
    return "\n".join(lines)
