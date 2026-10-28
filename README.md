# TermPaint
Display any image as ASCII art right in your terminal.

## The Problem
My terminal is boring. I want to point it at an image and see it drawn in text.

## How It Works
![System diagram](docs/system-diagram.png)

1. Python converts the image to a raw brightness file.
2. The NASM program loads that file and reads the terminal size.
3. It splits the image into blocks that fit the terminal and averages each one.
4. Each block's brightness picks a character, from ` ` (dark) to `@` (bright).
5. The result is printed to the terminal.

The full plan is in [docs/PLAN.md](docs/PLAN.md).

## Tech Stack
| Part | Choice |
|---|---|
| Languages | x86-64 NASM, Python 3.12 |
| Libraries | Pillow, NumPy, pytest |
| Tools | nasm, ld (binutils) |
| Data storage | Raw `.tpr` brightness file on disk |

## Setup
```bash
git clone ________ TermPaint
cd TermPaint
```
With Conda:
```bash
conda env create -f environment.yml
conda activate termpaint
```
Without Conda (install `nasm` and `binutils` with your package manager):
```bash
pip install -r requirements.txt
```
Run: `________`
Example input / output: `________`

## Roadmap
- [x] Plan scope and priorities
- [x] Design the system diagram
- [x] Set up the environment
- [ ] Python prototype
- [ ] NASM renderer
- [ ] Tests
- [ ] Extras (color, video, presets)
