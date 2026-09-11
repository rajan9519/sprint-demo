"""System prompts and JSON schemas for the two LLM stages.

Stage 1 (map):    one call per session  -> SESSION_SCHEMA
Stage 2 (reduce): one call per sprint   -> SPRINT_SCHEMA

Bump PROMPT_VERSION whenever you change a prompt or schema so cached stage-1 results are redone.
"""
from __future__ import annotations

from typing import Any, Dict, Tuple

PROMPT_VERSION = "5"

_STR = {"type": "string"}
_STR_LIST = {"type": "array", "items": {"type": "string"}}

SESSION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": _STR,
        "objective": _STR,
        "outcome": _STR,
        "status": {"type": "string", "enum": ["done", "in_progress", "blocked", "exploratory", "abandoned", "trivial"]},
        "work_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": _STR,
                    "tickets": _STR_LIST,
                    "description": _STR,
                    "status": {"type": "string", "enum": ["done", "in_progress", "blocked", "exploratory", "abandoned"]},
                    "demo_worthy": {"type": "boolean"},
                    "evidence": _STR_LIST,
                },
                "required": ["title", "tickets", "description", "status", "demo_worthy", "evidence"],
            },
        },
        "technical_highlights": _STR_LIST,
        "problems": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"problem": _STR, "resolution": _STR},
                "required": ["problem", "resolution"],
            },
        },
        "decisions": _STR_LIST,
        "followups": _STR_LIST,
        "tickets": _STR_LIST,
        "tags": _STR_LIST,
        "is_noise": {"type": "boolean"},
    },
    "required": ["title", "objective", "outcome", "status", "work_items", "technical_highlights",
                 "problems", "decisions", "followups", "tickets", "tags", "is_noise"],
}

SESSION_SYSTEM = """You are an engineering work analyst. You will receive a digest of ONE Claude Code
coding session: metadata, the files it edited, the git/build commands it ran, and the
(truncated) conversation between the engineer (U) and the assistant (A).

Your job is to extract WHAT WORK WAS DONE, in terms a sprint demo audience cares about.

Rules:
- Work from evidence in the digest only. Never invent tickets, files, or outcomes.
- The engineer's prompts define the intent; the assistant's messages, edited files, and git
  commands show what actually happened. A 'git commit'/'git push' is strong evidence of completion.
- Ticket ids look like FIX-133803 (PREFIX-NUMBER). Copy them exactly as written.
- Split distinct pieces of work into separate work_items (a feature, a bug fix, a CI change,
  an investigation...). Merge trivial back-and-forth into the item it belongs to.
- status: done = finished/committed; in_progress = real progress but not finished;
  blocked = stuck on something external; exploratory = analysis/debugging/reading with no
  code change; abandoned = started and dropped; trivial = greetings, tests of the tool, nothing.
- demo_worthy = true only for user-visible or team-visible outcomes someone could show or
  explain in a demo (a working feature, a fixed bug, a faster pipeline, a new tool, a doc).
- problems: real obstacles hit during the session and how they were resolved (or not).
- decisions: notable technical or product decisions taken, with the reason if stated.
- followups: explicit TODOs, deferred items, or things the engineer said they would do later.
- technical_highlights: 0-5 crisp bullets a senior engineer would find interesting.
- tags: 2-6 short lowercase topic tags (e.g. "electron", "ci", "macos-signing", "logging").
- is_noise = true when the session contains no meaningful engineering work.
- Be concrete and compact. Prefer specific nouns (file names, commands, error codes) over adjectives.
- If the digest is marked PART i of N, summarize only that part; parts are merged later."""

