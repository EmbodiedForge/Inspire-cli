---
name: codex-chat
description: Send messages to and receive responses from OpenAI Codex running in a tmux window. Use when the user asks to discuss with, message, or coordinate with Codex.
allowed-tools: Bash(tmux *), Bash(sleep *)
---

# Codex Chat (tmux IPC)

Communicate with OpenAI Codex CLI running in a tmux session.

## Finding the Codex window

```bash
# List windows to find Codex
tmux list-windows -t claude 2>/dev/null | grep -i codex
```

Look for windows named `codex`, `codex-insp`, etc. The user will tell you which one.

## Sending messages

**CRITICAL**: Codex's TUI requires text and Enter to be sent as **two separate `send-keys` calls**. Combining them in one call (e.g. `send-keys 'msg' Enter`) silently fails — the text appears but is never submitted.

```bash
# Two-step send (REQUIRED pattern):
tmux send-keys -t claude:<window>.0 'Your message here' && sleep 0.3 && tmux send-keys -t claude:<window>.0 Enter
```

### Why two steps?
The Codex TUI input widget needs a frame to process typed text before `Enter` can trigger submission. A single `send-keys 'text' Enter` (or `C-m`) delivers both in the same event batch, so the TUI absorbs the Enter as a text-input keystroke instead of a submit action.

### Rules for sending:
1. **Always use two-step send** — text first, then `Enter` separately with `sleep 0.3` between.
2. **Keep messages short** — under 300 chars if possible. Long messages may get truncated in the TUI display.
3. **Single line only** — do NOT include newlines in the message. For complex content, write to a file and tell Codex to read it.
4. **Escape single quotes** — if your message contains `'`, use `"` for the outer quotes or escape properly.
5. **Pre-filled suggestions** (shown in the `›` prompt) are automatically replaced when you type — no need to clear them first.
6. **For long discussions**, write detailed content to a file (e.g., `.claude-review.md`) and send a short message pointing Codex to read it.

## Receiving responses

```bash
# Wait N seconds then capture the pane output
sleep <seconds> && tmux capture-pane -t claude:<window>.0 -p -S -50 | tail -50
```

### How to know Codex is done:
- **Processing**: Shows `◦ Working (Ns • esc to interrupt)` or `• Explored` / `• Read`
- **Done**: Shows the `›` prompt at the bottom with `gpt-5.4 high · NN% left` status line
- **Typical wait**: 30-60 seconds for analysis, 10-20 seconds for short answers

### Polling pattern:
```bash
# Quick check (non-blocking)
tmux capture-pane -t claude:<window>.0 -p -S -5 | tail -5

# Full response capture
tmux capture-pane -t claude:<window>.0 -p -S -80 | tail -80
```

## Keyboard / TUI notes

- **Escape** enters scroll/edit-history mode. Press `q` to return to the prompt.
- **Ctrl+U, Ctrl+A+Ctrl+K** do NOT clear the input (non-standard TUI widget).
- Pre-filled suggestions are replaced automatically when you start typing.

## Workflow for discussions

1. **Write detailed content to a file** if it's more than a sentence or two
2. **Send a short message** via tmux pointing to the file
3. **Wait 30-60s** for Codex to process
4. **Capture response** and relay to user
5. **Repeat** until agreement is reached

## Example exchange

```bash
# Write detailed review
cat > .claude-review.md << 'EOF'
# My Review
...details...
EOF

# Send short pointer (two-step!)
tmux send-keys -t claude:1.0 'I wrote my review to .claude-review.md - please read it and respond.' && sleep 0.3 && tmux send-keys -t claude:1.0 Enter

# Wait and capture
sleep 45 && tmux capture-pane -t claude:1.0 -p -S -50 | tail -50
```
