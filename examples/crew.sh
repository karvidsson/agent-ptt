#!/usr/bin/env bash
# Three real Claude Code sessions working on one real repo, reporting into one
# Agent PTT channel — and you at the table, weighing in from the web UI.
#
#   ./examples/crew.sh ~/dev/my-repo "add a --json flag to every command"
#   ./examples/crew.sh                        # this repo, asks for a goal
#
# Unlike examples/work-session.sh, these are not scripted turns: they are full
# interactive Claude Code sessions in tmux panes. You can type into any of them
# at any time. The channel is how they hear each other and you.
#
# They share ONE working tree on a scratch branch (ptt/crew) — same files, same
# branch, like three people at one table. Your checkout is never touched.
#
# Override the crew with CREW="Name|what they own|voice-id;...", the branch with
# CREW_BRANCH, the workspace with CREW_DIR, what runs without asking with
# CREW_ALLOWED_TOOLS, and how hard it asks with CREW_PERMISSION_MODE.
set -euo pipefail

SERVER="${AGENT_PTT_URL:-http://localhost:8770}"
# Sent on every curl call when the server runs with AGENT_PTT_API_KEY set.
AUTH=()
[[ -n "${AGENT_PTT_API_KEY:-}" ]] && AUTH=(-H "Authorization: Bearer ${AGENT_PTT_API_KEY}")
HOOK="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/crew/hook.py"
REPO_ARG="${1:-$PWD}"
GOAL="${2:-}"

DEFAULT_CREW="Mara|the implementation — the code that makes the goal real|fantine;\
Kai|the tests and the docs for whatever Mara builds|javert;\
Roy|reviewing what the other two wrote and fixing what is broken, rather than adding features|javert"
IFS=';' read -r -a CREW_MEMBERS <<<"${CREW:-$DEFAULT_CREW}"

for bin in git tmux claude curl jq python3; do
  command -v "$bin" >/dev/null || { echo "missing dependency: $bin" >&2; exit 1; }
done
curl -sf ${AUTH[@]+"${AUTH[@]}"} "$SERVER/channels" >/dev/null || {
  echo "no Agent PTT server at $SERVER — start one with: uv run agent-ptt server start" >&2
  exit 1
}

REPO=$(git -C "$REPO_ARG" rev-parse --show-toplevel 2>/dev/null) || {
  echo "not a git repository: $REPO_ARG" >&2; exit 1; }
REPO_NAME=$(basename "$REPO")

if [ -z "$GOAL" ]; then
  echo "what should the crew work on in $REPO_NAME?" >&2
  echo "usage: $0 [repo-path] \"the goal\"" >&2
  exit 1
fi

# Editing and committing must not block on a permission prompt, or the crew
# stalls the moment you look away. Everything else still asks — you are sitting
# right there in the pane and can answer.
#
# git is allowed as a whole rather than command by command: a per-command
# allowlist misses `git add -A && git commit`, and a seat that cannot commit
# spends its turn asking someone else to do it. Add your test runner here to
# save yourself approving it every turn, e.g.
#   CREW_ALLOWED_TOOLS='Read,Write,Edit,Glob,Grep,Bash(git:*),Bash(uv run pytest:*)'
if [ -n "${CREW_ALLOWED_TOOLS:-}" ]; then
  IFS=',' read -r -a ALLOWED <<<"$CREW_ALLOWED_TOOLS"      # comma-separated: tool specs contain spaces
else
  ALLOWED=(Read Write Edit Glob Grep "Bash(git:*)")
fi
ALLOWED_ARGS=$(printf ' %q' "${ALLOWED[@]}")

PERMISSION_MODE="${CREW_PERMISSION_MODE:-acceptEdits}"
BRANCH="${CREW_BRANCH:-ptt/crew}"
WORKSPACE="${CREW_DIR:-$HOME/.agent-ptt/crew/$REPO_NAME}"
STATE="$HOME/.agent-ptt/crew/.state/$REPO_NAME"
mkdir -p "$STATE"

# 1. One shared worktree on a scratch branch. The crew works together in here;
#    the repo you are sitting in stays exactly as it is.
if git -C "$REPO" worktree list --porcelain | grep -qx "worktree $WORKSPACE"; then
  echo "reusing worktree: $WORKSPACE"
else
  if git -C "$REPO" show-ref --verify --quiet "refs/heads/$BRANCH"; then
    git -C "$REPO" worktree add "$WORKSPACE" "$BRANCH" >/dev/null
  else
    git -C "$REPO" worktree add -b "$BRANCH" "$WORKSPACE" >/dev/null
  fi
  echo "worktree:  $WORKSPACE  (branch $BRANCH)"
fi

# 2. One channel for the crew.
CHANNEL="${CHANNEL:-$(curl -sS ${AUTH[@]+"${AUTH[@]}"} -X POST "$SERVER/channels" \
  -H 'content-type: application/json' \
  -d "$(jq -nc --arg n "$REPO_NAME crew" '{name:$n}')" | jq -r .channel_id)}"

