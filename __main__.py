#!/usr/bin/env python3
"""Main entry point for SquishIt development launches."""

import sys
import os

# Add parent directory to path for development
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from video_compressor.__main__ import main

if __name__ == "__main__":
    main()