#!/usr/bin/env bash
#
# sprint-report installer. Everything it does, in full:
#
#   1. checks python3 >= 3.9 exists, and warns if `git` or `claude` are missing
#   2. creates ~/.local/bin and ~/.claude/skills if they do not exist
#   3. makes TWO symlinks pointing back into this folder:
#        ~/.local/bin/sprint-report        -> <this folder>/sprint_report.py
#        ~/.claude/skills/sprint-report    -> <this folder>/skills/sprint-report
#   4. marks sprint_report.py executable
#
# It never downloads anything, never uses sudo, never writes outside those two
# paths, and refuses to replace a file it did not create. Run with --dry-run to
# see the exact commands first, or --uninstall to remove the two symlinks.
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
BIN_DIR="${HOME:-}/.local/bin"
SKILL_DIR="${HOME:-}/.claude/skills"
BIN="$BIN_DIR/sprint-report"
SKILL="$SKILL_DIR/sprint-report"
DRY=0
FORCE=0
MODE=install

for arg in "$@"; do
  case "$arg" in
    --dry-run)   DRY=1 ;;
    --force)     FORCE=1 ;;
    --uninstall) MODE=uninstall ;;
    -h|--help)   sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *)           echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

[ -n "${HOME:-}" ] || { echo "HOME is not set; cannot decide where to install" >&2; exit 1; }
if [ "$(id -u)" = "0" ]; then
  echo "refusing to run as root: this installs into your own home directory only." >&2
  echo "run it as your normal user, without sudo." >&2
  exit 1
fi

run() {  # echo the command, then run it unless --dry-run
  echo "  \$ $*"
  [ "$DRY" = "1" ] || "$@"
}

# True when $1 is a symlink that resolves to something inside this folder.
points_here() {
  [ -L "$1" ] || return 1
  local dest dir
  dest="$(readlink "$1")" || return 1
  case "$dest" in
    /*) ;;                                    # absolute: use as is
    *)  dir="$(cd "$(dirname "$1")" 2>/dev/null && pwd -P)" || return 1
        dest="$dir/$dest" ;;                  # relative: resolve against the link's own directory
  esac
  case "$dest" in
    "$REPO"|"$REPO"/*) return 0 ;;
    *) return 1 ;;
  esac
}

if [ "$MODE" = uninstall ]; then
  echo "uninstalling (reports under ~/sprint-reports are left alone):"
  for path in "$BIN" "$SKILL"; do
    if points_here "$path"; then
      run rm -f "$path"
    elif [ -e "$path" ] || [ -L "$path" ]; then
      echo "  ! $path exists but was not created by this installer; leaving it alone"
    else
      echo "  - $path is not present"
    fi
  done
  exit 0
fi

# --- checks -----------------------------------------------------------------
command -v python3 >/dev/null 2>&1 || { echo "python3 (3.9+) is required" >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || { echo "python3 is $(python3 -V 2>&1); 3.9 or newer is required" >&2; exit 1; }

for required in sprint_report.py sprintreport/__init__.py skills/sprint-report/SKILL.md; do
  [ -e "$REPO/$required" ] || { echo "missing $required — extract the whole zip, then run this from inside it" >&2; exit 1; }
done

command -v git >/dev/null 2>&1 || echo "warning: git not found; reports will have no commit history"
command -v claude >/dev/null 2>&1 || echo "warning: the 'claude' CLI is not on PATH; install Claude Code and sign in before generating a report"

# --- install ----------------------------------------------------------------
link() {  # link <source> <destination>
  local src="$1" dest="$2"
  if points_here "$dest"; then
    run ln -sfn "$src" "$dest"          # ours already: refresh it
  elif [ -e "$dest" ] || [ -L "$dest" ]; then
    if [ "$FORCE" = "1" ]; then
      echo "  ! replacing existing $dest (--force)"
      run rm -rf -- "$dest"
      run ln -sfn "$src" "$dest"
    else
      echo "  ! $dest already exists and was not created by this installer." >&2
      echo "    Move it aside, or re-run with --force to replace it." >&2
      exit 1
    fi
  else
    run ln -sfn "$src" "$dest"
  fi
}

echo "installing sprint-report from $REPO"
if [ "$DRY" = "1" ]; then echo "(dry run: nothing below is actually executed)"; fi
run mkdir -p "$BIN_DIR" "$SKILL_DIR"
link "$REPO/sprint_report.py" "$BIN"
link "$REPO/skills/sprint-report" "$SKILL"
run chmod +x "$REPO/sprint_report.py"

echo
echo "installed:"
echo "  command  $BIN"
echo "  skill    $SKILL      (type /sprint-report inside Claude Code)"
echo "keep this folder where it is: both links point back into it."
case ":${PATH:-}:" in
  *":$BIN_DIR:"*) ;;
  *) echo
     echo "note: $BIN_DIR is not on your PATH. Add this to ~/.zshrc (or ~/.bashrc):"
     echo "      export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac
SINCE="$(date -v-14d +%F 2>/dev/null || date -d '14 days ago' +%F 2>/dev/null || echo 2026-09-01)"
echo
echo "try it (fast, no model calls, nothing sent anywhere):"
echo "      sprint-report --since $SINCE --no-llm"
