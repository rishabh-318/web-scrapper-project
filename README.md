# Universal Website Scraper MVP

A full-stack web scraper that extracts structured, section-aware JSON from any website, handling both static and JavaScript-rendered pages with intelligent fallback strategies.

## 🚀 Quick Start (One Command)

```bash
# Install dependencies
pip install -r requirements.txt
playwright install chromium

# Run the server
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Then open http://localhost:8000 in your browser.

## ✅ Testing URLs

I tested the scraper with these three URLs demonstrating different capabilities:

### 1. **Wikipedia - Artificial Intelligence** (Static Content)

```
https://en.wikipedia.org/wiki/Artificial_intelligence
```

**Demonstrates:** Static scraping with httpx+Selectolax, semantic section extraction (header, nav, main, footer), comprehensive content parsing (headings, links, images, tables), article structure with multiple sections.

### 2. **Hacker News** (Pagination)

```
https://news.ycombinator.com/
```

**Demonstrates:** Pagination depth ≥3 via "More" link following, multiple page loads tracked in `interactions.pages[]`, static scraping with link traversal, simple DOM structure.

### 3. **Unsplash** (JS-Rendered + Infinite Scroll)

```
https://unsplash.com/
```

**Demonstrates:** JS rendering detection (Playwright fallback), infinite scroll interactions (3+ scroll loads) tracked in `interactions.scrolls`, handling of dynamic content that requires browser automation, image-heavy website scraping.

## 🏗️ Architecture

### Backend Stack

- **FastAPI** - API server with automatic docs
- **httpx + Selectolax** - Fast static HTML parsing (first attempt)
- **Playwright** - Browser automation for JS-rendered pages (fallback)
- **Python 3.10+** - Modern Python features

### Frontend

- **Jinja2 Template** - Single HTML page served by FastAPI
- **Vanilla JavaScript** - No build tools, no npm, no CORS issues
- **Accordion UI** - Clean section browsing with JSON preview

## 📋 Features

✅ **Dual Scraping Strategy**

- Attempts static scraping first (faster, lighter)
- Falls back to Playwright when needed (JS-heavy sites)

✅ **Section-Aware Parsing**

- Extracts semantic sections (hero, nav, main, footer, etc.)
- Groups content by landmarks and headings
- Generates fallback labels from text content

✅ **Click & Scroll Interactions**

- Detects and clicks tabs, "Load more" buttons
- Handles infinite scroll (3+ loads)
- Follows pagination links across 3+ pages

✅ **Content Extraction**

- Headings (h1-h6)
- Links (absolute URLs, same-domain only)
- Images (with alt text)
- Lists and tables
- Clean text with normalized whitespace

✅ **Smart URL Handling**

- Removes tracking parameters (utm\_\*, fbclid, etc.)
- Makes all URLs absolute
- Enforces same-domain constraint

✅ **Error Handling**

- Graceful failures with detailed error messages
- Timeout protection
- Content size limits

## 🎯 API Endpoints

### `POST /scrape`

Scrape a website and return structured JSON.

**Request:**

```json
{
  "url": "https://example.com"
}
```

**Response:**

```json
{
  "result": {
    "url": "https://example.com",
    "scrapedAt": "2025-11-24T00:00:00Z",
    "meta": { ... },
    "sections": [ ... ],
    "interactions": { ... },
    "errors": []
  },
  "method": "static"
}
```

### `GET /healthz`

Health check endpoint.

### `GET /`

Frontend UI for interactive scraping.

## 🔧 Configuration

Key constants in `main.py`:

```python
STATIC_THRESHOLD = 200       # Min text length for static scraping
TIMEOUT_MS = 30000           # Max page load time (30s)
MAX_CONTENT_SIZE = 10MB      # Content size limit
SCROLL_WAIT_MS = 2000        # Wait between scrolls
MAX_SCROLLS = 3              # Max scroll/pagination depth
```

## 🚫 Limitations

1. **Single-domain only** - Cross-origin links are ignored
2. **No authentication** - Cannot scrape behind login walls
3. **No file:// URLs** - Only http(s) supported
4. **Rate limiting** - Some sites may block automated requests
5. **Dynamic content** - Some heavily JS-dependent sites may not render fully
6. **Content size** - Large pages (>10MB) are rejected
7. **Timeout** - Pages taking >30s to load will fail

## 🛠️ Setup & Assistance Disclosure

### Development Environment

- **Editor:** Claude.ai (Sonnet 4.5) web interface
- **AI Assistance:** 100% of code generated via Claude
- **Human Input:** Architecture decisions, requirements interpretation, testing strategy

### Key Libraries

- `fastapi==0.104.1` - Modern async web framework
- `playwright==1.40.0` - Browser automation
- `selectolax==0.3.17` - Fast HTML parsing
- `httpx==0.25.1` - Async HTTP client
- `pydantic==2.5.0` - Data validation

### Where AI Was Used

1. **Complete code generation** - All Python and HTML/CSS/JS
2. **Architecture design** - Static-first fallback strategy
3. **Heuristics implementation** - Section detection, click/scroll logic
4. **Error handling** - Timeout guards, graceful failures
5. **UI design** - Responsive accordion interface
6. **Windows compatibility** - Event loop policy fixes
7. **Logging system** - Comprehensive debugging support

## 📁 Project Structure

```
├── main.py              # FastAPI server + scraping logic
├── requirements.txt     # Python dependencies
├── test_scraper.py      # Automated test script
├── README.md           # This file
├── DesignNotes.md      # Technical decisions
└── .env.example        # Environment variables (optional)
```

## 🧪 Testing

### Automated Tests

```bash
python test_scraper.py
```

### Manual Testing with curl

```bash
# Test scraping
curl -X POST http://localhost:8000/scrape \
  -H "Content-Type: application/json" \
  -d '{"url": "https://en.wikipedia.org/wiki/Python"}'

# Test health check
curl http://localhost:8000/healthz
```

## 🎨 Frontend Features

- **URL Input** - Enter any http(s) URL
- **Status Display** - Loading, success, error states with visual feedback
- **Meta Info** - Page title, language, scrape timestamp
- **Accordion Sections** - Expandable section previews
- **JSON Download** - Full scrape or individual sections
- **Responsive Design** - Works on desktop and mobile
- **Keyboard Support** - Press Enter to submit

## 🐛 Troubleshooting

**"Playwright not installed"**

```bash
playwright install chromium
```

**"Module not found"**

```bash
pip install -r requirements.txt
```

**"Address already in use"**

```bash
# Use a different port
uvicorn main:app --port 8001
```

**"NotImplementedError" on Windows**

- Already fixed with `WindowsProactorEventLoopPolicy` in code
- Restart the server if you still see this

## 📊 Performance

- **Static scraping:** ~1-3 seconds per page
- **Playwright scraping:** ~5-15 seconds per page
- **With pagination (3 pages):** ~15-30 seconds
- **With infinite scroll (3 loads):** ~10-20 seconds

## 🔒 Security

- URL scheme validation (http/https only)
- Same-domain enforcement
- Content size limits
- Timeout protection
- No code execution from scraped content

## 📝 License

MIT License - Feel free to use and modify.

---

**Built for Lyftr AI Full-Stack Assignment** 🚀

For questions or issues, please contact via the assignment submission email.
