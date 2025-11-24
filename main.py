"""
Universal Website Scraper - FastAPI Server
"""
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl
from datetime import datetime
import httpx
from selectolax.parser import HTMLParser
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse
import re
from typing import List, Dict, Any, Optional
import time

app = FastAPI(title="Universal Website Scraper")

# --- Models ---
class ScrapeRequest(BaseModel):
    url: HttpUrl

# --- Configuration ---
STATIC_THRESHOLD = 200  # Min text length to consider static scraping successful
TIMEOUT_MS = 30000
MAX_CONTENT_SIZE = 10 * 1024 * 1024  # 10MB
SCROLL_WAIT_MS = 2000
MAX_SCROLLS = 3
NOISE_SELECTORS = [
    '[id*="cookie"]', '[class*="cookie"]',
    '[id*="newsletter"]', '[class*="newsletter"]',
    '[class*="popup"]', '[class*="modal"]'
]

# --- Utility Functions ---
def clean_url(url: str, base_url: str) -> str:
    """Make URL absolute and remove tracking parameters"""
    abs_url = urljoin(base_url, url)
    parsed = urlparse(abs_url)
    
    # Remove tracking params
    if parsed.query:
        params = parse_qs(parsed.query)
        clean_params = {k: v for k, v in params.items() 
                       if not k.startswith(('utm_', 'fbclid', 'gclid', 'ref_'))}
        clean_query = urlencode(clean_params, doseq=True)
        parsed = parsed._replace(query=clean_query)
    
    return urlunparse(parsed)

def is_same_domain(url1: str, url2: str) -> bool:
    """Check if two URLs are from the same domain"""
    return urlparse(url1).netloc == urlparse(url2).netloc

def detect_language(html: str) -> str:
    """Simple language detection from HTML"""
    lang_match = re.search(r'<html[^>]*\slang=["\']([^"\']+)["\']', html)
    if lang_match:
        return lang_match.group(1)
    return "en"

def truncate_html(html: str, max_bytes: int = 5000) -> tuple[str, bool]:
    """Safely truncate HTML to max bytes"""
    if len(html.encode('utf-8')) <= max_bytes:
        return html, False
    
    # Truncate at character boundary
    truncated = html[:max_bytes]
    return truncated + "...", True

def normalize_text(text: str) -> str:
    """Normalize whitespace in text"""
    return re.sub(r'\s+', ' ', text).strip()

# --- Static Scraping ---
def scrape_static(url: str) -> Optional[Dict[str, Any]]:
    """Attempt static scraping with httpx + Selectolax"""
    try:
        with httpx.Client(follow_redirects=True, timeout=10.0) as client:
            response = client.get(url, headers={
                'User-Agent': 'Mozilla/5.0 (compatible; LyftrScraper/1.0)'
            })
            response.raise_for_status()
            
            if len(response.content) > MAX_CONTENT_SIZE:
                return None
            
            html = response.text
            tree = HTMLParser(html)
            
            # Check if we have sufficient content
            main_text = tree.body.text() if tree.body else ""
            main_text_clean = normalize_text(main_text)
            
            # Fallback condition: insufficient content or missing main element
            if len(main_text_clean) < STATIC_THRESHOLD or not tree.css_first('main, [role="main"], article'):
                return None
            
            return parse_html_content(tree, url, html)
    except Exception:
        return None

