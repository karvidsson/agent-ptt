#!/usr/bin/env bash
# A panel of AI agents talk to each other in an Agent PTT channel — out loud.
#
#   ./examples/roundtable.sh "whether code comments are a sign of failure" 9
#   ./examples/roundtable.sh "whether microservices were a mistake" 6
#
# Topics that reliably start a fight:
#   whether tabs or spaces won the argument
#   whether code comments are a sign of failure
#   whether microservices were a mistake
#   whether rewriting it in Rust ever pays for itself
#   whether vim users are actually faster or just louder
#   whether TypeScript's type system has gone too far
#   whether the terminal will outlive the IDE
#   whether story points have ever once been useful
#   whether AI coding agents should merge their own pull requests
#
# Each agent is a headless `claude -p` call that reads the channel transcript
# and answers whoever spoke last. Nobody holds a session: they are just
# participation keys posting to the same channel over REST, which is all any
# agent needs to join a conversation.
#
# Join the channel yourself — `agent-ptt join <id> --handle you`, or the Join
# box in the web UI — and cut in at any time. Everyone on the panel answers
# you by name before they go back to arguing with each other.
#
# Override the panel with PANEL="Name|persona|voice-id;Name|persona|voice-id",
# and the channel with CHANNEL=<existing-channel-id>.
# Drop the voice-id and the handle gets an auto-designed one instead.
set -euo pipefail

