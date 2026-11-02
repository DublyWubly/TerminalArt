"""
termpaint.py - Python prototype of the renderer (the blue boxes in the diagram).

Written to be copied into NASM almost line by line:
  - integer math only (add, mul, div, shift)
  - the symbol ramp and strength histogram are plain tables
  - each function below becomes one NASM routine

Pipeline:
    load parsed file -> fetch terminal size -> compute cell grid
    -> average each block (pass 1) -> build strength histogram
    -> map strength to symbol + boosted color (pass 2) -> write
"""

import fcntl
import os
import select
import struct
import sys
import termios
from pathlib import Path

from preprocess import image_to_rgba, read_tpr

# Symbols from weakest to strongest. NASM: byte table in .data.
# No space: anything visible always gets a symbol in its color, so even the
# darkest parts of the image (like a dark red couch) still show up.
RAMP = ".:-=+*#%@"

# Blocks with average alpha below this are background and print as a space.
ALPHA_CUTOFF = 128

# Below this terminal size the art gets too small, so a zoom tip is shown.
TIP_COLS = 120
TIP_ROWS = 40
TIP = "Tip: zoom out (Ctrl+-) and run again for more detail.\n"


# Takes: nothing (asks the terminal that stdout is connected to).
# Gives: (cols, rows, x_pixels, y_pixels): the terminal size in characters,
#        plus its size in pixels (0, 0 if unknown). 80x24 if not a terminal.
def fetch_terminal_size():
    """Return (cols, rows, x_pixels, y_pixels) of the terminal.

    Same call the NASM version makes: ioctl(1, TIOCGWINSZ, &winsize), where
    winsize is four u16 values: rows, cols, x_pixels, y_pixels.
    Pixel sizes are 0 if the terminal doesn't report them.
    Falls back to 80x24 with no pixel info when output isn't a terminal.
    """
    # Takes: stdout. Gives: the four winsize numbers, or falls through on error.
    try:
        ws = fcntl.ioctl(1, termios.TIOCGWINSZ, bytes(8))
        rows, cols, xpix, ypix = struct.unpack("HHHH", ws)
        # Takes: the reported size. Gives: it back, only if it's a real (non-zero) size.
        if cols > 0 and rows > 0:
            return cols, rows, xpix, ypix
    # Takes: the error when stdout isn't a terminal. Gives: nothing, falls to 80x24.
    except OSError:
        pass
    return 80, 24, 0, 0


# Takes: nothing (talks to the terminal through /dev/tty).
# Gives: (width, height) of one character in pixels, from the terminal's
#        answer to ESC[16t, or (0, 0) if there's no terminal or no answer.
def query_char_size():
    """Ask the terminal directly for one character's size in pixels.

    Used when ioctl gives no pixel size. Sends ESC[16t; terminals that
    support it reply ESC[6;<height>;<width>t. Terminals that don't simply
    stay quiet, so we give up after 100 ms.
    NASM: open /dev/tty, ioctl TCGETS/TCSETS (turn off echo + line mode),
    write the query, poll with a timeout, read, parse digits, restore.
    Returns (width, height), or (0, 0) if there is no answer.
    """
    # Takes: the /dev/tty path. Gives: a file handle to the terminal, or (0, 0) if there is none.
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return 0, 0                 # no terminal at all (piped / cron / etc.)

    old = termios.tcgetattr(fd)
    reply = b""
    # Takes: the terminal handle. Gives: the terminal's raw reply bytes in `reply`;
    #        the terminal's settings are always restored afterwards (finally).
    try:
        new = termios.tcgetattr(fd)
        new[3] &= ~(termios.ICANON | termios.ECHO)   # read bytes as they come, don't echo them
        termios.tcsetattr(fd, termios.TCSANOW, new)
        os.write(fd, b"\x1b[16t")
        poller = select.poll()
        poller.register(fd, select.POLLIN)
        # Takes: bytes as the terminal sends them. Gives: the full reply (ends in 't'),
        #        or stops early after 100 ms of silence or 32 bytes.
        while not reply.endswith(b"t") and len(reply) < 32:
            # Takes: 100 ms of waiting. Gives: a stop if nothing arrived.
            if not poller.poll(100):                 # 100 ms with no answer: give up
                break
            reply += os.read(fd, 32)
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, old)  # always put the terminal back
        os.close(fd)

    # Expected: ESC [ 6 ; height ; width t
    start = reply.find(b"\x1b[6;")
    end = reply.find(b"t", start)
    # Takes: where the reply starts/ends. Gives: (0, 0) if it isn't there.
    if start < 0 or end < 0:
        return 0, 0
    parts = reply[start + 4:end].split(b";")
    # Takes: the "height;width" part. Gives: (0, 0) unless it's exactly two numbers.
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return 0, 0
    return int(parts[1]), int(parts[0])


