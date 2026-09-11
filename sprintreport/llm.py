"""Thin wrapper around the local `claude` CLI in non-interactive mode, plus a JSON cache.

Why the CLI and not the SDK: the CLI already holds the user's credentials (OAuth or key
helper), so this tool never touches API keys: _clean_env makes sure one can never reach the
subprocess. Every call is a fresh, tool-less, no-persistence `claude -p` with a replaced system
prompt and (optionally) a JSON schema for structured output.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, Optional, Tuple


class ClaudeError(RuntimeError):
    pass


def _clean_env() -> Dict[str, str]:
    """Drop the nested-session markers so `claude -p` runs happily from inside another session.

    Also drop any inherited API key. The subprocess must authenticate as the Claude Code session
    that launched it; a key exported in a shell profile outranks that login and fails every call
    with a 401 as soon as it goes stale.
    """
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("CLAUDE_CODE_") or k in ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT",
                                                  "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_PREVIEW_CLASSIFIER_FLOOR"):
            env.pop(k, None)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(k, None)
    return env


def _extract_json(stdout: str) -> Any:
    """`--output-format json` prints one object, or (with verbose settings) an array of events."""
    s = stdout.strip()
    if not s:
        raise ClaudeError("claude produced no output")
    start = min([i for i in (s.find("["), s.find("{")) if i >= 0] or [0])
    try:
        data, _ = json.JSONDecoder().raw_decode(s[start:])
    except json.JSONDecodeError as e:
        raise ClaudeError(f"could not parse claude output as JSON: {e}\n--- head ---\n{s[:800]}") from None
    return data


def _find_result(data: Any) -> Dict[str, Any]:
    if isinstance(data, dict) and data.get("type") == "result":
        return data
    if isinstance(data, list):
        for item in reversed(data):
            if isinstance(item, dict) and item.get("type") == "result":
                return item
    if isinstance(data, dict) and ("result" in data or "structured_output" in data):
        return data
    raise ClaudeError(f"no result object in claude output (got {type(data).__name__})")


class ClaudeCLI:
    def __init__(self, binary: str = "claude", timeout: int = 900, retries: int = 2,
                 log=None, dry_run: bool = False):
        self.binary = shutil.which(binary) or binary
        self.timeout = timeout
        self.retries = retries
        self.log = log or (lambda *_: None)
        self.dry_run = dry_run
        self.total_cost_usd = 0.0
        self.total_calls = 0
        self.total_ms = 0

    def available(self) -> bool:
        return bool(shutil.which(self.binary)) or os.path.exists(self.binary)

    def run(self, prompt: str, system: str, model: str, schema: Optional[Dict[str, Any]] = None,
            effort: Optional[str] = None, label: str = "") -> Tuple[Any, Dict[str, Any]]:
        """Run one non-interactive call. Returns (structured_or_text_result, usage_info)."""
        cmd = [self.binary, "-p",
               "--model", model,
               "--output-format", "json",
               "--no-session-persistence",
               "--permission-mode", "dontAsk",
               "--tools", "",                 # pure text task: no file/shell tools
               "--strict-mcp-config",         # and no MCP servers
               "--system-prompt", system]
        if effort:
            cmd += ["--effort", effort]
        if schema is not None:
            cmd += ["--json-schema", json.dumps(schema, separators=(",", ":"))]
        if self.dry_run:
            self.log(f"  [dry-run] would call: {' '.join(cmd[:6])} ... ({len(prompt):,} chars prompt)")
            return None, {}
        last_err: Optional[str] = None
        for attempt in range(self.retries + 1):
            t0 = time.time()
            try:
                p = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                   timeout=self.timeout, env=_clean_env(), check=False)
            except subprocess.TimeoutExpired:
                last_err = f"timed out after {self.timeout}s"
                self.log(f"  ! {label} {last_err} (attempt {attempt + 1})")
                continue
            except OSError as e:
                raise ClaudeError(f"could not run {self.binary}: {e}") from None
            elapsed = int((time.time() - t0) * 1000)
            if p.returncode != 0 and not p.stdout.strip():
                last_err = f"exit {p.returncode}: {p.stderr.strip()[:600]}"
                self.log(f"  ! {label} {last_err} (attempt {attempt + 1})")
                time.sleep(5 * (attempt + 1))
                continue
            try:
                res = _find_result(_extract_json(p.stdout))
            except ClaudeError as e:
                last_err = str(e)
                self.log(f"  ! {label} {last_err[:300]} (attempt {attempt + 1})")
                time.sleep(3)
                continue
            usage = {
                "cost_usd": float(res.get("total_cost_usd") or 0.0),
                "duration_ms": int(res.get("duration_ms") or elapsed),
                "num_turns": res.get("num_turns"),
                "model": model,
            }
            if res.get("is_error") or res.get("subtype") not in (None, "success"):
                last_err = f"{res.get('subtype')}: {str(res.get('result'))[:600]}"
                self.log(f"  ! {label} claude returned an error: {last_err} (attempt {attempt + 1})")
                time.sleep(10 * (attempt + 1))
                continue
            self.total_calls += 1
            self.total_cost_usd += usage["cost_usd"]
            self.total_ms += usage["duration_ms"]
            if schema is not None:
                so = res.get("structured_output")
                if so is None:
                    raw = res.get("result")
                    try:
                        so = json.loads(raw) if isinstance(raw, str) else raw
                    except json.JSONDecodeError:
                        last_err = "structured output missing and result is not JSON"
                        self.log(f"  ! {label} {last_err} (attempt {attempt + 1})")
                        continue
                return so, usage
            return res.get("result"), usage
        raise ClaudeError(f"{label or 'claude call'} failed after {self.retries + 1} attempts: {last_err}")


class JsonCache:
    """One JSON file per key, invalidated by a caller-provided fingerprint string."""

    def __init__(self, directory: str):
        self.dir = directory
        os.makedirs(self.dir, mode=0o700, exist_ok=True)

    def _path(self, key: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in key)
        return os.path.join(self.dir, safe + ".json")

    def get(self, key: str, fingerprint: str) -> Optional[Any]:
        p = self._path(key)
        if not os.path.exists(p):
            return None
        try:
            with open(p, "r", encoding="utf-8") as fh:
                o = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None
        if o.get("fingerprint") != fingerprint:
            return None
        return o.get("value")

    def put(self, key: str, fingerprint: str, value: Any) -> None:
        p = self._path(key)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"fingerprint": fingerprint, "value": value}, fh, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)


def stderr_log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
