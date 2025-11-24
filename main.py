"""
Universal Website Scraper - FastAPI Server (Complete Fixed Version)
"""
# CRITICAL: Set Windows event loop policy FIRST, before any other imports
import sys
import asyncio

if sys.platform == 'win32':
    # This MUST be set before any async operations
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse
    from pydantic import BaseModel, HttpUrl
except ImportError as exc:
    raise ImportError(
        "FastAPI and its dependencies are required. Install them with "
        "`pip install -r requirements.txt`."
    ) from exc

from datetime import datetime
import httpx
from selectolax.parser import HTMLParser
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse
import re
from typing import List, Dict, Any, Optional
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

if sys.platform == 'win32':
    logger.info("Windows detected - ProactorEventLoop policy set for Playwright compatibility")

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
def is_js_rendered(html: str, tree: HTMLParser) -> bool:
    """Detect if page is likely JS-rendered (React, Vue, Next.js, etc.)"""
    html_lower = html.lower()
    
    # Check for JS framework markers
    js_markers = [
        'react', 'vue', 'angular', 'next.js', 'nuxt', 'svelte',
        '__next', 'data-reactroot', 'ng-app', 'v-application',
        'id="__next"', 'id="root"', 'id="app"', 'id="main"'
    ]
    
    # Count script tags (JS-rendered pages often have many)
    script_count = len(tree.css('script'))
    
    # Check body content - JS shells often have minimal body text
    body_text = tree.body.text() if tree.body else ""
    body_text_clean = normalize_text(body_text)
    
    # Check for common JS-rendered patterns
    has_js_marker = any(marker in html_lower for marker in js_markers)
    has_many_scripts = script_count > 5
    has_minimal_body = len(body_text_clean) < 500
    
    # Check for empty or placeholder content areas
    main_elements = tree.css('main, [role="main"], #main, #app, #root, #__next')
    has_empty_main = False
    if main_elements:
        for main in main_elements:
            main_text = normalize_text(main.text() if main.text else "")
            if len(main_text) < 100:  # Main content area is mostly empty
                has_empty_main = True
                break
    
    # If multiple indicators suggest JS rendering, return True
    indicators = sum([has_js_marker, has_many_scripts, has_minimal_body, has_empty_main])
    
    logger.info(f"JS detection - markers: {has_js_marker}, scripts: {script_count}, "
                f"minimal body: {has_minimal_body}, empty main: {has_empty_main}, "
                f"indicators: {indicators}")
    
    # If 2+ indicators, likely JS-rendered
    return indicators >= 2

async def scrape_static(url: str) -> Optional[Dict[str, Any]]:
    """Attempt static scraping with httpx + Selectolax"""
    try:
        logger.info(f"Attempting static scraping for: {url}")
        async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
            response = await client.get(url, headers={
                'User-Agent': 'Mozilla/5.0 (compatible; LyftrScraper/1.0)'
            })
            response.raise_for_status()
            
            if len(response.content) > MAX_CONTENT_SIZE:
                logger.warning(f"Content too large: {len(response.content)} bytes")
                return None
            
            html = response.text
            tree = HTMLParser(html)
            
            # FIRST: Check if this looks like a JS-rendered page
            if is_js_rendered(html, tree):
                logger.info("Page appears to be JS-rendered, will use Playwright")
                return None
            
            # SECOND: Check if we have sufficient meaningful content
            main_text = tree.body.text() if tree.body else ""
            main_text_clean = normalize_text(main_text)
            
            logger.info(f"Static scraping - text length: {len(main_text_clean)}")
            
            # Check for meaningful content (not just meta tags and scripts)
            # Look for actual content elements
            content_elements = tree.css('article, section, main, [role="main"], .content, .post, .article')
            has_content_elements = len(content_elements) > 0
            
            # Check if body has substantial text content
            has_sufficient_text = len(main_text_clean) >= STATIC_THRESHOLD
            
            # Fallback if insufficient content
            if not has_sufficient_text and not has_content_elements:
                logger.info("Static scraping insufficient content, will try Playwright")
                return None
            
            # Additional check: if text is mostly from scripts/meta, it's likely JS-rendered
            script_text = ""
            for script in tree.css('script'):
                script_text += script.text() if script.text else ""
            script_text_clean = normalize_text(script_text)
            
            # If script text is a large portion of total text, likely JS-rendered
            if len(script_text_clean) > 0 and len(main_text_clean) > 0:
                script_ratio = len(script_text_clean) / len(main_text_clean)
                if script_ratio > 2.0:  # Scripts are 2x the body text
                    logger.info("High script-to-content ratio, likely JS-rendered")
                    return None
            
            logger.info("Static scraping successful")
            return parse_html_content(tree, url, html)
    except Exception as e:
        logger.error(f"Static scraping error: {str(e)}")
        return None

