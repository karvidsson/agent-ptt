#!/usr/bin/env bash
# A team of AI agents build something together, out loud, in an Agent PTT
# channel — and you can weigh in from the web UI while they work.
#
#   ./examples/work-session.sh                      # default project, 9 turns
#   ./examples/work-session.sh "a CLI that renames photos by EXIF date" 12
#
# This is not a debate. Each agent takes a turn, does a real piece of work in
# a shared project directory with its own tools, and then reports one spoken
# sentence to the channel. They only talk to each other when one of them
# actually needs something to keep going.
#
# You are at the table too: join the channel from the web UI (or
# `agent-ptt join <id> --handle you`) and say something. Everyone hears it,
# treats it as a decision rather than an opinion, and acts on it if it
# touches their work — nobody parrots it back.
#
# Override the team with TEAM="Name|what they own|voice-id;...", the project
# directory with PROJECT_DIR=<path>, and the channel with CHANNEL=<id>.
set -euo pipefail

SERVER="${AGENT_PTT_URL:-http://localhost:8770}"
# Sent on every curl call when the server runs with AGENT_PTT_API_KEY set.
AUTH=()
[[ -n "${AGENT_PTT_API_KEY:-}" ]] && AUTH=(-H "Authorization: Bearer ${AGENT_PTT_API_KEY}")
# A bare number as the only argument means turns, not a project brief.
if [ $# -eq 1 ] && [[ "$1" =~ ^[0-9]+$ ]]; then set -- "" "$1"; fi
PROJECT="${1:-a single-page tip calculator — index.html, style.css, app.js, no frameworks and no build step}"
TURNS="${2:-9}"
PROJECT_DIR="${PROJECT_DIR:-$(mktemp -d -t agent-ptt-worksession)}"

DEFAULT_TEAM="Mara|the markup and the styling — index.html and style.css|fantine;\
Kai|the behaviour — app.js and anything the page has to actually do|javert;\
Roy|reviewing what the other two wrote and fixing what is broken, rather than adding features|javert"
IFS=';' read -r -a TEAM <<<"${TEAM:-$DEFAULT_TEAM}"

for bin in jq claude curl; do
  command -v "$bin" >/dev/null || { echo "missing dependency: $bin" >&2; exit 1; }
done
curl -sf ${AUTH[@]+"${AUTH[@]}"} "$SERVER/channels" >/dev/null || {
  echo "no Agent PTT server at $SERVER — start one with: uv run agent-ptt server start" >&2
  exit 1
}

mkdir -p "$PROJECT_DIR"

# 1. A channel to work out loud in. Set CHANNEL=<id> to reuse an existing one
#    (handy when you want the web UI open on it before they start).
CHANNEL="${CHANNEL:-$(curl -sS ${AUTH[@]+"${AUTH[@]}"} -X POST "$SERVER/channels" \
  -H 'content-type: application/json' \
  -d "$(jq -nc --arg n "Work session: $PROJECT" '{name:$n}')" | jq -r .channel_id)}"

# 2. One participant per teammate, each with a clearly different voice.
HANDLES=() ROLES=() KEYS=()
for member in "${TEAM[@]}"; do
  IFS='|' read -r handle role voice <<<"$member"
  key=$(curl -sS ${AUTH[@]+"${AUTH[@]}"} -X POST "$SERVER/channels/$CHANNEL/join" \
    -H 'content-type: application/json' \
    -d "$(jq -nc --arg h "$handle" --arg v "${voice:-}" \
          '{handle:$h, voice_id:(if $v == "" then null else $v end)}')" | jq -r .key_id)
  HANDLES+=("$handle"); ROLES+=("$role"); KEYS+=("$key")
done

TEAM_LIST=$(IFS=, ; echo "${HANDLES[*]}")

echo "channel: $CHANNEL"
echo "team:    ${HANDLES[*]}"
echo "working: $PROJECT_DIR"
echo "listen:  agent-ptt listen $CHANNEL   (or open $SERVER/ui/#$CHANNEL)"
echo "         join from the UI and say something — they will act on it"
echo

# Whatever the human last said, carried to every teammate exactly once. They
# don't all answer it: they act on it if it touches their work, and otherwise
# carry on, which is what a room full of working people actually does.
GUEST_ID="" GUEST_HANDLE="" GUEST_TEXT="" UNHEARD=" "

is_teammate() {
  local h="$1" x
  for x in "${HANDLES[@]}"; do [ "$x" = "$h" ] && return 0; done
  return 1
}
has_not_heard() { [[ "$UNHEARD" == *" $1 "* ]]; }
has_heard() { UNHEARD="${UNHEARD/ $1 / }"; }

fetch_state() {
  local json guest gid ghandle gtext
  json=$(curl -sS ${AUTH[@]+"${AUTH[@]}"} "$SERVER/channels/$CHANNEL/history")
  TRANSCRIPT=$(printf '%s' "$json" | jq -r '.[-10:][] | "\(.handle): \(.text)"')

  guest=$(printf '%s' "$json" | jq -r --arg p "$TEAM_LIST" '
    ($p | split(",")) as $team
    | [.[] | select(.handle as $h | ($team | index($h)) == null)] | last
    | if . == null then "" else [.message_id, .handle, .text] | @tsv end')
  IFS=$'\t' read -r gid ghandle gtext <<<"$guest" || true

  if [ -n "${gid:-}" ] && [ "$gid" != "$GUEST_ID" ]; then
    GUEST_ID="$gid"; GUEST_HANDLE="$ghandle"; GUEST_TEXT="$gtext"
    UNHEARD=" ${HANDLES[*]} "
  fi
}

# 3. One turn: read the room, do real work in the project directory, then say
#    one sentence about it. The work is the point; the sentence is the report.
turn() { # $1=key  $2=handle  $3=role
  local human out reply marker files
  fetch_state

  human=""
  if [ -n "$GUEST_ID" ] && has_not_heard "$2"; then
    human="$GUEST_HANDLE is the human at the table, and just said:
  \"$GUEST_TEXT\"
Treat that as a decision, not an opinion. If it changes your work, do it now
and say so in one clause. If it doesn't touch your work, carry on without
mentioning it at all."
  fi

  marker="$PROJECT_DIR/.ptt-turn-marker"; : >"$marker"

  out=$(cd "$PROJECT_DIR" && claude -p "You are $2, working at one table with $TEAM_LIST on a shared project.

The project: $PROJECT
You own: $3
You are already in the project directory. It may be empty — if it is, start.

$human

What the room has heard so far (everyone hears everything):
${TRANSCRIPT:-(nothing yet — you are first)}

Do the next concrete piece of work NOW, with your tools: read what your
teammates have already written, then create or edit the files you own. One
small step per turn. Never redo something a teammate already reported doing,
and never edit a file another teammate owns — ask them instead.

Then report to the room. What you say is heard out loud, so:
- ONE sentence, at most 25 words, plain spoken English.
- Say what you just did or found, concretely.
- Address a teammate by name ONLY if you need something from them to keep
  going, or they asked you something. Otherwise just report and carry on.
- No small talk, no agreeing, no encouragement, no restating the plan.

End your reply with exactly one line, nothing after it:
SAY: <your sentence>" \
    --permission-mode acceptEdits \
    --allowedTools Read Write Edit Glob Grep < /dev/null 2>&1) || true

  reply=$(printf '%s\n' "$out" | grep -m1 '^SAY:' | sed 's/^SAY:[[:space:]]*//' || true)
  [ -n "$reply" ] || reply=$(printf '%s\n' "$out" | grep -v '^[[:space:]]*$' | tail -1)
  reply=$(printf '%s' "$reply" | tr '\n' ' ' | tr -s ' ' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
  [ -n "$reply" ] || return 0
  has_heard "$2"

  echo "$2: $reply"
  files=$(cd "$PROJECT_DIR" && find . -type f -newer "$marker" -not -name '.ptt-turn-marker' \
    -not -path './.*' | sed 's|^\./||' | tr '\n' ' ')
  [ -z "$files" ] || echo "  ✎ $files"

  curl -sS ${AUTH[@]+"${AUTH[@]}"} -X POST "$SERVER/channels/$CHANNEL/say" \
    -H 'content-type: application/json' \
    -d "$(jq -nc --arg k "$1" --arg t "$reply" '{key_id:$k,text:$t}')" >/dev/null
  sleep 2   # let the speakers finish the line before the next one starts
}

# 4. Round-robin for N turns.
n=${#HANDLES[@]}
for ((i = 0; i < TURNS; i++)); do
  s=$((i % n))
  turn "${KEYS[$s]}" "${HANDLES[$s]}" "${ROLES[$s]}"
done

rm -f "$PROJECT_DIR/.ptt-turn-marker"
echo
echo "they built: $PROJECT_DIR"
ls -1 "$PROJECT_DIR"
echo "transcript: agent-ptt channel history $CHANNEL"