def parse_html_content(tree: HTMLParser, base_url: str, raw_html: str) -> Dict[str, Any]:
    """Parse HTML tree into structured JSON"""
    
    # Extract meta information
    title_node = tree.css_first('title')
    title = normalize_text(title_node.text()) if title_node else ""
    
    desc_node = tree.css_first('meta[name="description"]')
    description = desc_node.attributes.get('content', '') if desc_node else ""
    
    canonical_node = tree.css_first('link[rel="canonical"]')
    canonical = canonical_node.attributes.get('href', base_url) if canonical_node else base_url
    
    language = detect_language(raw_html)
    
    meta = {
        "title": title,
        "description": description,
        "language": language,
        "canonical": clean_url(canonical, base_url)
    }
    
    # Extract sections
    sections = []
    section_id = 0
    
    # Look for semantic sections
    for selector in ['header', 'nav', 'main', 'article', 'section', 'aside', 'footer']:
        elements = tree.css(selector)
        for elem in elements:
            section_data = extract_section(elem, base_url, section_id, selector)
            if section_data:
                sections.append(section_data)
                section_id += 1
    
    # If no sections found, treat body as one section
    if not sections and tree.body:
        section_data = extract_section(tree.body, base_url, 0, "unknown")
        if section_data:
            sections.append(section_data)
    
    return {
        "url": base_url,
        "scrapedAt": datetime.utcnow().isoformat() + "Z",
        "meta": meta,
        "sections": sections,
        "interactions": {
            "clicks": [],
            "scrolls": 0,
            "pages": [base_url]
        },
        "errors": []
    }

def extract_section(elem, base_url: str, section_id: int, tag_type: str) -> Optional[Dict[str, Any]]:
    """Extract structured content from a section element"""
    
    # Determine section type
    section_type = infer_section_type(elem, tag_type)
    
    # Extract headings
    headings = []
    for h in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
        for heading in elem.css(h):
            text = normalize_text(heading.text())
            if text:
                headings.append(text)
    
    # Extract text content
    text_content = normalize_text(elem.text())
    if len(text_content) > 500:
        text_content = text_content[:500] + "..."
    
    # Extract links
    links = []
    for link in elem.css('a[href]'):
        href = link.attributes.get('href', '')
        if href and not href.startswith(('#', 'javascript:', 'mailto:')):
            abs_href = clean_url(href, base_url)
            if is_same_domain(base_url, abs_href):
                links.append({
                    "text": normalize_text(link.text()),
                    "href": abs_href
                })
    
    # Extract images
    images = []
    for img in elem.css('img[src]'):
        src = img.attributes.get('src', '')
        if src:
            images.append({
                "src": clean_url(src, base_url),
                "alt": img.attributes.get('alt', '')
            })
    
    # Extract lists
    lists = []
    for ul in elem.css('ul, ol'):
        items = [normalize_text(li.text()) for li in ul.css('li')]
        if items:
            lists.append(items)
    
    # Extract tables
    tables = []
    for table in elem.css('table'):
        rows = []
        for tr in table.css('tr'):
            cells = [normalize_text(td.text()) for td in tr.css('td, th')]
            if cells:
                rows.append(cells)
        if rows:
            tables.append(rows)
    
    # Generate label
    label = headings[0] if headings else generate_fallback_label(text_content, section_type)
    
    # Get raw HTML
    raw_html = elem.html
    truncated_html, is_truncated = truncate_html(raw_html, 3000)
    
    return {
        "id": f"{section_type}-{section_id}",
        "type": section_type,
        "label": label,
        "sourceUrl": base_url,
        "content": {
            "headings": headings,
            "text": text_content,
            "links": links[:10],  # Limit links
            "images": images[:5],  # Limit images
            "lists": lists[:3],  # Limit lists
            "tables": tables[:2]  # Limit tables
        },
        "rawHtml": truncated_html,
        "truncated": is_truncated
    }

def infer_section_type(elem, tag_type: str) -> str:
    """Infer section type from element attributes and content"""
    html = elem.html.lower()
    
    if tag_type == 'header' or 'hero' in html or 'banner' in html:
        return 'hero'
    elif tag_type == 'nav':
        return 'nav'
    elif tag_type == 'footer':
        return 'footer'
    elif 'faq' in html or 'question' in html:
        return 'faq'
    elif 'pricing' in html or 'price' in html:
        return 'pricing'
    elif 'grid' in html or 'cards' in html:
        return 'grid'
    elif 'list' in html:
        return 'list'
    else:
        return tag_type if tag_type in ['section', 'article', 'aside'] else 'section'

def generate_fallback_label(text: str, section_type: str) -> str:
    """Generate fallback label from text content"""
    words = text.split()[:7]
    label = ' '.join(words)
    if len(label) > 50:
        label = label[:50] + "..."
    return label or f"Unlabeled {section_type}"

