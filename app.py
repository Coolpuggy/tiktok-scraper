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
import queue
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

            # Launch browser (without proxy - user solves CAPTCHA manually)
            browser = p.chromium.launch(
                headless=True,
                args=launch_args,
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
            job['message'] = 'Loading page...'
            print(f"[{job_id}] Navigating to: {product_url}")

            try:
                page.goto(product_url, timeout=30000, wait_until='domcontentloaded')
                print(f"[{job_id}] Page loaded")
            except Exception as nav_err:
                print(f"[{job_id}] Navigation timeout (continuing): {nav_err}")

            page.wait_for_timeout(2000)
            job['message'] = 'Please solve the CAPTCHA if shown...'

            # Take initial screenshot
            try:
                ss = page.screenshot(type='jpeg', quality=50)
                job['_screenshot'] = base64.b64encode(ss).decode('utf-8')
                job['_screenshot_updated'] = time.time()
                print(f"[{job_id}] Initial screenshot taken ({len(ss)} bytes)")
            except Exception as ss_err:
                print(f"[{job_id}] Screenshot error: {ss_err}")

            # Wait for CAPTCHA to be solved (check for review elements or page content)
            # We give the user up to 3 minutes to solve it
            captcha_timeout = 180  # seconds
            start_wait = time.time()
            captcha_solved = False
            screenshot_interval = 0.4  # seconds between screenshots

            while time.time() - start_wait < captcha_timeout:
                if job.get('status') == 'error':
                    break

                # Process any pending mouse/keyboard events from frontend
                event_queue = job.get('_event_queue')
                if event_queue:
                    while not event_queue.empty():
                        try:
                            evt = event_queue.get_nowait()
                            evt_type = evt.get('type')
                            x = evt.get('x', 0)
                            y = evt.get('y', 0)
                            if evt_type == 'click':
                                page.mouse.click(x, y)
                            elif evt_type == 'mousedown':
                                page.mouse.move(x, y)
                                page.mouse.down()
                            elif evt_type == 'mouseup':
                                page.mouse.move(x, y)
                                page.mouse.up()
                            elif evt_type == 'mousemove':
                                page.mouse.move(x, y)
                            elif evt_type == 'scroll':
                                page.mouse.wheel(evt.get('deltaX', 0), evt.get('deltaY', 0))
                            elif evt_type == 'keydown':
                                key = evt.get('key', '')
                                if key:
                                    page.keyboard.press(key)
                        except Exception as evt_err:
                            print(f"[{job_id}] Event error: {evt_err}")

                # Take screenshot for streaming to frontend
                try:
                    ss = page.screenshot(type='jpeg', quality=50)
                    job['_screenshot'] = base64.b64encode(ss).decode('utf-8')
                    job['_screenshot_updated'] = time.time()
                except Exception:
                    pass

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

                time.sleep(screenshot_interval)

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
                    const debug = {found_ratings: 0, skipped_no_container: 0, skipped_no_username: 0, skipped_short_text: 0};
                    const ratingElements = document.querySelectorAll('[aria-label*="Rating:"][aria-label*="out of 5 stars"]');
                    debug.found_ratings = ratingElements.length;

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

                            if (!container) { debug.skipped_no_container++; return; }
                            const fullText = container.innerText || '';

                            const usernameMatch = fullText.match(/([A-Za-z]\\*+[A-Za-z0-9])/);
                            const username = usernameMatch ? usernameMatch[1] : '';
                            if (!username) { debug.skipped_no_username++; return; }

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
                            } else {
                                debug.skipped_short_text++;
                            }
                        } catch (e) {}
                    });
                    return {reviews, debug};
                }""")

                # review_data is now {reviews: [...], debug: {...}}
                page_reviews = review_data.get('reviews', []) if isinstance(review_data, dict) else review_data
                debug_info = review_data.get('debug', {}) if isinstance(review_data, dict) else {}
                print(f"[{job_id}] Page {current_page} debug: {debug_info}")
                print(f"[{job_id}] Page {current_page}: found {len(page_reviews)} reviews")

                # Deduplicate and add
                for r in page_reviews:
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
                    print(f"[{job_id}] Clicked next, waiting for page {current_page + 1}...")
                    page.wait_for_timeout(2000)
                    current_page += 1
                else:
                    print(f"[{job_id}] No next button found after page {current_page}")
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
        '_event_queue': queue.Queue(),
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
    """Receive mouse/keyboard events from frontend and queue them for the browser."""
    if job_id not in scrape_jobs:
        return jsonify({'error': 'Job not found'}), 404

    job = scrape_jobs[job_id]

    if job.get('_browser_closed') or job.get('status') not in ('captcha', 'starting', 'loading'):
        return jsonify({'error': 'Browser not in interactive state'}), 400

    event_queue = job.get('_event_queue')
    if not event_queue:
        return jsonify({'error': 'Event queue not available'}), 400

    data = request.json
    event_queue.put(data)
    return jsonify({'ok': True})


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


@app.route('/debug-dom/<job_id>')
def debug_dom(job_id):
    """Debug: dump DOM structure of reviews section from an active job."""
    if job_id not in scrape_jobs:
        return jsonify({'error': 'Job not found'}), 404

    job = scrape_jobs[job_id]
    page = job.get('_page')
    if not page or job.get('_browser_closed'):
        return jsonify({'error': 'No active browser'}), 400

    try:
        dom_info = page.evaluate("""() => {
            const info = {
                title: document.title,
                url: window.location.href,
                rating_elements: 0,
                review_containers: [],
                pagination_html: '',
                all_aria_labels: [],
            };

            // Find rating elements
            const ratings = document.querySelectorAll('[aria-label*="Rating"]');
            info.rating_elements = ratings.length;

            // Get first 3 review container HTML samples
            ratings.forEach((el, i) => {
                if (i >= 3) return;
                let container = el;
                for (let j = 0; j < 10; j++) {
                    container = container.parentElement;
                    if (!container) break;
                    if (container.innerText && container.innerText.length > 50) break;
                }
                if (container) {
                    info.review_containers.push({
                        index: i,
                        outerHTML: container.outerHTML.substring(0, 2000),
                        innerText: container.innerText.substring(0, 500),
                    });
                }
            });

            // Find pagination
            const paginations = document.querySelectorAll('[class*="pagination"], [class*="Pagination"], [class*="pager"], nav');
            paginations.forEach((p, i) => {
                if (i < 2) {
                    info.pagination_html += p.outerHTML.substring(0, 1500) + '\\n---\\n';
                }
            });

            // Also check for numbered page buttons
            const pageButtons = document.querySelectorAll('button, [role="button"]');
            const pageNums = [];
            pageButtons.forEach(btn => {
                const text = btn.innerText?.trim();
                if (/^\\d+$/.test(text) && parseInt(text) > 0 && parseInt(text) < 200) {
                    pageNums.push({text, classes: btn.className, ariaLabel: btn.getAttribute('aria-label')});
                }
            });
            info.page_number_buttons = pageNums.slice(0, 20);

            // Check for any "next" or arrow elements
            const allEls = document.querySelectorAll('*');
            const nextEls = [];
            allEls.forEach(el => {
                const aria = el.getAttribute('aria-label') || '';
                const text = el.innerText?.trim() || '';
                if ((aria.toLowerCase().includes('next') || text === '>' || text === '›' || text === '»' || text === 'Next')
                    && el.getBoundingClientRect().width > 0) {
                    nextEls.push({
                        tag: el.tagName,
                        text: text.substring(0, 50),
                        aria: aria,
                        classes: el.className?.substring?.(0, 100) || '',
                        rect: el.getBoundingClientRect(),
                    });
                }
            });
            info.next_elements = nextEls.slice(0, 10);

            return info;
        }""")

        return jsonify(dom_info)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/health')
def health():
    """Health check endpoint."""
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'true').lower() == 'true'
    app.run(debug=debug, host='0.0.0.0', port=port, threaded=True)