def parse_html_content(tree: HTMLParser, base_url: str, raw_html: str) -> Dict[str, Any]:
    """Parse HTML tree into structured JSON"""
    
    # Extract meta information
    try:
        title_node = tree.css_first('title')
        title = normalize_text(title_node.text() if title_node and title_node.text else "") if title_node else ""
    except:
        title = ""
    
    try:
        desc_node = tree.css_first('meta[name="description"]')
        description = desc_node.attributes.get('content', '') if desc_node and desc_node.attributes else ""
    except:
        description = ""
    
    try:
        canonical_node = tree.css_first('link[rel="canonical"]')
        canonical = canonical_node.attributes.get('href', base_url) if canonical_node and canonical_node.attributes else base_url
    except:
        canonical = base_url
    
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
    
    # Ensure we always have at least one section, even if empty
    if not sections:
        sections.append({
            "id": "content-0",
            "type": "content",
            "label": "Page Content",
            "sourceUrl": base_url,
            "content": {
                "headings": [],
                "text": normalize_text(tree.body.text() if tree.body else "")[:500] if tree.body else "",
                "links": [],
                "images": [],
                "lists": [],
                "tables": []
            },
            "rawHtml": "",
            "truncated": False
        })
    
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
            try:
                text = normalize_text(heading.text() if heading.text else "")
                if text:
                    headings.append(text)
            except:
                continue
    
    # Extract text content
    try:
        text_content = normalize_text(elem.text() if elem.text else "")
    except:
        text_content = ""
    if len(text_content) > 500:
        text_content = text_content[:500] + "..."
    
    # Extract links
    links = []
    for link in elem.css('a[href]'):
        try:
            href = link.attributes.get('href', '') if link.attributes else ''
            if href and not href.startswith(('#', 'javascript:', 'mailto:')):
                abs_href = clean_url(href, base_url)
                if is_same_domain(base_url, abs_href):
                    link_text = normalize_text(link.text() if link.text else "")
                    links.append({
                        "text": link_text,
                        "href": abs_href
                    })
        except:
            continue
    
    # Extract images
    images = []
    for img in elem.css('img[src]'):
        try:
            src = img.attributes.get('src', '') if img.attributes else ''
            if src:
                images.append({
                    "src": clean_url(src, base_url),
                    "alt": img.attributes.get('alt', '') if img.attributes else ''
                })
        except:
            continue
    
    # Extract lists
    lists = []
    for ul in elem.css('ul, ol'):
        try:
            items = []
            for li in ul.css('li'):
                try:
                    text = normalize_text(li.text() if li.text else "")
                    if text:
                        items.append(text)
                except:
                    continue
            if items:
                lists.append(items)
        except:
            continue
    
    # Extract tables
    tables = []
    for table in elem.css('table'):
        try:
            rows = []
            for tr in table.css('tr'):
                try:
                    cells = []
                    for td in tr.css('td, th'):
                        try:
                            text = normalize_text(td.text() if td.text else "")
                            if text:
                                cells.append(text)
                        except:
                            continue
                    if cells:
                        rows.append(cells)
                except:
                    continue
            if rows:
                tables.append(rows)
        except:
            continue
    
    # Generate label
    label = headings[0] if headings else generate_fallback_label(text_content, section_type)
    
    # Get raw HTML
    try:
        raw_html = elem.html if hasattr(elem, 'html') else ""
    except:
        raw_html = ""
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
    try:
        html = elem.html.lower() if hasattr(elem, 'html') and elem.html else ""
    except:
        html = ""
    
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
async def scrape_with_playwright(url: str) -> Dict[str, Any]:
    """Scrape using Playwright for JS-rendered content"""
    logger.info(f"Starting Playwright scraping for: {url}")
    errors = []
    interactions = {
        "clicks": [],
        "scrolls": 0,
        "pages": [url]
    }
    
    browser = None
    page = None
    try:
        async with async_playwright() as p:
            logger.info("Launching browser...")
            try:
                # Try to launch browser with better error handling
                browser = await p.chromium.launch(
                    headless=True,
                    args=['--no-sandbox', '--disable-setuid-sandbox']  # Help with some permission issues
                )
                logger.info("Browser launched successfully")
            except Exception as launch_error:
                error_msg = f"Failed to launch browser: {str(launch_error)}"
                logger.error(error_msg)
                # Check if it's a browser installation issue
                if "Executable doesn't exist" in str(launch_error) or "browser" in str(launch_error).lower():
                    raise HTTPException(
                        status_code=500,
                        detail=f"Playwright browser not installed. Please run: playwright install chromium"
                    )
                raise HTTPException(status_code=500, detail=error_msg)
            
            try:
                page = await browser.new_page()
                logger.info("New page created")
            except Exception as page_error:
                error_msg = f"Failed to create page: {str(page_error)}"
                logger.error(error_msg)
                if browser:
                    try:
                        await browser.close()
                    except:
                        pass
                raise HTTPException(status_code=500, detail=error_msg)
            
            try:
                # Navigate to page with better wait strategy
                logger.info(f"Navigating to {url}")
                
                # Try multiple wait strategies for better compatibility
                # networkidle can be too strict for sites with continuous connections
                # For sites like Unsplash, start with domcontentloaded (faster)
                navigation_strategies = [
                    ('domcontentloaded', 3000),   # Fast - just DOM ready (good for JS sites)
                    ('load', 2000),               # Medium - all resources loaded
                    ('networkidle', 1000),        # Slow - network quiet (may timeout on active sites)
                ]
                
                navigation_success = False
                last_error = None
                for wait_until, extra_wait in navigation_strategies:
                    try:
                        logger.info(f"Trying navigation with wait_until='{wait_until}'...")
                        await page.goto(url, wait_until=wait_until, timeout=TIMEOUT_MS)
                        await page.wait_for_timeout(extra_wait)  # Extra wait for JS rendering
                        logger.info(f"Page loaded with '{wait_until}' event")
                        navigation_success = True
                        break
                    except PlaywrightTimeout as e:
                        last_error = e
                        logger.info(f"'{wait_until}' timeout, trying next strategy...")
                        continue
                    except Exception as e:
                        last_error = e
                        logger.warning(f"Navigation error with '{wait_until}': {e}, trying next...")
                        continue
                
                if not navigation_success:
                    error_detail = f"Page load timeout - all strategies failed. Last error: {str(last_error) if last_error else 'Unknown'}"
                    logger.error(error_detail)
                    raise HTTPException(status_code=408, detail=error_detail)
                
                # Wait for content to actually appear (body with meaningful content)
                # For image-heavy sites like Unsplash, check for images and visual elements too
                logger.info("Waiting for content to render...")
                try:
                    # Wait for body to exist and have content (text OR images OR elements)
                    # Use a more lenient check for sites like Unsplash
                    await page.wait_for_function(
                        """() => {
                            if (!document.body) return false;
                            const text = document.body.innerText || '';
                            const textLength = text.trim().length;
                            const hasText = textLength > 50;  // Very lenient for image sites
                            const hasElements = document.querySelectorAll('article, section, main, [role="main"], .content, p, h1, h2, h3, div').length > 0;
                            // Check for images (important for sites like Unsplash)
                            const hasImages = document.querySelectorAll('img[src], img[data-src], img[srcset], img[loading="lazy"]').length > 0;
                            // Check for common content containers (Unsplash uses these)
                            const hasContainers = document.querySelectorAll('div[class*="grid"], div[class*="card"], div[class*="item"], div[class*="photo"], div[class*="Image"], a[href*="/photos/"]').length > 0;
                            return hasText || hasElements || hasImages || hasContainers;
                        }""",
                        timeout=25000  # Increased timeout for slow-loading sites like Unsplash
                    )
                    logger.info("Content detected")
                    
                    # Additional wait for dynamic content to fully render
                    # Unsplash and similar sites need more time for JS to hydrate
                    await page.wait_for_timeout(4000)  # Increased wait for JS-heavy sites
                    
                    # Verify content actually loaded (not just empty shell)
                    content_check = await page.evaluate("""() => {
                        const bodyText = (document.body.innerText || '').trim();
                        const hasText = bodyText.length > 100;  // Lowered threshold
                        const hasElements = document.querySelectorAll('article, section, main, p, h1, h2, h3, div[class*="content"]').length > 3;
                        const hasImages = document.querySelectorAll('img[src], img[data-src]').length > 0;
                        const hasContainers = document.querySelectorAll('div[class*="grid"], div[class*="card"], div[class*="item"]').length > 0;
                        return hasText || hasElements || hasImages || hasContainers;
                    }""")
                    
                    if not content_check:
                        logger.warning("Content check failed - page may still be loading, waiting more...")
                        # Wait longer and check again
                        await page.wait_for_timeout(5000)
                except PlaywrightTimeout:
                    logger.warning("Content wait timeout, checking if any content exists...")
                    # Final check before proceeding
                    has_any_content = False
                    try:
                        has_any_content = await page.evaluate("""() => {
                            const bodyText = (document.body.innerText || '').trim();
                            const hasText = bodyText.length > 50;
                            const hasImages = document.querySelectorAll('img').length > 0;
                            const hasElements = document.querySelectorAll('div, article, section').length > 5;
                            return hasText || hasImages || hasElements;
                        }""")
                        if not has_any_content:
                            logger.error("No content detected after timeout")
                            raise HTTPException(status_code=408, detail="Page content did not load within timeout")
                    except HTTPException:
                        raise
                    except Exception as e:
                        logger.warning(f"Content check error: {e}")
                    if not has_any_content:
                        logger.error("No content detected after timeout")
                        raise HTTPException(status_code=408, detail="Page content did not load within timeout")
                    await page.wait_for_timeout(3000)  # Fallback wait
                except Exception as e:
                    logger.warning(f"Content wait error: {e}, proceeding anyway...")
                    await page.wait_for_timeout(3000)  # Fallback wait
                
                logger.info("Page loaded successfully")
                
                # Remove noise elements (properly escape selectors)
                for selector in NOISE_SELECTORS:
                    try:
                        # Use evaluate with proper parameter passing to avoid injection issues
                        await page.evaluate("""
                            (selector) => {
                                try {
                                    document.querySelectorAll(selector).forEach(el => {
                                        try { el.remove(); } catch(e) {}
                                    });
                                } catch(e) {}
                            }
                        """, selector)
                    except Exception as e:
                        logger.debug(f"Failed to remove noise element {selector}: {e}")
                        pass
                
                # Wait for lazy-loaded images and content (important for image-heavy sites)
                logger.info("Waiting for lazy-loaded content...")
                await page.wait_for_timeout(2000)
                
                # Try to wait for images to load
                try:
                    # Wait for at least some images to be loaded
                    await page.wait_for_function(
                        """() => {
                            const images = Array.from(document.querySelectorAll('img'));
                            const loadedImages = images.filter(img => img.complete && img.naturalHeight > 0);
                            return loadedImages.length > 0 || images.length === 0;
                        }""",
                        timeout=5000
                    )
                except:
                    logger.debug("Image load wait timeout, proceeding...")
                
                # Try click interactions (tabs, load more buttons)
                logger.info("Attempting click interactions...")
                try:
                    await attempt_clicks(page, interactions)
                except Exception as e:
                    logger.warning(f"Click interactions failed: {e}")
                    errors.append({"message": f"Click interactions error: {str(e)}", "phase": "interactions"})
                
                # Wait after clicks for content to load
                await page.wait_for_timeout(2000)
                
                # Try scroll/pagination (important for infinite scroll sites like Unsplash)
                logger.info("Attempting scrolls...")
                try:
                    await attempt_scrolls(page, interactions, url)
                except Exception as e:
                    logger.warning(f"Scroll interactions failed: {e}")
                    errors.append({"message": f"Scroll interactions error: {str(e)}", "phase": "interactions"})
                
                # Final wait for any content loaded by scrolling
                await page.wait_for_timeout(2000)
                
                # Final content verification before scraping
                logger.info("Verifying final content state...")
                try:
                    final_content_check = await page.evaluate("""() => {
                        const bodyText = (document.body.innerText || '').trim();
                        const textLength = bodyText.length;
                        const elementCount = document.querySelectorAll('article, section, main, p, h1, h2, h3, div').length;
                        const imageCount = document.querySelectorAll('img[src], img[data-src]').length;
                        const hasContent = textLength > 50 || elementCount > 3 || imageCount > 0;  // More lenient
                        return {
                            textLength: textLength,
                            elementCount: elementCount,
                            imageCount: imageCount,
                            hasContent: hasContent
                        };
                    }""")
                    
                    logger.info(f"Final content check - text: {final_content_check['textLength']} chars, "
                              f"elements: {final_content_check['elementCount']}, "
                              f"images: {final_content_check['imageCount']}")
                    
                    if not final_content_check['hasContent']:
                        logger.warning("Very little content detected, but proceeding with scrape...")
                except Exception as e:
                    logger.warning(f"Final content check error: {e}, proceeding anyway...")
                
                # Get final HTML
                logger.info("Extracting content...")
                html = await page.content()
                tree = HTMLParser(html)
                
                result = parse_html_content(tree, url, html)
                result["interactions"] = interactions
                result["errors"] = errors
                
                logger.info(f"Successfully scraped {len(result['sections'])} sections")
                # Browser will be closed by context manager when exiting async with block
                return result
                
            except PlaywrightTimeout as e:
                error_msg = f"Timeout: {str(e)}"
                logger.error(error_msg)
                errors.append({"message": error_msg, "phase": "render"})
                # Browser will be closed by context manager
                raise HTTPException(status_code=408, detail="Page load timeout")
            except HTTPException:
                # Re-raise HTTP exceptions (browser cleanup handled by context manager)
                raise
            except Exception as e:
                error_msg = f"Scraping error: {str(e)}"
                logger.error(error_msg, exc_info=True)
                errors.append({"message": str(e), "phase": "scraping"})
                # Browser will be closed by context manager
                raise HTTPException(status_code=500, detail=f"Scraping failed: {str(e)}")
    except HTTPException:
        # Re-raise HTTP exceptions (browser cleanup handled by context manager)
        raise
    except Exception as e:
        error_msg = f"Playwright initialization error: {str(e)}"
        logger.error(error_msg, exc_info=True)
        
        # Provide more helpful error messages
        error_detail = str(e)
        if "Executable doesn't exist" in error_detail or "browser" in error_detail.lower():
            error_detail = "Playwright browser not installed. Please run: playwright install chromium"
        elif "timeout" in error_detail.lower():
            error_detail = f"Browser launch timeout: {error_detail}"
        elif "permission" in error_detail.lower() or "access" in error_detail.lower():
            error_detail = f"Permission error launching browser: {error_detail}"
        
        raise HTTPException(status_code=500, detail=f"Playwright initialization failed: {error_detail}")

