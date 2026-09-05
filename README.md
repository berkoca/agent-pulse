# claude-code-matrix

Live status of Claude Code on a 4× MAX7219 LED panel, driven by hooks.

The panel sits on your desk and tells you what Claude is doing without you
having to look at the terminal: a heartbeat when nothing is happening, a
marching border while it works, `I NEED REPLY` when it is waiting on you, and a
`DONE` report with how long the job took, how many tokens it burned and how
full the context window is.

---

## What it shows

| Claude is | The panel shows |
|-----------|-----------------|
| idle | dark, with one ECG beat every 10s as a sign of life |
| working | dashes marching around the panel edge |
| waiting on you | `I` / `NEED` / `REPLY` cycling, centred, 900ms each |
| finished | `DONE` flashes 3×, the duration holds 15s, then `token …` and `context …%` scroll past, then dark |
| interrupted (Ctrl+C) | `ABORT` flashes 2× fast, holds 3s, then dark |
| session quit | cleared |

A finished job reads, in order:

```
  D O N E          flash ×3, 1.5s
  4 m 3 9 s        static, 15s
  token 1.9M       scrolls once
  context 27%      scrolls once
  (dark)
```

`token` is everything the job was charged for - input, cache creation, cache
reads and output - summed over every request since the job began. `context` is
how full the context window is, from the most recent request.

---

## Requirements

**Tested only on macOS 15 (Darwin 25.6) with Claude Code 2.1.259.** Nothing in
it is macOS-specific except the serial device glob in `send.py`
(`/dev/cu.usbserial*`); on Linux that would be `/dev/ttyUSB*` plus membership of
the `dialout` group. It has not been run there.

### Hardware

- Arduino Nano. This one is a CH340 clone with the **older bootloader**, which
  matters when flashing (see [Install](#install)).
- 4× MAX7219 8×8 LED matrix, the common FC16 "4-in-1" module.
- A USB cable. That is the whole bill of materials.

### Software

- `arduino-cli` (a standalone binary, no Homebrew or Arduino IDE needed)
- `arduino:avr` core
- `MD_Parola` and `MD_MAX72XX` libraries
- Python 3 - **standard library only**, no `pyserial`, no `pip install`

---

## Wiring

Hardware SPI, so the data and clock pins are fixed:

| MAX7219 | Arduino Nano |
|---------|--------------|
| VCC | 5V |
| GND | GND |
| DIN | D11 (MOSI) |
| CS / LOAD | D10 |
| CLK | D13 (SCK) |

**Power.** Four modules at full brightness can pull well over an amp, more than
a USB port or the Nano's 5V rail wants to give. The sketch boots at brightness
1 for that reason, and the MAX7219 lights one row at a time so average current
tracks lit pixels ÷ 8. If you raise brightness a long way (`matrix "#B12"`) or
run the `#T` all-LEDs self test, feed the modules' VCC from a separate 5V
supply with its ground tied to the Nano's.

---

## Install

### 1. Toolchain

```sh
curl -fsSL -o acli.tar.gz \
  "https://downloads.arduino.cc/arduino-cli/arduino-cli_latest_macOS_ARM64.tar.gz"
tar xzf acli.tar.gz arduino-cli && mv arduino-cli ~/.local/bin/
arduino-cli config init
arduino-cli core install arduino:avr
arduino-cli lib install "MD_Parola"        # pulls MD_MAX72XX too
```

### 2. Flash

This board needs the `atmega328old` variant (57600 baud upload). Plain
`arduino:avr:nano` fails with "not in sync".

```sh
cd ~/Desktop/claude-code-matrix
arduino-cli compile -b arduino:avr:nano:cpu=atmega328old ClaudeMatrix
arduino-cli upload  -b arduino:avr:nano:cpu=atmega328old \
                    -p $(ls /dev/cu.usbserial* | head -1) ClaudeMatrix
```

### 3. Check it

```sh
python3 send.py -v -- "#N4m39s,token 1.9M,context 27%"
```

It should echo `NOTIFY 3` - the board confirming it parsed three fields - and
the sequence should play on the panel. (`-v` reads for one second after
writing, so the later `HOLD …` lines land after it stops listening. The boot
banner does not appear either: `send.py` consumes it while waiting for the
board to come up.)

### 4. CLI

```sh
cat > ~/.local/bin/matrix <<'SH'
#!/bin/sh
exec /usr/bin/python3 "$HOME/Desktop/claude-code-matrix/send.py" "$@"
SH
chmod +x ~/.local/bin/matrix
```

### 5. Hooks

Add to `~/.claude/settings.json`, with absolute paths:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "timeout": 5,
        "command": "/Users/you/Desktop/claude-code-matrix/prompt_hook.py" }] }
    ],
    "PreToolUse": [
      { "matcher": "AskUserQuestion", "hooks": [{ "type": "command", "timeout": 5,
        "command": "/Users/you/Desktop/claude-code-matrix/notify_hook.py '#Q'" }] }
    ],
    "PostToolUse": [
      { "matcher": "AskUserQuestion", "hooks": [{ "type": "command", "timeout": 5,
        "command": "/Users/you/Desktop/claude-code-matrix/notify_hook.py '#L'" }] }
    ],
    "Stop": [
      { "hooks": [{ "type": "command", "timeout": 5,
        "command": "/Users/you/Desktop/claude-code-matrix/stop_hook.py" }] }
    ],
    "SessionEnd": [
      { "hooks": [{ "type": "command", "timeout": 5,
        "command": "/Users/you/Desktop/claude-code-matrix/notify_hook.py '#C'" }] }
    ]
  }
}
```

---

## Files

| File | What it does |
|------|--------------|
| `ClaudeMatrix/ClaudeMatrix.ino` | Firmware. A non-blocking state machine: idle, scrolling, flashing, holding a report, working, asking, beating. Owns all the timing. |
| `send.py` | Sends one line over serial. Standard library only - `termios` directly, no `pyserial`. |
| `prompt_hook.py` | `UserPromptSubmit`: starts the working animation and the cancellation watcher. |
| `stop_hook.py` | `Stop`: works out the job's duration, tokens and context, sends the report. |
| `cancel_watch.py` | Per-turn watcher that catches Ctrl+C. See [Catching Ctrl+C](#catching-ctrlc). |
| `notify_hook.py` | Fire-and-forget hook that sends one command it is given. |

Every hook exits 0 even with no board plugged in, and none print on stdout,
because a `UserPromptSubmit` hook's stdout would be injected into the session
context.

---

## How it works

The host only speaks at four moments. Everything with a duration - the beat
interval, the report timings, the animations - is timed by the board, which is
why the panel keeps behaving sensibly with Claude Code closed.

| Event | Matcher | Sends |
|-------|---------|-------|
| `UserPromptSubmit` | — | `#L` — clear the last report, start working |
| `PreToolUse` | `AskUserQuestion` | `#Q` — waiting on an answer |
| `PostToolUse` | `AskUserQuestion` | `#L` — answered, back to working |
| `Stop` | — | `#N<duration>,token …,context …` |
| `SessionEnd` | — | `#C` — clear |

