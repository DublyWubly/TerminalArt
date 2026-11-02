"""
preprocess.py - "Process the photo" step from the system diagram.

Converts any image Pillow can open into a raw .tpr file that the renderer can read without any parsing library.

.tpr layout (little-endian):
    offset 0  : 4 bytes   magic b"TPR2"
    offset 4  : uint32    width
    offset 8  : uint32    height
    offset 12 : width*height*4 bytes, row by row, each pixel = R, G, B, A
                (4 bytes per pixel so NASM can load one pixel as one dword)
"""

import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

MAGIC = b"TPR2"
HEADER = struct.Struct("<4sII")  # magic, width, height = 12 bytes


# Takes: the file path of an image (png, jpg, ...).
# Gives: a numpy array of 8-bit ints shaped (height, width, 4): one R, G, B, A
#        value per pixel, rows top to bottom, pixels left to right, so every
#        4 numbers in a row are the next pixel. Phone photos come out upright.
def image_to_rgba(path):
    """Open an image and return a (height, width, 4) uint8 RGBA array.

    Phone photos are stored sideways with an EXIF "rotate me" tag;
    exif_transpose applies it so the picture isn't rotated.
    """
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGBA")
    return np.asarray(img, dtype=np.uint8)


# Takes: an RGBA array from image_to_rgba and the path to save to.
# Gives: a .tpr file on disk: 12-byte header (magic, width, height), then
#        every pixel's R, G, B, A bytes in order. Returns nothing.
def write_tpr(rgba, path):
    """Save an RGBA array as a .tpr file."""
    h, w, _ = rgba.shape
    # Takes: the open output file. Gives: header bytes, then all pixel bytes, written to it.
    with open(path, "wb") as f:
        f.write(HEADER.pack(MAGIC, w, h))
        f.write(np.ascontiguousarray(rgba).tobytes())


# Takes: the path of a .tpr file.
# Gives: the same (height, width, 4) RGBA array that was saved, or raises
#        ValueError if the file is too short, isn't a .tpr, or is cut off.
def read_tpr(path):
    """Load a .tpr file and check it isn't broken ("Load parsed file")."""
    data = Path(path).read_bytes()
    # Takes: the file's byte count. Gives: an error if it can't even hold a header.
    if len(data) < HEADER.size:
        raise ValueError("file too small to be a .tpr")
    magic, w, h = HEADER.unpack_from(data)
    # Takes: the first 4 bytes. Gives: an error if they aren't "TPR2".
    if magic != MAGIC:
        raise ValueError("not a .tpr file (bad magic)")
    # Takes: the file size and the header's width/height. Gives: an error if they don't agree.
    if len(data) != HEADER.size + w * h * 4:
        raise ValueError("file size doesn't match header")
    return np.frombuffer(data, dtype=np.uint8, offset=HEADER.size).reshape(h, w, 4)


# Takes: command line args: an image path and an optional output path.
# Gives: the .tpr file on disk (default: same name as the image, .tpr ending)
#        and a one-line message with where it went and the image size.
if __name__ == "__main__":
    # Usage: python preprocess.py <image> [output.tpr]
    # Takes: the arg count. Gives: a usage message and exit if no image was given.
    if len(sys.argv) < 2:
        sys.exit("usage: python preprocess.py <image> [output.tpr]")
    src = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(".tpr")
    rgba = image_to_rgba(src)
    write_tpr(rgba, out)
    print(f"wrote {out} ({rgba.shape[1]}x{rgba.shape[0]})")