# --- Playwright Scraping ---
def scrape_with_playwright(url: str) -> Dict[str, Any]:
    """Scrape using Playwright for JS-rendered content"""
    errors = []
    interactions = {
        "clicks": [],
        "scrolls": 0,
        "pages": [url]
    }
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        try:
            # Navigate to page
            page.goto(url, wait_until='domcontentloaded', timeout=TIMEOUT_MS)
            page.wait_for_timeout(2000)  # Wait for JS to render
            
            # Remove noise elements
            for selector in NOISE_SELECTORS:
                try:
                    page.evaluate(f'document.querySelectorAll("{selector}").forEach(el => el.remove())')
                except:
                    pass
            
            # Try click interactions (tabs, load more buttons)
            click_attempted = attempt_clicks(page, interactions)
            
            # Try scroll/pagination
            scroll_attempted = attempt_scrolls(page, interactions, url)
            
            # Get final HTML
            html = page.content()
            tree = HTMLParser(html)
            
            result = parse_html_content(tree, url, html)
            result["interactions"] = interactions
            result["errors"] = errors
            
            browser.close()
            return result
            
        except PlaywrightTimeout as e:
            errors.append({"message": f"Timeout: {str(e)}", "phase": "render"})
            browser.close()
            raise HTTPException(status_code=408, detail="Page load timeout")
        except Exception as e:
            errors.append({"message": str(e), "phase": "scraping"})
            browser.close()
            raise HTTPException(status_code=500, detail=f"Scraping failed: {str(e)}")

def attempt_clicks(page, interactions: Dict) -> bool:
    """Attempt to click tabs or 'Load more' buttons"""
    click_selectors = [
        'button:has-text("Load more")',
        'button:has-text("Show more")',
        'a:has-text("Load more")',
        '[role="tab"]',
        '[data-testid*="more"]',
        '.load-more',
        '#load-more'
    ]
    
    clicked = False
    for selector in click_selectors:
        try:
            elements = page.query_selector_all(selector)
            for elem in elements[:3]:  # Limit to 3 clicks
                if elem.is_visible():
                    elem.click()
                    page.wait_for_timeout(SCROLL_WAIT_MS)
                    interactions["clicks"].append(selector)
                    clicked = True
        except:
            continue
    
    return clicked

def attempt_scrolls(page, interactions: Dict, base_url: str) -> bool:
    """Attempt infinite scroll or pagination"""
    scrolled = False
    
    # Try infinite scroll
    for i in range(MAX_SCROLLS):
        prev_height = page.evaluate('document.body.scrollHeight')
        page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
        page.wait_for_timeout(SCROLL_WAIT_MS)
        new_height = page.evaluate('document.body.scrollHeight')
        
        if new_height > prev_height:
            interactions["scrolls"] += 1
            scrolled = True
        else:
            break
    
    # Try pagination links
    if not scrolled:
        pagination_selectors = ['a[rel="next"]', '.next', '.pagination a:has-text("Next")', 'a:has-text("›")']
        for selector in pagination_selectors:
            for i in range(MAX_SCROLLS):
                try:
                    next_link = page.query_selector(selector)
                    if next_link and next_link.is_visible():
                        href = next_link.get_attribute('href')
                        if href:
                            full_url = urljoin(base_url, href)
                            if is_same_domain(base_url, full_url):
                                page.goto(full_url, wait_until='domcontentloaded', timeout=TIMEOUT_MS)
                                page.wait_for_timeout(2000)
                                interactions["pages"].append(full_url)
                                scrolled = True
                    else:
                        break
                except:
                    break
    
    return scrolled

# --- API Endpoints ---
@app.post("/scrape")
async def scrape_url(request: ScrapeRequest):
    """Main scraping endpoint"""
    url = str(request.url)
    
    # Validate URL scheme
    if not url.startswith(('http://', 'https://')):
        raise HTTPException(status_code=400, detail="Only http(s) URLs are supported")
    
    # Try static scraping first
    result = scrape_static(url)
    
    if result:
        return {"result": result, "method": "static"}
    
    # Fallback to Playwright
    result = scrape_with_playwright(url)
    return {"result": result, "method": "playwright"}