### Serial protocol

115200 baud, newline terminated:

| Command | Effect |
|---------|--------|
| `#L` | working: march the border |
| `#Q` | waiting: cycle `I` / `NEED` / `REPLY` |
| `#N<a>,<b>,<c>` | finished: flash `DONE`, hold field `a` still, scroll `b` and `c`, go dark. Fields are optional. |
| `#X` | interrupted: flash `ABORT`, hold, go dark |
| `#B<0-15>` | brightness, saved to EEPROM |
| `#S<10-150>` | scroll frame delay in ms |
| `#T` | self test: every LED for 1s (see the power note) |
| `#C` | clear now |
| anything else | scrolled once, then back to idle |

Any new line pre-empts whatever is on screen.

---

## The awkward bits

These are the things that took the longest to get right. They are all
documented in the code too, but they are the reason it is shaped this way.

### Opening the port resets the board

macOS asserts DTR on open, which resets the Nano; nothing can be written until
the sketch boots, measured at 1.47s on this board. Rather than sleep a fixed
amount, `send.py` waits for the sketch's `READY` banner, capped by `--wait`
(default 2.5s), and a `flock` on `/tmp/claude-code-matrix.lock` stops two
senders fighting over the port.

Two consequences shape the whole design. Sends are expensive, so the host only
speaks four times per turn and the board keeps its own time. And brightness has
to live in EEPROM, or `#B` would only last until the next command arrived.

The reset has one useful side effect: the panel blanks the instant a new prompt
is submitted, before the animation even starts.

### Five characters

Static text is centred, and the 5×7 font plus spacing gives 6 columns per
character, so 32 columns fit **five characters**. Only the first `#N` field is
static and bound by this; the scrolled fields can carry a label, which is why
they read `token 1.9M` rather than a bare number.

Durations are formatted to fit, trading seconds away as the scale grows:

| Elapsed | Shown |
|---------|-------|
| under 1 min | `42s` |
| under 10 min | `2m05s` |
| under 1 hour | `37m` |
| an hour or more | `1h05` |
| 10 hours or more | `13h` |

Token counts use thresholds picked so rounding can never produce a sixth
character: 99950 renders as `100k`, not `100.0k`.

The same limit is why the interrupt label is `ABORT`. `CANCEL` needs 35 columns
and cannot be centred; title-case `Cancel` fits at 27 but would be the only
status word breaking the panel's convention - **status words are upper case**
(`DONE`, `ABORT`, `I NEED REPLY`), **data labels are lower case** (`token`,
`context`). `ABORT` fits at 29 and keeps the rule.

### Buffer column 0 is the right-hand end

For this hardware type a `getColumn()` dump reads mirrored even when the
display is correct. Rows are unaffected: row 0 is the top. It only matters for
asymmetric shapes - `drawHeart()` writes its table to `COLUMNS - 1 - p` so the
ECG spike lands before the undershoot on the panel.

### Catching Ctrl+C

