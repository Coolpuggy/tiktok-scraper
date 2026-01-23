"""
TikTok Shop Review Scraper - Web Interface
Flask app with Bright Data Scraping Browser (handles CAPTCHAs automatically)
"""

from flask import Flask, render_template, request, jsonify, Response
from flask_cors import CORS
import json
import time
import re
import threading
import os
from urllib.parse import urlparse

app = Flask(__name__)
CORS(app)

# Store for scraping progress and results
scrape_jobs = {}


def extract_product_id(url):
    """Extract product ID from TikTok Shop URL."""
    match = re.search(r'/(\d{15,20})(?:\?|$)', url)
    if match:
        return match.group(1)
    parsed = urlparse(url)
    path_parts = parsed.path.strip('/').split('/')
    for part in path_parts:
        if part.isdigit() and len(part) > 10:
            return part
    return None


def get_scraping_browser_url():
    """Get the Bright Data Scraping Browser WebSocket URL."""
    # Option 1: Direct scraping browser URL
    sb_url = os.environ.get('BRIGHT_DATA_SB_URL')
    if sb_url:
        return sb_url

    # Option 2: Build from credentials
    # Format: wss://brd-customer-CUSTOMER_ID-zone-ZONE:PASSWORD@brd.superproxy.io:9222
    sb_auth = os.environ.get('BRIGHT_DATA_SB_AUTH')
    if sb_auth:
        # sb_auth format: brd-customer-XXX-zone-ZONE:PASSWORD
        return f"wss://{sb_auth}@brd.superproxy.io:9222"

    return None


