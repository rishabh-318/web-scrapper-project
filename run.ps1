Write-Host "Universal Website Scraper - Setup & Run"
Write-Host "=========================================="

Write-Host "Installing dependencies..."
pip install -r requirements.txt

Write-Host "Installing playwright chromium..."
playwright install chromium

Write-Host "Setup complete! Starting server..."
Write-Host "Open http://localhost:8000 in your browser"
Write-Host "Press Ctrl+C to stop the server"

uvicorn main:app --reload --host 0.0.0.0 --port 8000
