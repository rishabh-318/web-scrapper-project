#!/bin/bash

echo "🚀 Universal Website Scraper - Setup & Run"
echo "=========================================="

# Check Python version
python_version=$(python3 --version 2>&1 | grep -oP '\d+\.\d+')
if (( $(echo "$python_version < 3.10" | bc -l) )); then
    echo "❌ Python 3.10+ required. Current version: $python_version"
    exit 1
fi

echo "✓ Python $python_version detected"

# Install dependencies
echo ""
echo "📦 Installing dependencies..."
pip install -r requirements.txt

# Install Playwright browsers
echo ""
echo "🌐 Installing Playwright Chromium..."
playwright install chromium

# Run server
echo ""
echo "🎉 Setup complete! Starting server..."
echo ""
echo "Open http://localhost:8000 in your browser"
echo "Press Ctrl+C to stop the server"
echo ""

uvicorn main:app --reload --host 0.0.0.0 --port 8000