SPRINT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "sprint_title": _STR,
        "executive_summary": _STR,
        "headline_achievements": _STR_LIST,
        "demo_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": _STR,
                    "tickets": _STR_LIST,
                    "status": {"type": "string", "enum": ["done", "in_progress", "blocked", "planned"]},
                    "what_changed": _STR,
                    "why_it_matters": _STR,
                    "demo_steps": _STR_LIST,
                    "talking_points": _STR_LIST,
                    "evidence": _STR_LIST,
                },
                "required": ["title", "tickets", "status", "what_changed", "why_it_matters",
                             "demo_steps", "talking_points", "evidence"],
            },
        },
        "work_streams": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": _STR,
                    "tickets": _STR_LIST,
                    "status": {"type": "string", "enum": ["done", "in_progress", "blocked", "planned", "dropped"]},
                    "summary": _STR,
                    "details": _STR_LIST,
                    "branches": _STR_LIST,
                    "prs": _STR_LIST,
                },
                "required": ["name", "tickets", "status", "summary", "details", "branches", "prs"],
            },
        },
        "technical_decisions": _STR_LIST,
        "challenges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"problem": _STR, "resolution": _STR},
                "required": ["problem", "resolution"],
            },
        },
        "learnings": _STR_LIST,
        "carry_over": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"item": _STR, "tickets": _STR_LIST, "reason": _STR},
                "required": ["item", "tickets", "reason"],
            },
        },
        "risks_and_asks": _STR_LIST,
        "metrics_commentary": _STR,
    },
    "required": ["sprint_title", "executive_summary", "headline_achievements", "demo_items",
                 "work_streams", "technical_decisions", "challenges", "learnings", "carry_over",
                 "risks_and_asks", "metrics_commentary"],
}

SPRINT_SYSTEM = """You are a staff engineer helping a teammate prepare their SPRINT DEMO report.

You receive, for one engineer and one sprint window:
  1. computed metrics (sessions, prompts, active hours, commits, tickets),
  2. the engineer's git commits, active branches and merged pull requests,
  3. plan documents written during the sprint,
  4. structured summaries of every Claude Code session in the window (chronological).

Produce the content of a polished sprint demo report as JSON matching the schema.

Guidelines:
- Organize by OUTCOME (ticket / work stream), never by session or by day. Many sessions
  contribute to one ticket; merge them. Deduplicate aggressively.
- Ground everything in the evidence. Cite ticket ids, PR numbers (#63), branch names and
  commit subjects where they exist. Do not invent anything, do not pad.
- Status comes from the DEFINITION OF DONE stated in the material, applied to the git facts, in
  this priority order: (1) The git facts. Every branch is annotated MERGED (with the pull request
  or branch that contains it) or NOT MERGED, computed from history, and the TICKET INDEX carries a
  derived hint per ticket. MERGED means done. Commits only on a NOT MERGED branch mean in_progress.
  (2) Session evidence ('git commit'/'git push', the engineer saying it is finished) refines the
  description but never promotes a NOT MERGED branch to done. Say "awaiting review" or "on branch"
  for those instead. (3) Analysis with no code is exploratory; fold it into the related stream or
  into learnings.
- demo_items: 3-8 items the engineer can actually SHOW or WALK THROUGH in a demo, ordered by
  impact. Each needs concrete demo_steps (what to open/run/click, 2-6 steps) and 2-4
  talking_points (the "so what": user impact, risk removed, time saved, numbers if known).
  Include in-progress items only if there is something visible to show.
- work_streams: complete coverage of everything worked on, including small items and
  investigations that did not become demo items. Keep each summary to 1-3 sentences and each
  detail bullet to one line.
- headline_achievements: 3-7 one-line bullets for the first slide.
- executive_summary: 3-6 sentences, plain language for a mixed audience (PM, QA, engineers).
- challenges: real obstacles and how they were resolved; learnings: reusable insights.
- carry_over: unfinished work, explicit follow-ups, deferred items, with the reason.
- risks_and_asks: dependencies on other teams, reviews needed, decisions required. Empty list if none.
- metrics_commentary: 1-3 sentences interpreting the metrics (where the effort went).
- Ignore sessions flagged as noise, and ignore the tooling session that generated this report
  unless it is the only work.
- Write in crisp, professional English. Use **bold** sparingly for emphasis inside strings;
  no other markdown. Never include secrets or personal data."""


def session_prompt(digest_text: str, part: Tuple[int, int] = None) -> str:
    head = "Analyze the following Claude Code session digest and return the structured summary."
    if part:
        head += f" This is PART {part[0]} of {part[1]} of a long session."
    return f"{head}\n\n<session_digest>\n{digest_text}\n</session_digest>"


def sprint_prompt(context_text: str) -> str:
    return ("Prepare the sprint demo report from the material below and return the structured JSON.\n\n"
            f"<sprint_material>\n{context_text}\n</sprint_material>")
