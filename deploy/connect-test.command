#!/bin/bash
set -eu

echo 'Agent PTT: http://localhost:8780/workspace/'
echo 'Keep this window open for access. Press Ctrl-C to close the tunnel.'
echo 'The server continues running independently on Proxmox.'
exec ssh -F "$HOME/.ssh/agent-ptt-test.conf" \
  -o ExitOnForwardFailure=yes \
  -N -L 127.0.0.1:8780:127.0.0.1:8080 agent-ptt-test
