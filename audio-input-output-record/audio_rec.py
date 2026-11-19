#!/usr/bin/env python3
"""audio-rec entry point: `python audio_rec.py [command]` on Linux, macOS and
Windows (see README.md).  The code lives in the `audiorec` package."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from audiorec.cli import run  # noqa: E402

if __name__ == "__main__":
    run()