HANDLES=() ROLES=() VOICES=()
for member in "${CREW_MEMBERS[@]}"; do
  IFS='|' read -r handle role voice <<<"$member"
  HANDLES+=("$handle"); ROLES+=("$role"); VOICES+=("${voice:-}")
done
CREW_LIST=$(IFS=, ; echo "${HANDLES[*]}")

# 3. Hooks live outside the repo, so the crew's worktree stays clean.
cat > "$STATE/settings.json" <<JSON
{
  "hooks": {
    "UserPromptSubmit": [
      { "matcher": "", "hooks": [ { "type": "command", "command": "python3 '$HOOK'", "async": true, "timeout": 30 } ] }
    ],
    "Stop": [
      { "matcher": "", "hooks": [ { "type": "command", "command": "python3 '$HOOK'", "timeout": 30 } ] }
    ]
  }
}
JSON

# The standing rules of the table — appended to every session's system prompt,
# so they survive the whole session rather than just the first turn.
cat > "$STATE/rules.md" <<RULES
You are part of a crew working at one table on the same repository, in the same
working tree, on branch $BRANCH. The others are: $CREW_LIST. A human is at the
table too and may speak at any time.

You are all in one Agent PTT voice channel. End EVERY reply with a single line:

SAY: <one sentence, at most 25 words, what you just did>

That line is read out loud to everyone, so write it to be heard: plain spoken
English, no filenames spelled out letter by letter, no markdown.

How this table works:
- Work in small steps. One file, one function, one test at a time.
- You share the working tree with the others. Re-read a file before you edit
  it: someone may have changed it while you were thinking.
- Stay inside what you own. If you need something in someone else's area, say
  their name in your SAY line and ask for it — that is the only thing that
  interrupts them, so use it when you are genuinely blocked, not to chat.
- Never agree, encourage, restate the plan, or narrate what you are about to
  do. Report what is done.
- Commit your own work as you go, small commits, present tense messages.
- When the human says something, it is a decision, not a suggestion. Act on it
  if it touches your work; if it does not, carry on without mentioning it.
RULES

# 4. One launcher per seat — readable, and re-runnable on its own.
for i in "${!HANDLES[@]}"; do
  cat > "$STATE/seat-${HANDLES[$i]}.sh" <<SEAT
#!/usr/bin/env bash
export AGENT_PTT_URL="$SERVER"
export AGENT_PTT_CHANNEL_ID="$CHANNEL"
export AGENT_PTT_HANDLE="${HANDLES[$i]}"
export AGENT_PTT_VOICE="${VOICES[$i]}"
export AGENT_PTT_CREW="$CREW_LIST"
export AGENT_PTT_API_KEY="${AGENT_PTT_API_KEY:-}"
cd "$WORKSPACE"
# --allowedTools is variadic: keep it away from the trailing prompt or the
# prompt is parsed as one more tool name and the session starts with nothing.
exec claude \\
  --allowedTools$ALLOWED_ARGS \\
  --settings "$STATE/settings.json" \\
  --append-system-prompt "\$(cat '$STATE/rules.md')" \\
  --permission-mode "$PERMISSION_MODE" \\
  "You are ${HANDLES[$i]}. You own ${ROLES[$i]}.

The crew's goal: $GOAL

Look at the repository first, then start on your part. Small steps."
SEAT
  chmod +x "$STATE/seat-${HANDLES[$i]}.sh"
done

# 5. One tmux window, one pane per seat.
SESSION="${CREW_SESSION:-crew}"
tmux kill-session -t "$SESSION" 2>/dev/null || true
tmux new-session -d -s "$SESSION" -n "$REPO_NAME" -c "$WORKSPACE" \
  "bash '$STATE/seat-${HANDLES[0]}.sh'"
for i in "${!HANDLES[@]}"; do
  [ "$i" -eq 0 ] && continue
  tmux split-window -t "$SESSION" -c "$WORKSPACE" "bash '$STATE/seat-${HANDLES[$i]}.sh'"
  tmux select-layout -t "$SESSION" tiled >/dev/null
done
tmux set-option -t "$SESSION" -g mouse on >/dev/null 2>&1 || true
tmux set-option -t "$SESSION" remain-on-exit on >/dev/null 2>&1 || true

cat <<INFO

crew:      ${HANDLES[*]}
repo:      $REPO  →  $WORKSPACE (branch $BRANCH)
goal:      $GOAL
channel:   $CHANNEL
watch:     $SERVER/ui/#$CHANNEL      ← join here and weigh in; they will hear you
attach:    tmux attach -t $SESSION
seats:     $STATE/seat-<name>.sh     ← run one on its own if you prefer

They are already working. Anything you say in the channel reaches every seat
the moment it finishes its current turn.

When they are done:
  git -C $WORKSPACE log --oneline $BRANCH
  git -C $REPO merge $BRANCH          # or throw it away:
  git -C $REPO worktree remove $WORKSPACE --force && git -C $REPO branch -D $BRANCH
INFO
