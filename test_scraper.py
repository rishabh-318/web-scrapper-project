"""
Test script for the Universal Website Scraper
Tests the three required URLs and validates output
"""
import httpx
import json
import sys
from datetime import datetime

TEST_URLS = [
    {
        "url": "https://en.wikipedia.org/wiki/Artificial_intelligence",
        "name": "Wikipedia (Static)",
        "expected_sections_min": 5,
        "expected_method": "static"
    },
    {
        "url": "https://news.ycombinator.com/",
        "name": "Hacker News (Pagination)",
        "expected_sections_min": 1,
        "expected_method": "static"
    },
    {
        "url": "https://unsplash.com/",
        "name": "Unsplash (JS-Rendered + Infinite Scroll)",
        "expected_sections_min": 3,
        "expected_method": "playwright"
    }
]

API_URL = "http://localhost:8000/scrape"

def test_scraper():
    """Run tests on all URLs"""
    print("=" * 70)
    print("🧪 Universal Website Scraper - Test Suite")
    print("=" * 70)
    print()
    
    results = []
    
    for i, test in enumerate(TEST_URLS, 1):
        print(f"Test {i}/{len(TEST_URLS)}: {test['name']}")
        print(f"URL: {test['url']}")
        print("-" * 70)
        
        try:
            # Make request
            with httpx.Client(timeout=60.0) as client:
                response = client.post(
                    API_URL,
                    json={"url": test["url"]}
                )
                response.raise_for_status()
                data = response.json()
            
            result = data.get("result", {})
            method = data.get("method", "unknown")
            
            # Validate response
            checks = {
                "has_url": bool(result.get("url")),
                "has_meta": bool(result.get("meta")),
                "has_sections": len(result.get("sections", [])) > 0,
                "enough_sections": len(result.get("sections", [])) >= test["expected_sections_min"],
                "has_scraped_at": bool(result.get("scrapedAt")),
                "has_interactions": bool(result.get("interactions")),
            }
            
            all_passed = all(checks.values())
            
            # Print results
            print(f"✓ Response received")
            print(f"  Method: {method}")
            print(f"  Sections: {len(result.get('sections', []))}")
            print(f"  Title: {result.get('meta', {}).get('title', 'N/A')[:60]}")
            print(f"  Interactions - Clicks: {len(result.get('interactions', {}).get('clicks', []))}, Scrolls: {result.get('interactions', {}).get('scrolls', 0)}, Pages: {len(result.get('interactions', {}).get('pages', []))}")
            
            print(f"\n  Validation:")
            for check, passed in checks.items():
                symbol = "✓" if passed else "✗"
                print(f"    {symbol} {check}")
            
            if all_passed:
                print(f"\n✅ PASSED")
                results.append({"test": test["name"], "status": "PASSED"})
            else:
                print(f"\n❌ FAILED")
                results.append({"test": test["name"], "status": "FAILED"})
            
            # Save output
            filename = f"test_output_{i}.json"
            with open(filename, "w", encoding='utf-8') as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
            print(f"  Output saved to: {filename}")
            
        except httpx.HTTPError as e:
            print(f"❌ HTTP Error: {e}")
            results.append({"test": test["name"], "status": "ERROR", "error": str(e)})
        except Exception as e:
            print(f"❌ Error: {e}")
            results.append({"test": test["name"], "status": "ERROR", "error": str(e)})
        
        print()
    
    # Summary
    print("=" * 70)
    print("📊 Test Summary")
    print("=" * 70)
    passed = sum(1 for r in results if r["status"] == "PASSED")
    failed = sum(1 for r in results if r["status"] == "FAILED")
    errors = sum(1 for r in results if r["status"] == "ERROR")
    
    for result in results:
        status = result["status"]
        symbol = "✅" if status == "PASSED" else "❌"
        print(f"{symbol} {result['test']}: {status}")
        if "error" in result:
            print(f"   Error: {result['error']}")
    
    print()
    print(f"Total: {len(results)} | Passed: {passed} | Failed: {failed} | Errors: {errors}")
    
    return passed == len(results)

def check_health():
    """Check if server is running"""
    try:
        response = httpx.get("http://localhost:8000/healthz", timeout=5.0)
        response.raise_for_status()
        print("✓ Server is running")
        return True
    except:
        print("✗ Server is not running. Please start it with:")
        print("  uvicorn main:app --reload --host 0.0.0.0 --port 8000")
        return False

if __name__ == "__main__":
    print()
    if not check_health():
        sys.exit(1)
    
    print()
    success = test_scraper()
    
    sys.exit(0 if success else 1)