# Takes: image size in pixels, terminal size in characters, and the shape of
#        one character (char_w x char_h, in pixels or just 1 x 2).
# Gives: (out_cols, out_rows): how many characters wide and tall the art is,
#        as big as fits while keeping the image's proportions on screen.
def compute_cell_grid(img_w, img_h, cols, rows, char_w, char_h):
    """How many characters wide and tall the art should be.

    char_w x char_h is the shape of one terminal character (in pixels, or
    just 1 x 2 when unknown). The art keeps the image's shape on screen:
        out_cols * char_w / (out_rows * char_h) = img_w / img_h
    Try using every row first; if that's too wide, use every column instead.
    Returns (out_cols, out_rows).
    """
    out_rows = rows
    out_cols = rows * img_w * char_h // (img_h * char_w)
    # Takes: the width that using every row would need. Gives: width/height
    #        recomputed from the column limit instead, if that's too wide.
    if out_cols > cols:
        out_cols = cols
        out_rows = cols * img_h * char_w // (img_w * char_h)
    # At least 1, and never more characters than pixels.
    out_cols = max(1, min(out_cols, img_w))
    out_rows = max(1, min(out_rows, img_h))
    return out_cols, out_rows


# Takes: the RGBA array and a rectangle of pixels (x0..x1-1, y0..y1-1).
# Gives: (r, g, b, a): the average of each channel over that rectangle,
#        as whole numbers 0-255. This is one character's worth of image.
def average_block(rgba, x0, x1, y0, y1):
    """Average R, G, B, A over pixels x0..x1-1, y0..y1-1 -> (r, g, b, a).

    NASM: two nested loops adding each byte into four sum registers,
    then one div per channel by the pixel count.
    """
    block = rgba[y0:y1, x0:x1]
    count = (x1 - x0) * (y1 - y0)
    r = int(block[:, :, 0].sum()) // count
    g = int(block[:, :, 1].sum()) // count
    b = int(block[:, :, 2].sum()) // count
    a = int(block[:, :, 3].sum()) // count
    return r, g, b, a


# Takes: one block's color (r, g, b), each 0-255.
# Gives: its strength 0-255, the biggest of the three channels. This decides
#        how dense the block's symbol is.
def strength(r, g, b):
    """The value a block's symbol is based on: its strongest channel, 0-255.

    max(r, g, b) instead of plain brightness, so a vivid color like bright
    red (220, 60, 40) counts as strong as white instead of medium.
    NASM: max of three bytes (two cmp/cmov).
    """
    return max(r, g, b)


# Takes: the list of every block's (r, g, b, a) average.
# Gives: (darker, total): darker[s] = how many visible blocks are weaker
#        than strength s (a 256-entry table), and total = visible blocks.
def build_darker_table(blocks):
    """Lighting relative to the whole image (histogram equalization).

    1. hist[l]   = how many visible blocks have strength l   (256 counters)
    2. darker[l] = how many visible blocks are weaker than l (running sum)

    A block's symbol then comes from darker[l] / total, i.e. what fraction
    of the image is weaker than it. So the weakest part of any image gets
    '.', the strongest gets '@', no matter how dark or bright it is.
    Returns (darker, total).
    """
    hist = [0] * 256
    # Takes: every block. Gives: hist[s] = how many visible blocks have strength s.
    for r, g, b, a in blocks:
        # Takes: the block's alpha. Gives: it's only counted if it isn't see-through.
        if a >= ALPHA_CUTOFF:
            hist[strength(r, g, b)] += 1

    darker = [0] * 256
    running = 0
    # Takes: hist, strength 0 up to 255. Gives: darker[l] = blocks weaker than l
    #        (running total of everything before l); `running` ends as the total.
    for l in range(256):
        darker[l] = running
        running += hist[l]
    return darker, running


# Takes: one block's color (r, g, b).
# Gives: the same color brightened (up to 2x for dark colors, about 1x for
#        bright ones) with the hue unchanged, so it shows on a dark terminal.
def boost_color(r, g, b):
    """Brighten dark colors so they show on a dark terminal, keeping their hue.

    gain = (510 - max) / 255   -> 2x for very dark colors, 1x for bright ones
    out  = color * gain
    The brightest channel can never pass 255 (max * (510 - max) <= 255 * 255),
    so the hue is kept exactly and no clamp is needed. Black stays black and
    tiny noise in dark areas (like (2, 1, 0) in a JPEG) stays tiny.
    NASM: max of three bytes, then per channel one mul and one div.
    """
    gain = 510 - max(r, g, b)
    return r * gain // 255, g * gain // 255, b * gain // 255


