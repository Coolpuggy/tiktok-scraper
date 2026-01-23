"""
TikTok Shop Review Scraper - Manual CAPTCHA Flow
Streams browser screenshots so users can solve CAPTCHAs manually,
then auto-scrapes reviews once past the CAPTCHA.
"""

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import json
import time
import re
import threading
import os
import base64
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


def get_proxy_url():
    """Get residential proxy URL from environment."""
    proxy = os.environ.get('BRIGHT_DATA_PROXY')
    if proxy:
        # Format: user:pass@host:port
        if not proxy.startswith('http'):
            proxy = f"http://{proxy}"
        return proxy
    return None


def scrape_reviews_with_progress(job_id, product_url, max_pages=50):
    """Scrape reviews with manual CAPTCHA solving via browser view."""
    job = scrape_jobs[job_id]
    job['status'] = 'starting'
    job['message'] = 'Launching browser...'

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        job['status'] = 'error'
        job['message'] = 'Playwright not installed.'
        return

    reviews = []
    seen_reviews = set()

    try:
        with sync_playwright() as p:
            print(f"[{job_id}] Launching local browser...")

            launch_args = [
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled',
            ]

            proxy_url = get_proxy_url()
            proxy_config = None
            if proxy_url:
                proxy_config = {"server": proxy_url}
                print(f"[{job_id}] Using residential proxy: {proxy_url.split('@')[-1] if '@' in proxy_url else proxy_url}")

            browser = p.chromium.launch(
                headless=True,
                args=launch_args,
                proxy=proxy_config,
            )

            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )

            page = context.new_page()
            job['_page'] = page
            job['_browser'] = browser

            # Navigate to product page
            job['status'] = 'captcha'
            job['message'] = 'Loading page - please solve CAPTCHA if shown...'
            print(f"[{job_id}] Navigating to: {product_url}")

            # Start screenshot streaming in background (starts even during page load)
            def update_screenshots():
                while job.get('status') in ('captcha', 'starting', 'loading'):
                    try:
                        if job.get('_page') and not job.get('_browser_closed'):
                            ss = page.screenshot(type='jpeg', quality=50)
                            job['_screenshot'] = base64.b64encode(ss).decode('utf-8')
                            job['_screenshot_updated'] = time.time()
                    except Exception as ss_err:
                        print(f"[{job_id}] Screenshot error: {ss_err}")
                        time.sleep(1)
                        continue
                    time.sleep(0.3)

            ss_thread = threading.Thread(target=update_screenshots, daemon=True)
            ss_thread.start()

            try:
                page.goto(product_url, timeout=60000, wait_until='domcontentloaded')
            except Exception as nav_err:
                print(f"[{job_id}] Navigation error (may be proxy issue): {nav_err}")
                # Even if goto times out, the page might have partially loaded
                # Continue and let the user interact

            page.wait_for_timeout(2000)

            # Wait for CAPTCHA to be solved (check for review elements or page content)
            # We give the user up to 3 minutes to solve it
            captcha_timeout = 180  # seconds
            start_wait = time.time()
            captcha_solved = False

            while time.time() - start_wait < captcha_timeout:
                if job.get('status') == 'error':
                    break

                # Check if page has reviews or product content (CAPTCHA solved)
                try:
                    has_content = page.evaluate("""() => {
                        // Check for rating elements (reviews section)
                        const ratings = document.querySelectorAll('[aria-label*="Rating:"][aria-label*="out of 5 stars"]');
                        if (ratings.length > 0) return 'reviews';

                        // Check for product title (at minimum we're past CAPTCHA)
                        const h1 = document.querySelector('h1');
                        if (h1 && h1.innerText.trim().length > 5) {
                            // Make sure it's not a CAPTCHA page title
                            const title = document.title.toLowerCase();
                            if (!title.includes('security') && !title.includes('verify') && !title.includes('captcha')) {
                                return 'product';
                            }
                        }
                        return null;
                    }""")

                    if has_content:
                        captcha_solved = True
                        print(f"[{job_id}] CAPTCHA solved! Found: {has_content}")
                        break
                except Exception:
                    pass

                time.sleep(1)

            if not captcha_solved:
                job['status'] = 'error'
                job['message'] = 'CAPTCHA was not solved in time. Please try again.'
                try:
                    browser.close()
                except Exception:
                    pass
                job['_browser_closed'] = True
                return

            # CAPTCHA solved - proceed with scraping
            job['status'] = 'scraping'
            job['message'] = 'CAPTCHA solved! Scraping reviews...'
            print(f"[{job_id}] Starting review extraction...")

            # Stop screenshot streaming
            job['_screenshot'] = None

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

            # Extract product info
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

                return {title, image};
            }""")

            job['product_title'] = product_info.get('title', '')
            job['product_image'] = product_info.get('image', '')
            print(f"[{job_id}] Product: {job['product_title'][:50]}")

            current_page = 1

            while current_page <= max_pages:
                job['current_page'] = current_page
                job['max_pages'] = max_pages
                job['message'] = f'Scraping page {current_page}...'
                job['progress'] = int((current_page / max_pages) * 100)

                # Extract reviews from current page
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
                                    username,
                                    rating,
                                    review_text: reviewText,
                                    date,
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

            browser.close()
            job['_browser_closed'] = True

    except Exception as e:
        print(f"[{job_id}] ERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        job['status'] = 'error'
        job['message'] = f'Error: {str(e)}'
        job['_browser_closed'] = True


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
        'review_count': 0,
        '_browser_closed': False,
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
        'product_image': job.get('product_image', ''),
        'has_screenshot': bool(job.get('_screenshot')),
    })


@app.route('/browser-stream/<job_id>')
def browser_stream(job_id):
    """SSE endpoint that streams browser screenshots as base64 JPEG."""
    if job_id not in scrape_jobs:
        return jsonify({'error': 'Job not found'}), 404

    def generate():
        last_sent = 0
        while True:
            if job_id not in scrape_jobs:
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                break

            job = scrape_jobs[job_id]

            # If no longer in captcha state, send done
            if job['status'] not in ('captcha', 'loading', 'starting'):
                yield f"data: {json.dumps({'type': 'solved', 'status': job['status']})}\n\n"
                break

            # Send screenshot if updated
            screenshot = job.get('_screenshot')
            updated = job.get('_screenshot_updated', 0)

            if screenshot and updated > last_sent:
                yield f"data: {json.dumps({'type': 'frame', 'image': screenshot})}\n\n"
                last_sent = updated

            time.sleep(0.3)

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/browser-event/<job_id>', methods=['POST'])
def browser_event(job_id):
    """Receive mouse/keyboard events from frontend and forward to browser."""
    if job_id not in scrape_jobs:
        return jsonify({'error': 'Job not found'}), 404

    job = scrape_jobs[job_id]
    page = job.get('_page')

    if not page or job.get('_browser_closed'):
        return jsonify({'error': 'Browser not available'}), 400

    data = request.json
    event_type = data.get('type')

    try:
        if event_type == 'click':
            x = data.get('x', 0)
            y = data.get('y', 0)
            page.mouse.click(x, y)
        elif event_type == 'mousemove':
            x = data.get('x', 0)
            y = data.get('y', 0)
            page.mouse.move(x, y)
        elif event_type == 'mousedown':
            x = data.get('x', 0)
            y = data.get('y', 0)
            page.mouse.move(x, y)
            page.mouse.down()
        elif event_type == 'mouseup':
            x = data.get('x', 0)
            y = data.get('y', 0)
            page.mouse.move(x, y)
            page.mouse.up()
        elif event_type == 'scroll':
            x = data.get('x', 0)
            y = data.get('y', 0)
            delta_x = data.get('deltaX', 0)
            delta_y = data.get('deltaY', 0)
            page.mouse.wheel(delta_x, delta_y)
        elif event_type == 'keydown':
            key = data.get('key', '')
            if key:
                page.keyboard.press(key)

        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/stream/<job_id>')
def stream_status(job_id):
    """Server-Sent Events for real-time progress (non-screenshot data)."""
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

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/health')
def health():
    """Health check endpoint."""
    return jsonify({
        'status': 'ok',
        'proxy_configured': bool(get_proxy_url()),
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'true').lower() == 'true'
    app.run(debug=debug, host='0.0.0.0', port=port, threaded=True)
