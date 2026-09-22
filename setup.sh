#!/bin/bash
# SquishIt - macOS/Linux Setup Script
# This script creates a virtual environment, installs dependencies, and optionally starts the app.

set -e

echo "============================================"
echo "   SquishIt - macOS/Linux Setup"
echo "============================================"
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check if Python is installed
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}[ERROR] Python 3 is not installed.${NC}"
    echo "Please install Python 3.10 or later:"
    echo ""
    echo "  macOS: brew install python3"
    echo "  Ubuntu/Debian: sudo apt install python3 python3-venv python3-pip"
    echo "  Fedora: sudo dnf install python3 python3-pip"
    exit 1
fi

# Get Python version
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
echo "[INFO] Found Python $PYTHON_VERSION"

# Check Python version is 3.10+
PYTHON_MAJOR=$(echo $PYTHON_VERSION | cut -d. -f1)
PYTHON_MINOR=$(echo $PYTHON_VERSION | cut -d. -f2)

if [ "$PYTHON_MAJOR" -lt 3 ] || ([ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 10 ]); then
    echo -e "${RED}[ERROR] Python 3.10 or later is required.${NC}"
    echo "Your version: $PYTHON_VERSION"
    exit 1
fi

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "[INFO] Creating virtual environment..."
    python3 -m venv venv
    echo -e "${GREEN}[OK] Virtual environment created.${NC}"
else
    echo -e "${GREEN}[OK] Virtual environment already exists.${NC}"
fi

# Activate virtual environment
echo "[INFO] Activating virtual environment..."
source venv/bin/activate

# Upgrade pip
echo "[INFO] Upgrading pip..."
pip install --upgrade pip --quiet

# Install dependencies
echo "[INFO] Installing dependencies..."
pip install -r requirements.txt --quiet
echo -e "${GREEN}[OK] Dependencies installed.${NC}"

# Check for FFmpeg
echo ""
echo "[INFO] Checking for FFmpeg..."
if ! command -v ffmpeg &> /dev/null; then
    echo -e "${YELLOW}[WARNING] FFmpeg is not installed.${NC}"
    echo ""
    echo "FFmpeg is required for video compression. Please install it:"
    echo ""
    echo "  macOS: brew install ffmpeg"
    echo "  Ubuntu/Debian: sudo apt install ffmpeg"
    echo "  Fedora: sudo dnf install ffmpeg"
    echo "  Arch Linux: sudo pacman -S ffmpeg"
    echo ""
    echo "After installing FFmpeg, run this script again."
    deactivate
    exit 1
else
    echo -e "${GREEN}[OK] FFmpeg is installed.${NC}"
fi

echo ""
echo "============================================"
echo "   Setup Complete!"
echo "============================================"
echo ""
echo "To run the application:"
echo "  1. Activate the virtual environment:"
echo "     source venv/bin/activate"
echo "  2. Run the app:"
echo "     python -m squishit"
echo ""
echo "Or use the provided run.sh script."
echo ""

# Ask if user wants to start the app
read -p "Start SquishIt now? (Y/n): " START_APP
if [[ ! "$START_APP" =~ ^[Nn]$ ]]; then
    echo "[INFO] Starting SquishIt..."
    python -m squishit
fi

deactivate