def scrape_reviews_with_progress(job_id, product_url, max_pages=50):
    """Scrape reviews using Bright Data Scraping Browser (handles CAPTCHAs)."""
    job = scrape_jobs[job_id]
    job['status'] = 'starting'
    job['message'] = 'Connecting to Scraping Browser...'

    browser_ws_url = get_scraping_browser_url()
    if not browser_ws_url:
        job['status'] = 'error'
        job['message'] = 'Scraping Browser not configured. Set BRIGHT_DATA_SB_URL or BRIGHT_DATA_SB_AUTH env var.'
        return

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        job['status'] = 'error'
        job['message'] = 'Playwright not installed. Run: pip install playwright'
        return

    reviews = []
    seen_reviews = set()

    try:
        with sync_playwright() as p:
            print(f"[{job_id}] Connecting to Scraping Browser...")
            job['message'] = 'Connecting to remote browser...'

            # Connect to Bright Data's Scraping Browser via CDP
            browser = p.chromium.connect_over_cdp(browser_ws_url)
            print(f"[{job_id}] Connected to Scraping Browser")

            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()

            # Set viewport
            page.set_viewport_size({"width": 1920, "height": 1080})

            # Navigate to product page
            job['status'] = 'loading'
            job['message'] = 'Loading product page (may solve CAPTCHA)...'
            print(f"[{job_id}] Loading URL: {product_url}")

            page.goto(product_url, timeout=120000, wait_until='domcontentloaded')

            # Wait for page to fully load - Scraping Browser handles CAPTCHAs automatically
            # Give it extra time for CAPTCHA solving
            page.wait_for_timeout(10000)

            # Check if we're past the CAPTCHA
            page_title = page.title()
            print(f"[{job_id}] Page title: '{page_title}'")

            # If still on security check, wait longer
            if 'security' in page_title.lower() or 'verify' in page_title.lower():
                print(f"[{job_id}] CAPTCHA detected, waiting for auto-solve...")
                job['message'] = 'Solving CAPTCHA...'
                page.wait_for_timeout(30000)
                page_title = page.title()
                print(f"[{job_id}] After wait - title: '{page_title}'")

            # Extract product info
            job['message'] = 'Extracting product info...'
            product_info = page.evaluate("""() => {
                let title = '';
                let image = '';

                const titleEl = document.querySelector('h1') ||
                               document.querySelector('[class*="title"]') ||
                               document.querySelector('[class*="Title"]');
                if (titleEl) title = titleEl.innerText.trim().split('\\n')[0];

                const imgEl = document.querySelector('[class*="ProductImage"] img') ||
                             document.querySelector('[class*="product-image"] img') ||
                             document.querySelector('[class*="gallery"] img') ||
                             document.querySelector('[class*="slider"] img') ||
                             document.querySelector('img[class*="product"]');
                if (imgEl) image = imgEl.src || imgEl.getAttribute('data-src') || '';

                if (!image) {
                    const imgs = document.querySelectorAll('img');
                    for (const img of imgs) {
                        if (img.width > 200 && img.height > 200 && img.src) {
                            image = img.src;
                            break;
                        }
                    }
                }

                return {title: title, image: image};
            }""")

            job['product_title'] = product_info.get('title', '')
            job['product_image'] = product_info.get('image', '')
            print(f"[{job_id}] Product: {job['product_title'][:50]}")

            # Scroll to reviews section
            job['message'] = 'Finding reviews section...'
            for i in range(5):
                page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {0.3 + i*0.15})")
                page.wait_for_timeout(400)

            page.evaluate("""() => {
                const ratingEl = document.querySelector('[aria-label*="Rating:"][aria-label*="out of 5 stars"]');
                if (ratingEl) ratingEl.scrollIntoView({block: 'center', behavior: 'smooth'});
            }""")
            page.wait_for_timeout(1000)

            # Scroll more to load pagination
            for _ in range(10):
                page.evaluate("window.scrollBy(0, 800)")
                page.wait_for_timeout(200)

            job['status'] = 'scraping'
            current_page = 1
            print(f"[{job_id}] Starting to scrape reviews...")

            while current_page <= max_pages:
                job['current_page'] = current_page
                job['max_pages'] = max_pages
                job['message'] = f'Scraping page {current_page}...'
                job['progress'] = int((current_page / max_pages) * 100)

                # Extract reviews
                review_data = page.evaluate("""() => {
                    const reviews = [];
                    const ratingElements = document.querySelectorAll('[aria-label*="Rating:"][aria-label*="out of 5 stars"]');

                    ratingElements.forEach(ratingEl => {
                        try {
                            const ariaLabel = ratingEl.getAttribute('aria-label');
                            const ratingMatch = ariaLabel.match(/Rating:\\s*(\\d+)\\s*out of 5/);
                            const rating = ratingMatch ? parseInt(ratingMatch[1]) : 0;

                            let container = ratingEl;
                            for (let i = 0; i < 8; i++) {
                                container = container.parentElement;
                                if (!container) break;
                                const text = container.innerText || '';
                                if (text.length > 30 && /[A-Za-z]\\*+[A-Za-z0-9]/.test(text)) break;
                            }

                            if (!container) return;
                            const fullText = container.innerText || '';

                            const usernameMatch = fullText.match(/([A-Za-z]\\*+[A-Za-z0-9])/);
                            const username = usernameMatch ? usernameMatch[1] : '';
                            if (!username) return;

                            const dateMatch = fullText.match(/(\\d{4}-\\d{2}-\\d{2})/);
                            const date = dateMatch ? dateMatch[1] : '';

                            let itemVariant = '';
                            const lines = fullText.split('\\n');
                            for (let i = 0; i < lines.length; i++) {
                                const trimmed = lines[i].trim();
                                if (trimmed === 'Item:' || trimmed === 'Item' || trimmed.toLowerCase() === 'item:') {
                                    for (let j = i + 1; j < lines.length && j < i + 3; j++) {
                                        const nextLine = lines[j].trim();
                                        if (nextLine && nextLine.length > 2 && nextLine.length < 80 &&
                                            !nextLine.includes('Verified') && !/^\\d+$/.test(nextLine)) {
                                            itemVariant = nextLine;
                                            break;
                                        }
                                    }
                                    if (itemVariant) break;
                                }
                                if (/^Item:\\s*.+/i.test(trimmed)) {
                                    itemVariant = trimmed.replace(/^Item:\\s*/i, '');
                                    break;
                                }
                                if (/^(Color|Size|Variant|Style):/i.test(trimmed)) {
                                    itemVariant = trimmed;
                                    break;
                                }
                                if (!itemVariant && trimmed.length > 5 && trimmed.length < 60 &&
                                    /^[A-Z][a-z]+[-\\s][A-Z]/.test(trimmed) &&
                                    !trimmed.includes('Verified') && !trimmed.includes('US') &&
                                    !trimmed.includes('Rating')) {
                                    itemVariant = trimmed;
                                }
                            }

                            let reviewText = '';
                            for (const line of lines) {
                                const trimmed = line.trim();
                                if (trimmed.length < 15) continue;
                                if (/^(Verified|US|Item:|Color:|Size:|\\d{4}-|\\d+$|Rating:)/.test(trimmed)) continue;
                                if (/^[A-Za-z]\\*+[A-Za-z0-9]$/.test(trimmed)) continue;
                                if (trimmed === itemVariant) continue;
                                if (trimmed.length > reviewText.length) reviewText = trimmed;
                            }

                            if (reviewText.length > 10) {
                                reviews.push({
                                    username: username,
                                    rating: rating,
                                    review_text: reviewText,
                                    date: date,
                                    item_variant: itemVariant
                                });
                            }
                        } catch (e) {}
                    });
                    return reviews;
                }""")

                # Deduplicate and add
                for r in review_data:
                    key = r['review_text'][:50] if r['review_text'] else ''
                    if key and key not in seen_reviews:
                        seen_reviews.add(key)
                        reviews.append(r)

                job['reviews'] = reviews.copy()
                job['review_count'] = len(reviews)

                if current_page >= max_pages:
                    break

                # Click Next button
                page.evaluate("window.scrollBy(0, 400)")
                page.wait_for_timeout(800)

                clicked = page.evaluate("""() => {
                    const allElements = document.querySelectorAll('button, a, span, div, li, [role="button"]');

                    for (const el of allElements) {
                        const text = el.innerText?.trim();
                        if (text === 'Next' || text === 'Next →' || text === 'Next→' || text === 'next') {
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0 && rect.top > 200) {
                                el.scrollIntoView({block: 'center'});
                                el.click();
                                return true;
                            }
                        }
                    }

                    for (const el of allElements) {
                        const text = el.innerText?.trim();
                        if (text === '→' || text === '>' || text === '›' || text === '»') {
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0 && rect.top > 200) {
                                el.scrollIntoView({block: 'center'});
                                el.click();
                                return true;
                            }
                        }
                    }

                    const paginationContainers = document.querySelectorAll('[class*="pagination"], [class*="Pagination"], [class*="pager"], [class*="Pager"]');
                    for (const container of paginationContainers) {
                        const buttons = container.querySelectorAll('button, a, li, span');
                        const buttonArray = Array.from(buttons);
                        for (let i = 0; i < buttonArray.length; i++) {
                            const btn = buttonArray[i];
                            if (btn.classList.contains('active') || btn.getAttribute('aria-current') === 'true' ||
                                btn.classList.contains('selected') || btn.classList.contains('current')) {
                                if (buttonArray[i + 1]) {
                                    buttonArray[i + 1].scrollIntoView({block: 'center'});
                                    buttonArray[i + 1].click();
                                    return true;
                                }
                            }
                        }
                    }

                    const svgButtons = document.querySelectorAll('button svg, a svg');
                    for (const svg of svgButtons) {
                        const parent = svg.closest('button, a');
                        if (parent) {
                            const rect = parent.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0 && rect.top > 200) {
                                const siblingText = parent.parentElement?.innerText || '';
                                if (siblingText.includes('Next') || parent.getAttribute('aria-label')?.toLowerCase().includes('next')) {
                                    parent.scrollIntoView({block: 'center'});
                                    parent.click();
                                    return true;
                                }
                            }
                        }
                    }

                    for (const el of allElements) {
                        const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                        if (aria.includes('next page') || aria.includes('go to next')) {
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0 && rect.top > 200) {
                                el.scrollIntoView({block: 'center'});
                                el.click();
                                return true;
                            }
                        }
                    }

                    return false;
                }""")

                if clicked:
                    page.wait_for_timeout(1500)
                    current_page += 1
                else:
                    job['message'] = 'No more pages found'
                    break

            job['status'] = 'complete'
            job['message'] = f'Done! Found {len(reviews)} reviews'
            job['progress'] = 100

            # Close browser
            browser.close()

    except Exception as e:
        print(f"[{job_id}] ERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        job['status'] = 'error'
        job['message'] = f'Error: {str(e)}'


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/start', methods=['POST'])
def start_scrape():
    data = request.json
    url = data.get('url', '').strip()
    max_pages = int(data.get('max_pages', 50))

    if not url:
        return jsonify({'error': 'URL is required'}), 400

    product_id = extract_product_id(url)
    if not product_id:
        return jsonify({'error': 'Invalid TikTok Shop URL. Could not find product ID.'}), 400

    # Create job
    job_id = f"job_{int(time.time() * 1000)}"
    scrape_jobs[job_id] = {
        'status': 'queued',
        'message': 'Starting...',
        'progress': 0,
        'current_page': 0,
        'max_pages': max_pages,
        'reviews': [],
        'review_count': 0
    }

    # Start scraping in background thread
    thread = threading.Thread(
        target=scrape_reviews_with_progress,
        args=(job_id, url, max_pages)
    )
    thread.daemon = True
    thread.start()

    return jsonify({'job_id': job_id, 'product_id': product_id})


@app.route('/status/<job_id>')
def get_status(job_id):
    if job_id not in scrape_jobs:
        return jsonify({'error': 'Job not found'}), 404

    job = scrape_jobs[job_id]
    return jsonify({
        'status': job['status'],
        'message': job['message'],
        'progress': job['progress'],
        'current_page': job.get('current_page', 0),
        'max_pages': job.get('max_pages', 0),
        'review_count': job.get('review_count', 0),
        'reviews': job.get('reviews', []) if job['status'] == 'complete' else [],
        'product_title': job.get('product_title', ''),
        'product_image': job.get('product_image', '')
    })


@app.route('/stream/<job_id>')
def stream_status(job_id):
    """Server-Sent Events for real-time progress."""
    def generate():
        while True:
            if job_id not in scrape_jobs:
                yield f"data: {json.dumps({'error': 'Job not found'})}\n\n"
                break

            job = scrape_jobs[job_id]
            data = {
                'status': job['status'],
                'message': job['message'],
                'progress': job['progress'],
                'current_page': job.get('current_page', 0),
                'max_pages': job.get('max_pages', 0),
                'review_count': job.get('review_count', 0)
            }

            if job['status'] in ('complete', 'error'):
                data['reviews'] = job.get('reviews', [])
                data['product_title'] = job.get('product_title', '')
                data['product_image'] = job.get('product_image', '')
                yield f"data: {json.dumps(data)}\n\n"
                break

            yield f"data: {json.dumps(data)}\n\n"
            time.sleep(0.5)

    return Response(generate(), mimetype='text/event-stream')


@app.route('/health')
def health():
    """Health check endpoint for Railway."""
    return jsonify({
        'status': 'ok',
        'scraping_browser_configured': bool(get_scraping_browser_url())
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'true').lower() == 'true'
    app.run(debug=debug, host='0.0.0.0', port=port, threaded=True)