# Takes: one block's average (r, g, b, a) plus the darker table and total.
# Gives: the text for that one character: a space if it's see-through,
#        otherwise a color code (ESC[38;2;R;G;Bm) followed by one symbol.
def block_to_text(r, g, b, a, darker, total):
    """One block -> its output text.

    Background (see-through) -> plain space.
    Otherwise -> symbol by relative strength, in the boosted color:
    ESC[38;2;R;G;Bm followed by one ramp symbol.
    """
    # Takes: the block's alpha. Gives: a plain space for see-through background.
    if a < ALPHA_CUTOFF:
        return " "
    idx = darker[strength(r, g, b)] * len(RAMP) // total    # 0-8, total > 0 here
    r, g, b = boost_color(r, g, b)
    return f"\x1b[38;2;{r};{g};{b}m" + RAMP[idx]


# Takes: the RGBA array, how many columns/rows are free in the terminal,
#        and the shape of one character.
# Gives: the whole picture as one string: colored symbols, one line per
#        row, each line ending with a color reset and a newline.
def render(rgba, cols, rows, char_w=1, char_h=2):
    """Run the full pipeline on an RGBA array, return the output string."""
    h, w, _ = rgba.shape
    out_cols, out_rows = compute_cell_grid(w, h, cols, rows, char_w, char_h)

    # Pass 1: average every block into a flat list (NASM: array in .bss).
    # Block edges are spread evenly: column c covers x = c*w/out_cols up to
    # (c+1)*w/out_cols, so the whole image is used with no leftover strip.
    blocks = []
    # Takes: each output row. Gives: the pixel rows (y0..y1) that row covers.
    for row in range(out_rows):
        y0 = row * h // out_rows
        y1 = (row + 1) * h // out_rows
        # Takes: each output column. Gives: that block's average color, added to `blocks`.
        for col in range(out_cols):
            x0 = col * w // out_cols
            x1 = (col + 1) * w // out_cols
            blocks.append(average_block(rgba, x0, x1, y0, y1))

    darker, total = build_darker_table(blocks)

    # Pass 2: turn each block into text.
    out = ""
    i = 0
    # Takes: each output row. Gives: one finished line of text added to `out`.
    for row in range(out_rows):
        # Takes: each block in the row, in order. Gives: its colored symbol added to `out`.
        for col in range(out_cols):
            r, g, b, a = blocks[i]
            out += block_to_text(r, g, b, a, darker, total)
            i += 1
        out += "\x1b[0m\n"   # reset color at the end of every line
    return out


# Takes: the command line (an image or .tpr path) and the current terminal.
# Gives: the ASCII art printed to the terminal, plus a zoom tip on stderr
#        when the terminal is small.
def main():
    # Usage: python termpaint.py <image or .tpr>
    # Takes: the arg count. Gives: a usage message and exit if no file was given.
    if len(sys.argv) < 2:
        sys.exit("usage: python termpaint.py <image or .tpr>")
    path = Path(sys.argv[1])
    # Takes: the path. Gives: an error and exit if the file doesn't exist.
    if not path.is_file():
        sys.exit(f"error: no such file: {path}")

    # .tpr files load directly, any other image is preprocessed in memory.
    rgba = read_tpr(path) if path.suffix == ".tpr" else image_to_rgba(path)

    cols, rows, xpix, ypix = fetch_terminal_size()

    # Real character shape in pixels:
    #   1. from ioctl, if the terminal filled in its pixel size
    #   2. else ask the terminal with ESC[16t
    #   3. else assume characters are 2x taller than wide
    char_w, char_h = 1, 2
    # Takes: the pixel size from ioctl. Gives: one character's size, if it was reported.
    if xpix >= cols and ypix >= rows:
        char_w, char_h = xpix // cols, ypix // rows
    # Takes: whether output is a terminal. Gives: the size from asking ESC[16t instead.
    elif sys.stdout.isatty():
        qw, qh = query_char_size()
        # Takes: the terminal's answer. Gives: it's used only if it's a real size.
        if qw > 0 and qh > 0:
            char_w, char_h = qw, qh

    rows -= 1                       # keep one row free for the shell prompt
    small = cols < TIP_COLS or rows < TIP_ROWS
    # Takes: whether the terminal is small. Gives: one less row, to make room for the tip.
    if small:
        rows -= 1                   # and one more for the tip line

    # One write for the whole picture, like the planned NASM write syscall.
    sys.stdout.write(render(rgba, cols, max(rows, 1), char_w, char_h))
    sys.stdout.flush()              # make sure the art is out before the tip
    # Takes: whether the terminal is small. Gives: the zoom tip under the art.
    if small:
        sys.stderr.write(TIP)


# Takes: running this file directly. Gives: a call to main().
if __name__ == "__main__":
    main()
