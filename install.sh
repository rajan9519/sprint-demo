#!/usr/bin/env bash
# Install (or remove) sprint-report for the current user:
#   ./install.sh            -> `sprint-report` command on PATH + /sprint-report skill in Claude Code
#   ./install.sh --uninstall
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin/sprint-report"
SKILL="$HOME/.claude/skills/sprint-report"

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -f "$BIN" "$SKILL"
  echo "removed $BIN and $SKILL (reports in ~/sprint-reports were left in place)"
  exit 0
fi

command -v python3 >/dev/null 2>&1 || { echo "python3 is required (3.9+)"; exit 1; }
python3 - <<'PY' || { echo "python 3.9+ is required"; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)
PY
command -v git >/dev/null 2>&1 || echo "warning: git not found; commit history will be skipped"
command -v claude >/dev/null 2>&1 || echo "warning: 'claude' CLI not found on PATH; install Claude Code and log in before generating reports"

mkdir -p "$HOME/.local/bin" "$HOME/.claude/skills"
ln -sfn "$REPO/sprint_report.py" "$BIN"
ln -sfn "$REPO/skills/sprint-report" "$SKILL"
chmod +x "$REPO/sprint_report.py"

echo "installed:"
echo "  command  $BIN -> $REPO/sprint_report.py"
echo "  skill    $SKILL -> $REPO/skills/sprint-report   (type /sprint-report in Claude Code)"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "note: add \$HOME/.local/bin to your PATH to use the 'sprint-report' command directly" ;;
esac
TWO_WEEKS_AGO="$(date -v-14d +%F 2>/dev/null || date -d '14 days ago' +%F 2>/dev/null || echo YYYY-MM-DD)"
echo "try:     sprint-report --since $TWO_WEEKS_AGO --no-llm     (fast, no model calls)"
echo "then:    sprint-report --since $TWO_WEEKS_AGO --open"