async def attempt_clicks(page, interactions: Dict) -> bool:
    """Attempt to click tabs or 'Load more' buttons"""
    click_selectors = [
        'button:has-text("Load more")',
        'button:has-text("Show more")',
        'a:has-text("Load more")',
        '[role="tab"]',
        '[data-testid*="more"]',
        '.load-more',
        '#load-more',
        'button[aria-label*="more" i]',
        'button[aria-label*="load" i]'
    ]
    
    clicked = False
    for selector in click_selectors:
        try:
            # Wait for selector to be available
            try:
                await page.wait_for_selector(selector, timeout=2000, state='visible')
            except:
                continue  # Selector not found, try next
            
            elements = await page.query_selector_all(selector)
            for elem in elements[:3]:  # Limit to 3 clicks
                try:
                    if await elem.is_visible():
                        # Scroll element into view before clicking
                        await elem.scroll_into_view_if_needed()
                        await page.wait_for_timeout(500)  # Small wait before click
                        await elem.click()
                        await page.wait_for_timeout(SCROLL_WAIT_MS)  # Wait for content to load
                        interactions["clicks"].append(selector)
                        clicked = True
                        logger.info(f"Clicked element: {selector}")
                except Exception as e:
                    logger.debug(f"Failed to click {selector}: {e}")
                    continue
        except Exception as e:
            logger.debug(f"Error with selector {selector}: {e}")
            continue
    
    return clicked