@app.get("/healthz")
async def health_check():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}

@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve frontend"""
    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Universal Website Scraper</title>
        <style>
            * { box-sizing: border-box; margin: 0; padding: 0; }
            body { 
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding: 20px;
            }
            .container {
                max-width: 1200px;
                margin: 0 auto;
                background: white;
                border-radius: 16px;
                box-shadow: 0 20px 60px rgba(0,0,0,0.3);
                overflow: hidden;
            }
            .header {
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                padding: 40px;
                text-align: center;
            }
            .header h1 { font-size: 2.5em; margin-bottom: 10px; }
            .header p { opacity: 0.9; font-size: 1.1em; }
            .content { padding: 40px; }
            .input-group {
                display: flex;
                gap: 10px;
                margin-bottom: 30px;
            }
            input[type="url"] {
                flex: 1;
                padding: 15px;
                border: 2px solid #e0e0e0;
                border-radius: 8px;
                font-size: 16px;
                transition: border 0.3s;
            }
            input[type="url"]:focus {
                outline: none;
                border-color: #667eea;
            }
            button {
                padding: 15px 40px;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                border: none;
                border-radius: 8px;
                font-size: 16px;
                font-weight: 600;
                cursor: pointer;
                transition: transform 0.2s, box-shadow 0.2s;
            }
            button:hover {
                transform: translateY(-2px);
                box-shadow: 0 5px 15px rgba(102, 126, 234, 0.4);
            }
            button:active { transform: translateY(0); }
            button:disabled {
                opacity: 0.6;
                cursor: not-allowed;
                transform: none;
            }
            .status {
                padding: 15px;
                border-radius: 8px;
                margin-bottom: 20px;
                display: none;
            }
            .status.loading {
                background: #e3f2fd;
                color: #1976d2;
                display: block;
            }
            .status.success {
                background: #e8f5e9;
                color: #2e7d32;
                display: block;
            }
            .status.error {
                background: #ffebee;
                color: #c62828;
                display: block;
            }
            .accordion {
                margin-top: 20px;
            }
            .accordion-item {
                border: 1px solid #e0e0e0;
                border-radius: 8px;
                margin-bottom: 10px;
                overflow: hidden;
            }
            .accordion-header {
                padding: 15px 20px;
                background: #f5f5f5;
                cursor: pointer;
                display: flex;
                justify-content: space-between;
                align-items: center;
                transition: background 0.3s;
            }
            .accordion-header:hover { background: #eeeeee; }
            .accordion-header.active { background: #667eea; color: white; }
            .accordion-content {
                padding: 20px;
                display: none;
                background: #fafafa;
            }
            .accordion-content.active { display: block; }
            pre {
                background: #263238;
                color: #aed581;
                padding: 20px;
                border-radius: 8px;
                overflow-x: auto;
                font-size: 13px;
                line-height: 1.5;
            }
            .download-btn {
                margin-top: 15px;
                padding: 10px 20px;
                font-size: 14px;
            }
            .meta-info {
                background: #f0f4ff;
                padding: 20px;
                border-radius: 8px;
                margin-bottom: 20px;
            }
            .meta-info h3 { color: #667eea; margin-bottom: 15px; }
            .meta-info p { margin: 8px 0; color: #555; }
            .loader {
                border: 3px solid #f3f3f3;
                border-top: 3px solid #667eea;
                border-radius: 50%;
                width: 30px;
                height: 30px;
                animation: spin 1s linear infinite;
                display: inline-block;
                margin-right: 10px;
            }
            @keyframes spin {
                0% { transform: rotate(0deg); }
                100% { transform: rotate(360deg); }
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>🔍 Universal Website Scraper</h1>
                <p>Extract structured content from any website with intelligent parsing</p>
            </div>
            
            <div class="content">
                <div class="input-group">
                    <input type="url" id="urlInput" placeholder="https://example.com" required>
                    <button onclick="scrapeWebsite()">Scrape Website</button>
                </div>
                
                <div id="status" class="status"></div>
                
                <div id="results"></div>
            </div>
        </div>

        <script>
            let currentData = null;

            async function scrapeWebsite() {
                const url = document.getElementById('urlInput').value;
                const statusDiv = document.getElementById('status');
                const resultsDiv = document.getElementById('results');
                
                if (!url) {
                    showStatus('error', 'Please enter a valid URL');
                    return;
                }

                showStatus('loading', 'Scraping website... This may take a few moments');
                resultsDiv.innerHTML = '';
                
                try {
                    const response = await fetch('/scrape', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ url: url })
                    });
                    
                    if (!response.ok) {
                        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
                    }
                    
                    const data = await response.json();
                    currentData = data.result;
                    
                    showStatus('success', `✓ Successfully scraped ${data.result.sections.length} sections using ${data.method} method`);
                    displayResults(data.result);
                } catch (error) {
                    showStatus('error', `✗ Error: ${error.message}`);
                }
            }

            function showStatus(type, message) {
                const statusDiv = document.getElementById('status');
                statusDiv.className = `status ${type}`;
                statusDiv.innerHTML = type === 'loading' 
                    ? `<div class="loader"></div>${message}`
                    : message;
            }

            function displayResults(data) {
                const resultsDiv = document.getElementById('results');
                
                // Meta information
                const metaHtml = `
                    <div class="meta-info">
                        <h3>Page Information</h3>
                        <p><strong>Title:</strong> ${data.meta.title}</p>
                        <p><strong>URL:</strong> ${data.url}</p>
                        <p><strong>Language:</strong> ${data.meta.language}</p>
                        <p><strong>Scraped At:</strong> ${new Date(data.scrapedAt).toLocaleString()}</p>
                        <p><strong>Sections Found:</strong> ${data.sections.length}</p>
                        <button class="download-btn" onclick="downloadJSON()">📥 Download Full JSON</button>
                    </div>
                `;
                
                // Sections accordion
                const sectionsHtml = data.sections.map((section, index) => `
                    <div class="accordion-item">
                        <div class="accordion-header" onclick="toggleAccordion(${index})">
                            <span><strong>${section.label}</strong> (${section.type})</span>
                            <span>▼</span>
                        </div>
                        <div class="accordion-content" id="content-${index}">
                            <pre>${JSON.stringify(section, null, 2)}</pre>
                            <button class="download-btn" onclick="downloadSection(${index})">Download Section JSON</button>
                        </div>
                    </div>
                `).join('');
                
                resultsDiv.innerHTML = metaHtml + '<div class="accordion">' + sectionsHtml + '</div>';
            }

            function toggleAccordion(index) {
                const content = document.getElementById(`content-${index}`);
                const header = content.previousElementSibling;
                
                const isActive = content.classList.contains('active');
                
                // Close all others
                document.querySelectorAll('.accordion-content').forEach(el => el.classList.remove('active'));
                document.querySelectorAll('.accordion-header').forEach(el => el.classList.remove('active'));
                
                if (!isActive) {
                    content.classList.add('active');
                    header.classList.add('active');
                }
            }

            function downloadJSON() {
                const dataStr = JSON.stringify(currentData, null, 2);
                const blob = new Blob([dataStr], { type: 'application/json' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `scrape-${Date.now()}.json`;
                a.click();
                URL.revokeObjectURL(url);
            }

            function downloadSection(index) {
                const section = currentData.sections[index];
                const dataStr = JSON.stringify(section, null, 2);
                const blob = new Blob([dataStr], { type: 'application/json' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `section-${section.id}.json`;
                a.click();
                URL.revokeObjectURL(url);
            }

            // Allow Enter key to submit
            document.getElementById('urlInput').addEventListener('keypress', (e) => {
                if (e.key === 'Enter') scrapeWebsite();
            });
        </script>
    </body>
    </html>
    """
    return html