There is no cancellation hook. This version of Claude Code offers exactly nine
events (`PreToolUse`, `PostToolUse`, `Notification`, `UserPromptSubmit`,
`Stop`, `SubagentStop`, `SessionStart`, `SessionEnd`, `PreCompact`) and none
fire on an interrupt, so the working animation would otherwise run forever.

The session status file (`~/.claude/sessions/<pid>.json`) is no help either: a
normal finish and an interrupt both leave `status: idle`. What does work is the
transcript, which records an interrupt explicitly as a user entry reading
`[Request interrupted by user]`.

So `prompt_hook.py` writes `/tmp/claude-code-matrix-turn/<session_id>`
containing a nonce and spawns `cancel_watch.py`, which tails the transcript
from its current size and sends `#X` if that record appears. The watcher lives
only as long as the turn: `stop_hook.py` deletes the marker before anything
else so a normal finish ends it silently, a new prompt rewrites the nonce so at
most one watcher runs per session, and failing both it gives up after six
hours. Interrupts already in the transcript are ignored, since it only reads
what is appended after it starts.

One knock-on: an interrupt is logged as a *user* entry, so `is_human_turn()`
has to exclude it or it would count as a fresh prompt and reset the duration.

### What the duration covers

The whole job, not just the last leg.

A turn where Claude used `AskUserQuestion` needs no special handling: the
answer comes back as a `tool_result` inside the *same* turn, so `Stop` never
fires mid-question and the measurement already spans the time you took to
answer.

A turn that ended because Claude asked something in *plain text* does fire
`Stop`, and your answer then begins a fresh turn. So `stop_hook.py` walks back
through those question/answer pairs: from the last prompt, if the assistant
text right before it ends in `?`, that prompt was an answer, so keep going
back. That is a heuristic, bounded by `MAX_QUESTION_HOPS` (6) and
`MAX_SPAN_HOURS` (12). Set `MAX_QUESTION_HOPS = 0` to disable it.

### Context percentage

Claude Code records neither the context window size nor the `[1m]` suffix on
the model id in the transcript - it logs plain `claude-opus-5` - so
`context_limit()` starts from the `model` in `~/.claude/settings.json` and
widens to 1M if the observed context already exceeds 200k.

This is the context window, **not** the five-hour session quota. That is not
reachable from a hook: Claude Code keeps no local copy, and `/usage` reads it
live from an endpoint that needs the OAuth token out of the login keychain and
sits behind Cloudflare.

---

## Tuning

Constants at the top of the sketch:

| Constant | Now | What it is |
|----------|-----|------------|
| `DEFAULT_BRIGHTNESS` | 1 | boot brightness, overridden by EEPROM |
| `AWAIT_MS` | 10s | idle beat interval |
| `HOLD_STATIC_MS` | 15s | how long the duration stays up |
| `BLINK_COUNT` / `_ON_MS` / `_OFF_MS` | 3 / 300 / 250 | the `DONE` flash |
| `CANCEL_BLINKS` / `_ON_MS` / `_OFF_MS` | 2 / 170 / 130 | the `ABORT` flash |
| `CANCEL_HOLD_MS` | 3s | how long `ABORT` stays up |
| `ASK_DWELL_MS` | 900ms | per word of `I NEED REPLY` |
| `LOAD_FRAME_MS` | 50ms | border animation speed |
| `HEART_FRAME_MS` | 25ms | ECG speed; one beat is 64 frames, so 1.6s |

`HARDWARE_TYPE` is `FC16_HW`; if text comes out mirrored or split across the
wrong modules, try `PAROLA_HW`, `GENERIC_HW` or `ICSTATION_HW`.

---

## Known limitations

- **One panel, one producer.** Two Claude Code sessions both write to it and
  the last event wins, so a session that finishes while another is still
  working will park `DONE` on the panel. Sends are serialised by the lock so
  nothing breaks, but the display can be wrong. Fixing it properly means the
  board keeping a per-session registry rather than taking one-shot commands.
- **macOS only, in practice.** See [Requirements](#requirements).
- **Ctrl+C detection is a transcript tail**, not an API. If Claude Code changes
  how it records interrupts, it stops working silently.

---

## Troubleshooting

- **Text mirrored, or split across the wrong modules** — change
  `HARDWARE_TYPE` at the top of the sketch.
- **Nothing at all** — `matrix -v "#N42s"` should echo `NOTIFY 1`. If that
  comes back the serial path is fine and the problem is wiring or power. If not, check `ls /dev/cu.usbserial*` and override with
  `CLAUDE_MATRIX_PORT`.
- **Dark right after plugging in** — expected for up to 10 seconds, until the
  first idle beat. Boot only prints `READY` on serial.
- **A stale report stays on screen** — the `UserPromptSubmit` hook is not
  firing. Check the paths in `~/.claude/settings.json` are absolute.
- **Port changed after replugging** — the CH340 enumerates as
  `/dev/cu.usbserial-1x0` and the number does change (seen going `-140` →
  `-130`). `send.py` globs for it so the hooks are unaffected, but an
  `arduino-cli upload -p …` needs the current name.
- **Upload fails with "not in sync"** — either you left off
  `:cpu=atmega328old`, or another process is holding the port (a serial
  monitor, or a `send.py` still in its wait window).