async def attempt_scrolls(page, interactions: Dict, base_url: str) -> bool:
    """Attempt infinite scroll or pagination"""
    scrolled = False
    
    # Try infinite scroll with better detection for lazy-loaded content
    for i in range(MAX_SCROLLS):
        try:
            # Get initial state
            prev_state = await page.evaluate("""() => {
                return {
                    height: document.body.scrollHeight,
                    textLength: (document.body.innerText || '').trim().length,
                    imageCount: document.querySelectorAll('img[src], img[data-src]').length,
                    elementCount: document.querySelectorAll('div, article, section').length
                };
            }""")
            
            # Scroll to bottom smoothly
            await page.evaluate('window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" })')
            await page.wait_for_timeout(SCROLL_WAIT_MS)
            
            # Wait a bit more for lazy-loaded content (images, etc.)
            await page.wait_for_timeout(1000)
            
            # Get new state
            new_state = await page.evaluate("""() => {
                return {
                    height: document.body.scrollHeight,
                    textLength: (document.body.innerText || '').trim().length,
                    imageCount: document.querySelectorAll('img[src], img[data-src]').length,
                    elementCount: document.querySelectorAll('div, article, section').length
                };
            }""")
            
            # Check if new content loaded (height, text, images, or elements increased)
            height_increased = new_state['height'] > prev_state['height']
            text_increased = new_state['textLength'] > prev_state['textLength'] + 50
            images_increased = new_state['imageCount'] > prev_state['imageCount']
            elements_increased = new_state['elementCount'] > prev_state['elementCount'] + 5
            
            if height_increased or text_increased or images_increased or elements_increased:
                interactions["scrolls"] += 1
                scrolled = True
                logger.info(f"Scroll {i+1}: Content loaded (height: {prev_state['height']} -> {new_state['height']}, "
                          f"images: {prev_state['imageCount']} -> {new_state['imageCount']})")
            else:
                logger.info(f"Scroll {i+1}: No new content detected, stopping")
                break
        except Exception as e:
            logger.debug(f"Scroll error: {e}")
            break
    
    # Try pagination links
    if not scrolled:
        pagination_selectors = ['a[rel="next"]', '.next', '.pagination a:has-text("Next")', 'a:has-text("›")']
        for selector in pagination_selectors:
            for i in range(MAX_SCROLLS):
                try:
                    next_link = await page.query_selector(selector)
                    if next_link and await next_link.is_visible():
                        href = await next_link.get_attribute('href')
                        if href:
                            full_url = urljoin(base_url, href)
                            if is_same_domain(base_url, full_url):
                                try:
                                    await page.goto(full_url, wait_until='networkidle', timeout=TIMEOUT_MS)
                                except PlaywrightTimeout:
                                    try:
                                        await page.goto(full_url, wait_until='load', timeout=TIMEOUT_MS)
                                        await page.wait_for_timeout(2000)
                                    except:
                                        break  # If navigation fails, stop pagination
                                await page.wait_for_timeout(1500)  # Extra wait for content
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
    logger.info(f"=== Scrape request received for: {url} ===")
    
    # Validate URL scheme
    if not url.startswith(('http://', 'https://')):
        logger.error(f"Invalid URL scheme: {url}")
        raise HTTPException(status_code=400, detail="Only http(s) URLs are supported")
    
    try:
        # Try static scraping first
        result = await scrape_static(url)
        
        if result:
            logger.info("Returning static scraping result")
            return {"result": result, "method": "static"}
        
        # Fallback to Playwright
        logger.info("Falling back to Playwright scraping")
        result = await scrape_with_playwright(url)
        logger.info("Returning Playwright scraping result")
        return {"result": result, "method": "playwright"}
    except HTTPException:
        # Re-raise HTTP exceptions as-is
        raise
    except Exception as e:
        # Catch any other errors and return a proper error response
        error_msg = f"Unexpected error: {str(e)}"
        logger.error(error_msg, exc_info=True)
        raise HTTPException(
            status_code=500, 
            detail=error_msg
        )

