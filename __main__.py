#!/usr/bin/env python3
"""Main entry point for SquishIt development launches."""

import sys
import os

# Keep the project root importable when this file is launched directly.
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from video_compressor.__main__ import main

if __name__ == "__main__":
    main()