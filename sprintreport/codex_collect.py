"""Read local Codex rollout JSONL into the report's source-neutral digest shape.

Only conversation messages and selected tool-call arguments are retained. Reasoning,
developer instructions, tool results, command output, and world state are never sent
to the summarizer.
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import re
from collections import Counter
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .collect import (ACTIVE_GAP_MIN, ASST_HEAD, USER_HEAD, USER_TAIL,
                      SessionDigest, SubagentDigest, Turn, classify_command)
from .util import find_tickets, parse_ts, redact, short_project, truncate, uniq


def _records(path: str) -> Iterator[Dict[str, Any]]:
    with open(path, encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:  # an active rollout may end mid-line
                continue
            if isinstance(item, dict):
                yield item


def _text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    return "\n".join(str(part.get("text") or "") for part in content
                     if isinstance(part, dict) and part.get("type") in ("input_text", "output_text"))


def _human_text(text: str) -> str:
    # Desktop context and plugin inventories can be attached to a human turn.
    for tag in ("environment_context", "recommended_plugins"):
        text = re.sub(rf"<{tag}>.*?</{tag}>", "", text, flags=re.S)
    return text.strip()


def _arguments(item: Dict[str, Any]) -> Dict[str, Any]:
    raw = item.get("arguments") if item.get("type") == "function_call" else item.get("input")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                return decoded
        except json.JSONDecodeError:
            pass
    return {}


def _wrapped_values(source: str, key: str) -> List[str]:
    """Decode string arguments in a Codex `functions.exec` JavaScript wrapper."""
    values: List[str] = []
    decoder = json.JSONDecoder()
    for match in re.finditer(rf'(?<![\w])(?:"{re.escape(key)}"|{re.escape(key)})\s*:', source):
        try:
            value, _ = decoder.raw_decode(source[match.end():].lstrip())
        except json.JSONDecodeError:
            continue
        if isinstance(value, str):
            values.append(value)
    return values


def _session_id(path: str, meta: Dict[str, Any]) -> str:
    return str(meta.get("id") or meta.get("session_id") or os.path.splitext(os.path.basename(path))[0])


def parse_codex_session(path: str, since: dt.datetime, until: dt.datetime,
                        tz: dt.tzinfo) -> Tuple[Optional[SessionDigest], str]:
    meta: Dict[str, Any] = {}
    cwd = ""
    model = ""
    first = last = started = ended = previous = None
    turns: List[Turn] = []
    tools: Counter = Counter()
    touched: Counter = Counter()
    git_cmds: List[str] = []
    build_cmds: List[str] = []
    agents: List[str] = []
    skills: List[str] = []
    tickets_text: List[str] = []
    day_counts: Counter = Counter()
    n_user = n_asst = n_tools = files_read = 0
    active = 0.0
    for record in _records(path):
        kind = record.get("type")
        payload = record.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if kind == "session_meta":
            meta = payload
            cwd = str(payload.get("cwd") or cwd)
        elif kind == "turn_context":
            cwd = cwd or str(payload.get("cwd") or "")
            model = model or str(payload.get("model") or "")
        if kind != "response_item":
            continue
        typ = payload.get("type")
        if typ not in ("message", "function_call", "custom_tool_call"):
            continue
        ts = parse_ts(record.get("timestamp"))
        if ts:
            first = first or ts
            last = ts
        if not ts or not since <= ts < until:
            continue
        if typ == "message":
            role = payload.get("role")
            if role not in ("user", "assistant"):
                continue
            body = _text(payload.get("content"))
            if role == "user":
                body = _human_text(body)
            if not body:
                continue
            if previous:
                gap = (ts - previous).total_seconds() / 60
                if 0 <= gap <= ACTIVE_GAP_MIN:
                    active += gap
            previous = ts
            started = started or ts
            ended = ts
            if role == "user":
                n_user += 1
                day_counts[ts.astimezone(tz).strftime("%Y-%m-%d")] += 1
                tickets_text.append(body[:4000])
                turns.append(Turn(ts.isoformat(), "U", redact(truncate(body, USER_HEAD, USER_TAIL))))
            else:
                n_asst += 1
                turns.append(Turn(ts.isoformat(), "A", redact(truncate(body, ASST_HEAD))))
            continue
        n_tools += 1
        started = started or ts
        ended = ts
        name = str(payload.get("name") or "?")
        tools[name] += 1
        args = _arguments(payload)
        if name in ("exec_command", "shell_command", "Bash", "exec"):
            raw = str(payload.get("input") or "") if name == "exec" else ""
            commands = _wrapped_values(raw, "cmd") if raw else []
            commands += [str(args[k]) for k in ("cmd", "command") if args.get(k)]
            for command in commands:
                gs, bs = classify_command(command)
                git_cmds.extend(redact(truncate(x, 200)) for x in gs)
                build_cmds.extend(redact(truncate(x, 160)) for x in bs)
            patches = _wrapped_values(raw, "patch")
            for match in re.finditer(r"tools\.apply_patch\(\s*", raw):
                try:
                    patch, _ = json.JSONDecoder().raw_decode(raw[match.end():])
                    if isinstance(patch, str):
                        patches.append(patch)
                except json.JSONDecodeError:
                    pass
            for patch in patches:
                for file_path in re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", patch, re.M):
                    touched[file_path] += 1
        elif name in ("apply_patch", "write_file", "edit_file"):
            patch = str(args.get("patch") or args.get("input") or "")
            paths = re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", patch, re.M)
            paths += [str(args[k]) for k in ("path", "file_path") if args.get(k)]
            for file_path in paths:
                touched[file_path] += 1
        elif name in ("read_file", "view_image"):
            files_read += 1
        elif name in ("spawn_agent", "create_thread"):
            agents.append(redact(truncate(str(args.get("message") or args.get("prompt") or ""), 120)))
        elif name == "Skill" and args.get("skill"):
            skills.append(str(args["skill"]))
    if n_user == 0:
        return None, str(meta.get("parent_thread_id") or "")
    sid = _session_id(path, meta)
    title = next((re.sub(r"\s+", " ", t.text)[:80] for t in turns if t.role == "U"), sid)
    source = meta.get("source")
    entrypoint = "codex-" + (str(source) if isinstance(source, str) else "desktop")
    branch = ((meta.get("git") or {}).get("branch") if isinstance(meta.get("git"), dict) else None)
    stat = os.stat(path)
    digest = SessionDigest(
        session_id="codex:" + sid, path=path, project_dir="", cwd=cwd, project=short_project(cwd),
        title=title, entrypoint=entrypoint, version=str(meta.get("cli_version") or ""),
        git_branches=[str(branch)] if branch else [], models={model: n_asst} if model else {},
        started_at=started.isoformat() if started else None,
        ended_at=ended.isoformat() if ended else None,
        first_seen=first.isoformat() if first else None,
        last_seen=last.isoformat() if last else None,
        n_user_prompts=n_user, n_assistant_msgs=n_asst, n_tool_uses=n_tools,
        active_minutes=round(active, 1), tool_counts=dict(tools.most_common()),
        files_touched=dict(touched.most_common()), files_read=files_read,
        git_commands=uniq(git_cmds)[:40], build_test_commands=uniq(build_cmds)[:25],
        skills_used=uniq(skills), artifacts=[], agents_spawned=uniq(agents)[:20],
        questions_asked=0, plan_mode=False, tool_errors=[],
        tickets=find_tickets(title, str(branch or ""), " ".join(tickets_text), " ".join(git_cmds)),
        subagents=[], turns=turns,
        fingerprint=f"{stat.st_size}:{int(stat.st_mtime)}:{n_user}:{n_asst}",
        day_prompt_counts=dict(sorted(day_counts.items())),
    )
    return digest, str(meta.get("parent_thread_id") or "")


def collect_codex_sessions(codex_dir: str, since: dt.datetime, until: dt.datetime,
                           tz: dt.tzinfo, include: Optional[List[str]] = None,
                           exclude: Optional[List[str]] = None, min_prompts: int = 1,
                           include_subagents: bool = True, log=lambda *_: None) -> List[SessionDigest]:
    paths = glob.glob(os.path.join(codex_dir, "sessions", "**", "*.jsonl"), recursive=True)
    paths += glob.glob(os.path.join(codex_dir, "archived_sessions", "*.jsonl"))
    roots: List[SessionDigest] = []
    children: List[Tuple[SessionDigest, str]] = []
    scanned = 0
    for path in sorted(set(paths)):
        try:
            if os.stat(path).st_mtime < since.timestamp():
                continue
            scanned += 1
            digest, parent = parse_codex_session(path, since, until, tz)
        except (OSError, ValueError) as exc:
            log(f"  ! failed to parse {path}: {exc}")
            continue
        if digest is None:
            continue
        if parent:
            children.append((digest, parent))
            continue
        if digest.n_user_prompts < min_prompts:
            continue
        hay = (digest.cwd + " " + digest.project).lower()
        if include and not any(s.lower() in hay for s in include):
            continue
        if exclude and any(s.lower() in hay for s in exclude):
            continue
        roots.append(digest)
    if include_subagents:
        by_id = {root.session_id: root for root in roots}
        for child, parent in children:
            root = by_id.get("codex:" + parent)
            if root:
                task = next((t.text for t in child.turns if t.role == "U"), "")
                result = next((t.text for t in reversed(child.turns) if t.role == "A"), "")
                root.subagents.append(SubagentDigest(child.session_id, "codex", child.title,
                                                    redact(truncate(task, 800)),
                                                    redact(truncate(result, 800))))
    roots.sort(key=lambda d: d.started_at or "")
    log(f"  scanned {scanned} Codex rollout files, kept {len(roots)} root sessions")
    return roots
