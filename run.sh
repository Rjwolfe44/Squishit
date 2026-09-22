#!/bin/bash
# SquishIt - Quick Run Script for macOS/Linux
# Activates the virtual environment and starts the application

# Check if venv exists
if [ ! -d "venv" ]; then
    echo "[ERROR] Virtual environment not found!"
    echo "Please run setup.sh first to install the application."
    exit 1
fi

# Activate virtual environment
source venv/bin/activate

# Start the application
python -m squishit "$@"

deactivate