SERVER="${AGENT_PTT_URL:-http://localhost:8770}"
# Sent on every curl call when the server runs with AGENT_PTT_API_KEY set.
AUTH=()
[[ -n "${AGENT_PTT_API_KEY:-}" ]] && AUTH=(-H "Authorization: Bearer ${AGENT_PTT_API_KEY}")
# A bare number as the only argument means turns, not a subject to debate.
if [ $# -eq 1 ] && [[ "$1" =~ ^[0-9]+$ ]]; then set -- "" "$1"; fi
SUBJECT="${1:-whether code comments are a sign of failure}"
TURNS="${2:-9}"

DEFAULT_PANEL="Nova|a true believer, certain the new way is obviously right and the old guard is just scared|javert;\
Ada|an engineer who trusts benchmarks and evidence over taste, and says so bluntly|fantine;\
Roy|a grumpy veteran who has watched this exact idea fail twice already under a different name|javert"
IFS=';' read -r -a PANEL <<<"${PANEL:-$DEFAULT_PANEL}"

for bin in jq claude curl; do
  command -v "$bin" >/dev/null || { echo "missing dependency: $bin" >&2; exit 1; }
done
curl -sf ${AUTH[@]+"${AUTH[@]}"} "$SERVER/channels" >/dev/null || {
  echo "no Agent PTT server at $SERVER — start one with: uv run agent-ptt server start" >&2
  exit 1
}

# 1. A channel for them to talk in. Set CHANNEL=<id> to reuse an existing one
#    (handy when you want the web UI open on it before the panel starts).
CHANNEL="${CHANNEL:-$(curl -sS ${AUTH[@]+"${AUTH[@]}"} -X POST "$SERVER/channels" \
  -H 'content-type: application/json' \
  -d "$(jq -nc --arg n "Panel: $SUBJECT" '{name:$n}')" | jq -r .channel_id)}"

# 2. One participant per panelist, each with a clearly different voice. Leave
#    voice_id null and the server designs one from the handle and pins it.
HANDLES=() PERSONAS=() KEYS=()
for member in "${PANEL[@]}"; do
  IFS='|' read -r handle persona voice <<<"$member"
  key=$(curl -sS ${AUTH[@]+"${AUTH[@]}"} -X POST "$SERVER/channels/$CHANNEL/join" \
    -H 'content-type: application/json' \
    -d "$(jq -nc --arg h "$handle" --arg v "${voice:-}" \
          '{handle:$h, voice_id:(if $v == "" then null else $v end)}')" | jq -r .key_id)
  HANDLES+=("$handle"); PERSONAS+=("$persona"); KEYS+=("$key")
done

PANEL_LIST=$(IFS=, ; echo "${HANDLES[*]}")

echo "channel: $CHANNEL"
echo "panel:   ${HANDLES[*]}"
echo "listen:  agent-ptt listen $CHANNEL   (or open $SERVER/ui/)"
echo "         join the channel and cut in — every panelist will answer you"
echo

# A question from the room stays open until every panelist has answered it.
# Without this only the next speaker in the rotation replies, and the rest
# carry on with their own argument as if nobody had spoken.
GUEST_ID="" GUEST_HANDLE="" GUEST_TEXT="" PENDING=" "

is_panelist() {
  local h="$1" x
  for x in "${HANDLES[@]}"; do [ "$x" = "$h" ] && return 0; done
  return 1
}
owes_the_room() { [[ "$PENDING" == *" $1 "* ]]; }
answered_the_room() { PENDING="${PENDING/ $1 / }"; }

# Reads the channel into TRANSCRIPT / LAST_ID / LAST_HANDLE, and picks up the
# newest message from anyone who isn't on the panel.
fetch_state() {
  local json guest gid ghandle gtext
  json=$(curl -sS ${AUTH[@]+"${AUTH[@]}"} "$SERVER/channels/$CHANNEL/history")
  TRANSCRIPT=$(printf '%s' "$json" | jq -r '.[-8:][] | "\(.handle): \(.text)"')
  LAST_ID=$(printf '%s' "$json" | jq -r '.[-1].message_id // ""')
  LAST_HANDLE=$(printf '%s' "$json" | jq -r '.[-1].handle // ""')

  guest=$(printf '%s' "$json" | jq -r --arg p "$PANEL_LIST" '
    ($p | split(",")) as $panel
    | [.[] | select(.handle as $h | ($panel | index($h)) == null)] | last
    | if . == null then "" else [.message_id, .handle, .text] | @tsv end')
  IFS=$'\t' read -r gid ghandle gtext <<<"$guest" || true

  # A new question from the room puts every panelist back on the hook.
  if [ -n "${gid:-}" ] && [ "$gid" != "$GUEST_ID" ]; then
    GUEST_ID="$gid"; GUEST_HANDLE="$ghandle"; GUEST_TEXT="$gtext"
    PENDING=" ${HANDLES[*]} "
  fi
}

# 3. One turn: read the channel, think, speak. Anyone in the channel who isn't
#    on the panel is a human in the room, and gets answered by everyone —
#    including if they cut in while this turn was still thinking.
turn() { # $1=key  $2=handle  $3=persona
  local reply guest attempt pre_guest
  for attempt in 1 2; do
    fetch_state
    pre_guest="$GUEST_ID"

    guest=""
    if [ -n "$GUEST_ID" ] && owes_the_room "$2"; then
      guest="$GUEST_HANDLE is a human in the room, not a panelist, and said:
  \"$GUEST_TEXT\"
You have not answered that yet. Answer $GUEST_HANDLE directly and by name, in
your own voice and from your own position — do not repeat what another
panelist already told them — then go back to arguing with the panel."
    fi

    reply=$(claude -p "You are $2, on a panel discussing: $SUBJECT
You are $3. The other panelists are: $PANEL_LIST.
$guest

Transcript so far:
${TRANSCRIPT:-(nothing yet — you open the discussion)}

Reply with ONE spoken sentence of at most 25 words, in character. Answer the
previous speaker directly and name them. Output the sentence only: no quotes,
no preamble, no markdown, nothing that isn't meant to be heard." < /dev/null)

    # Someone cut in while we were thinking — throw the answer away and think
    # again with their words in front of us.
    fetch_state
    if [ "$attempt" -eq 1 ] && [ "$GUEST_ID" != "$pre_guest" ]; then
      echo "  … $GUEST_HANDLE cut in, $2 is rethinking"
      continue
    fi
    break
  done

  reply=$(printf '%s' "$reply" | tr '\n' ' ' | tr -s ' ' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
  [ -n "$reply" ] || return 0
  answered_the_room "$2"

  echo "$2: $reply"
  curl -sS ${AUTH[@]+"${AUTH[@]}"} -X POST "$SERVER/channels/$CHANNEL/say" \
    -H 'content-type: application/json' \
    -d "$(jq -nc --arg k "$1" --arg t "$reply" '{key_id:$k,text:$t}')" >/dev/null
  sleep 2   # let the speakers finish the line before the next one starts
}

# 4. Round-robin for N turns.
n=${#HANDLES[@]}
for ((i = 0; i < TURNS; i++)); do
  s=$((i % n))
  turn "${KEYS[$s]}" "${HANDLES[$s]}" "${PERSONAS[$s]}"
done

echo
echo "transcript: agent-ptt channel history $CHANNEL"