@app.get("/healthz")
async def health_check():
    """Health check endpoint - also verifies Playwright is available"""
    status = {"status": "ok", "timestamp": datetime.utcnow().isoformat()}
    
    # Check if Playwright browsers are installed
    try:
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
                await browser.close()
                status["playwright"] = "ready"
            except Exception as e:
                status["playwright"] = f"error: {str(e)[:100]}"
                status["playwright_help"] = "Run: playwright install chromium"
    except Exception as e:
        status["playwright"] = f"initialization_error: {str(e)[:100]}"
    
    return status

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
                    <button id="scrapeButton" onclick="scrapeWebsite()">Scrape Website</button>
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
                const button = document.getElementById('scrapeButton');
                
                if (!url) {
                    showStatus('error', 'Please enter a valid URL');
                    return;
                }

                // Disable button during scraping
                if (button) {
                    button.disabled = true;
                    button.textContent = 'Scraping...';
                }
                
                showStatus('loading', 'Scraping website... This may take a few moments');
                resultsDiv.innerHTML = '';
                currentData = null;
                
                try {
                    const response = await fetch('/scrape', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ url: url })
                    });
                    
                    if (!response.ok) {
                        const errorData = await response.json();
                        throw new Error(errorData.detail || `HTTP ${response.status}`);
                    }
                    
                    const data = await response.json();
                    currentData = data.result;
                    
                    if (!currentData || !currentData.sections) {
                        throw new Error('Invalid response: missing sections');
                    }
                    
                    showStatus('success', `✓ Successfully scraped ${currentData.sections.length} sections using ${data.method} method`);
                    displayResults(currentData);
                } catch (error) {
                    showStatus('error', `✗ Error: ${error.message}`);
                    currentData = null;
                } finally {
                    // Re-enable button
                    if (button) {
                        button.disabled = false;
                        button.textContent = 'Scrape Website';
                    }
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
                
                if (!data || !data.meta || !data.sections) {
                    resultsDiv.innerHTML = '<div class="status error">Invalid data structure received</div>';
                    return;
                }
                
                // Escape HTML to prevent XSS
                function escapeHtml(text) {
                    const div = document.createElement('div');
                    div.textContent = text;
                    return div.innerHTML;
                }
                
                // Meta information
                const metaHtml = `
                    <div class="meta-info">
                        <h3>Page Information</h3>
                        <p><strong>Title:</strong> ${escapeHtml(data.meta.title || 'N/A')}</p>
                        <p><strong>URL:</strong> <a href="${escapeHtml(data.url || '')}" target="_blank">${escapeHtml(data.url || 'N/A')}</a></p>
                        <p><strong>Language:</strong> ${escapeHtml(data.meta.language || 'en')}</p>
                        <p><strong>Scraped At:</strong> ${data.scrapedAt ? new Date(data.scrapedAt).toLocaleString() : 'N/A'}</p>
                        <p><strong>Sections Found:</strong> ${data.sections ? data.sections.length : 0}</p>
                        <button class="download-btn" onclick="downloadJSON()">📥 Download Full JSON</button>
                    </div>
                `;
                
                // Sections accordion
                const sectionsHtml = (data.sections || []).map((section, index) => {
                    const label = escapeHtml(section.label || `Section ${index + 1}`);
                    const type = escapeHtml(section.type || 'unknown');
                    const sectionJson = JSON.stringify(section, null, 2);
                    return `
                    <div class="accordion-item">
                        <div class="accordion-header" onclick="toggleAccordion(${index})">
                            <span><strong>${label}</strong> (${type})</span>
                            <span>▼</span>
                        </div>
                        <div class="accordion-content" id="content-${index}">
                            <pre>${sectionJson}</pre>
                            <button class="download-btn" onclick="downloadSection(${index})">Download Section JSON</button>
                        </div>
                    </div>
                `;
                }).join('');
                
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
                if (!currentData) {
                    alert('No data available to download. Please scrape a website first.');
                    return;
                }
                try {
                    const dataStr = JSON.stringify(currentData, null, 2);
                    const blob = new Blob([dataStr], { type: 'application/json' });
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
                    a.download = `scrape-${timestamp}.json`;
                    document.body.appendChild(a);
                    a.click();
                    document.body.removeChild(a);
                    URL.revokeObjectURL(url);
                } catch (error) {
                    alert('Error downloading JSON: ' + error.message);
                }
            }

            function downloadSection(index) {
                if (!currentData || !currentData.sections || !currentData.sections[index]) {
                    alert('Section data not available.');
                    return;
                }
                try {
                    const section = currentData.sections[index];
                    const dataStr = JSON.stringify(section, null, 2);
                    const blob = new Blob([dataStr], { type: 'application/json' });
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    const safeId = (section.id || `section-${index}`).replace(/[^a-z0-9-]/gi, '-');
                    a.download = `${safeId}.json`;
                    document.body.appendChild(a);
                    a.click();
                    document.body.removeChild(a);
                    URL.revokeObjectURL(url);
                } catch (error) {
                    alert('Error downloading section: ' + error.message);
                }
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