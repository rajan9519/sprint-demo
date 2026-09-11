"""sprintreport - build a sprint demo report from local Claude Code session transcripts.

Zero third-party dependencies. All LLM work is delegated to the locally installed
`claude` CLI (non-interactive `claude -p`), so the tool inherits whatever
authentication the user already has and never handles API keys itself.
"""

__version__ = "0.2.0"
