#!/usr/bin/env python3
"""Send a line of text to the ClaudeMatrix display over USB serial.

Uses only the standard library (termios), so there is no pyserial dependency.
Port is picked from $CLAUDE_MATRIX_PORT, else the first /dev/cu.usbserial*.

Opening the port asserts DTR, which resets the Nano, so nothing can be written
until the sketch has booted. Rather than sleep a fixed amount we wait for the
sketch's READY banner, which lands about 1.5s after open. A file lock keeps two
senders (say a Stop hook and the next UserPromptSubmit) off the port at once.
"""
import argparse
import fcntl
import glob
import os
import re
import sys
import termios
import time
import unicodedata

BAUD = termios.B115200
PORT_GLOB = "/dev/cu.usbserial*"
LOCK_PATH = "/tmp/claude-code-matrix.lock"
MAX_LEN = 160

# The MAX7219 font is plain ASCII, so fold Turkish and typographic characters.
FOLD = str.maketrans({
    "ş": "s", "Ş": "S", "ı": "i", "İ": "I", "ğ": "g", "Ğ": "G",
    "ü": "u", "Ü": "U", "ö": "o", "Ö": "O", "ç": "c", "Ç": "C",
    "“": '"', "”": '"', "‘": "'", "’": "'",
    "–": "-", "—": "-", "…": "...", " ": " ",
})


def find_port():
    env = os.environ.get("CLAUDE_MATRIX_PORT")
    if env:
        return env
    ports = sorted(glob.glob(PORT_GLOB))
    return ports[0] if ports else None


def to_ascii(text):
    text = text.translate(FOLD)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if 32 <= ord(c) < 127)
    return re.sub(r"\s+", " ", text).strip()


def acquire_lock(timeout):
    fh = open(LOCK_PATH, "w")
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            if time.monotonic() >= deadline:
                fh.close()
                return None
            time.sleep(0.05)


def open_port(port):
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    attrs = termios.tcgetattr(fd)
    attrs[0] = 0  # iflag: no input translation
    attrs[1] = 0  # oflag: no output post-processing
    # CLOCAL ignores modem lines; HUPCL left unset so closing does not drop DTR
    # and reset the board mid-animation.
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    attrs[3] = 0  # lflag: raw, no echo
    attrs[4] = attrs[5] = BAUD
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    return fd


def read_for(fd, seconds):
    """Drain whatever the board says for a while (diagnostics only)."""
    out = b""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            chunk = os.read(fd, 256)
        except BlockingIOError:
            chunk = b""
        if chunk:
            out += chunk
        else:
            time.sleep(0.02)
    return out.decode("ascii", "replace")


def wait_ready(fd, timeout):
    """Wait for the boot banner the DTR reset produces. True if it arrived."""
    deadline = time.monotonic() + timeout
    buf = b""
    while time.monotonic() < deadline:
        try:
            chunk = os.read(fd, 256)
        except BlockingIOError:
            chunk = b""
        if chunk:
            buf += chunk
            if b"READY" in buf:
                return True
        else:
            time.sleep(0.01)
    return False


def main():
    ap = argparse.ArgumentParser(description="Send text to the LED matrix.")
    ap.add_argument("text", nargs="*", help="text to scroll (or a #command)")
    ap.add_argument("-p", "--port", default=None)
    ap.add_argument("-w", "--wait", type=float, default=2.5,
                    help="seconds to wait for the boot banner after opening "
                         "the port (default 2.5)")
    ap.add_argument("-l", "--lock-wait", type=float, default=8.0,
                    help="seconds to wait for another sender to finish")
    ap.add_argument("-v", "--verify", action="store_true",
                    help="print what the board echoes back")
    args = ap.parse_args()

    text = " ".join(args.text) if args.text else sys.stdin.read()
    line = text.strip() if text.startswith("#") else to_ascii(text)[:MAX_LEN]
    if not line:
        return 0

    port = args.port or find_port()
    if not port:
        print("claude-code-matrix: no serial port found (%s)" % PORT_GLOB,
              file=sys.stderr)
        return 1

    lock = acquire_lock(args.lock_wait)
    if lock is None:
        print("claude-code-matrix: another sender is holding %s" % LOCK_PATH,
              file=sys.stderr)
        return 1

    try:
        try:
            fd = open_port(port)
        except OSError as e:
            print("claude-code-matrix: cannot open %s: %s" % (port, e),
                  file=sys.stderr)
            return 1
        try:
            if args.wait > 0:
                wait_ready(fd, args.wait)
            os.write(fd, (line + "\n").encode("ascii", "ignore"))
            termios.tcdrain(fd)
            if args.verify:
                print(read_for(fd, 1.0), end="")
        finally:
            os.close(fd)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
