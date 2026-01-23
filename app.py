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

                        // Check for any rating-like elements (TikTok may change aria labels)
                        const stars = document.querySelectorAll('[aria-label*="Rating"]');
                        if (stars.length >= 3) return 'ratings';

                        // Check for product title/price (we're past CAPTCHA)
                        const h1 = document.querySelector('h1');
                        const hasPrice = document.querySelector('[class*="price"], [class*="Price"]');
                        if (h1 && h1.innerText.trim().length > 5 && hasPrice) {
                            const title = document.title.toLowerCase();
                            if (!title.includes('security') && !title.includes('verify') && !title.includes('captcha')) {
                                return 'product';
                            }
                        }

                        // Check for add-to-cart button (definitely past CAPTCHA)
                        const addToCart = document.querySelector('[class*="AddToCart"], [class*="add-to-cart"], button[aria-label*="Add to cart"]');
                        if (addToCart) return 'product';

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

            # Scroll down to load the reviews section
            job['message'] = 'Finding reviews section...'
            for i in range(8):
                page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {0.2 + i*0.1})")
                page.wait_for_timeout(500)

            # Try to scroll directly to the reviews/rating section
            page.evaluate("""() => {
                // Look for review section headers or rating elements
                const targets = [
                    document.querySelector('[aria-label*="Rating:"][aria-label*="out of 5 stars"]'),
                    document.querySelector('[class*="review"], [class*="Review"]'),
                    document.querySelector('[id*="review"], [id*="Review"]'),
                ];
                for (const el of targets) {
                    if (el) {
                        el.scrollIntoView({block: 'start', behavior: 'smooth'});
                        return;
                    }
                }
            }""")
            page.wait_for_timeout(1500)

            # Scroll a bit more to ensure pagination loads
            for _ in range(5):
                page.evaluate("window.scrollBy(0, 600)")
                page.wait_for_timeout(300)

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

            # Collect DOM debug info on first page
            try:
                dom_debug = page.evaluate("""() => {
                    const info = {};
                    const ratings = document.querySelectorAll('[aria-label*="Rating"]');
                    info.total_rating_elements = ratings.length;

                    const ratings_5star = document.querySelectorAll('[aria-label*="Rating:"][aria-label*="out of 5 stars"]');
                    info.rating_5star_elements = ratings_5star.length;

                    // Sample first 2 rating elements' parent text
                    info.samples = [];
                    ratings_5star.forEach((el, i) => {
                        if (i >= 2) return;
                        let container = el;
                        for (let j = 0; j < 10; j++) {
                            container = container.parentElement;
                            if (!container) break;
                            if (container.innerText && container.innerText.length > 50) break;
                        }
                        if (container) {
                            info.samples.push(container.innerText.substring(0, 300));
                        }
                    });

                    // Check pagination
                    info.pagination = [];
                    const allEls = document.querySelectorAll('button, a, [role="button"]');
                    allEls.forEach(el => {
                        const text = el.innerText?.trim();
                        if (text === 'Next' || text === '>' || text === '›' || /^\\d+$/.test(text)) {
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 0 && rect.top > 200) {
                                info.pagination.push({text, tag: el.tagName, top: Math.round(rect.top)});
                            }
                        }
                    });

                    return info;
                }""")
                print(f"[{job_id}] DOM Debug: {json.dumps(dom_debug, indent=2)}")
                job['_dom_debug'] = dom_debug
            except Exception as debug_err:
                print(f"[{job_id}] DOM debug error: {debug_err}")

            current_page = 1

            while current_page <= max_pages:
                job['current_page'] = current_page
                job['max_pages'] = max_pages
                job['message'] = f'Scraping page {current_page}...'
                job['progress'] = int((current_page / max_pages) * 100)

                # Wait for reviews to be present on the page
                for wait_try in range(6):  # up to 3 seconds
                    rc = page.evaluate("""() => {
                        return document.querySelectorAll('[aria-label*="Rating:"][aria-label*="out of 5 stars"]').length;
                    }""")
                    if rc >= 2:
                        break
                    page.wait_for_timeout(500)

                # Extract reviews from current page
                review_data = page.evaluate("""() => {
                    const reviews = [];
                    const debug = {found_ratings: 0, extracted: 0, method: ''};

                    // Method 1: Find rating elements with aria-label
                    const ratingElements = document.querySelectorAll('[aria-label*="Rating:"][aria-label*="out of 5 stars"]');
                    debug.found_ratings = ratingElements.length;

                    ratingElements.forEach(ratingEl => {
                        try {
                            const ariaLabel = ratingEl.getAttribute('aria-label');
                            const ratingMatch = ariaLabel.match(/Rating:\\s*(\\d+(?:\\.\\d+)?)\\s*out of 5/);
                            const rating = ratingMatch ? Math.round(parseFloat(ratingMatch[1])) : 0;

                            // Walk up to find the review container
                            // Look for a container that has multiple distinct sections
                            let container = ratingEl;
                            for (let i = 0; i < 12; i++) {
                                container = container.parentElement;
                                if (!container) break;
                                const text = container.innerText || '';
                                // A review container typically has >50 chars and contains a date or username
                                if (text.length > 40 && (
                                    /\\d{4}-\\d{2}-\\d{2}/.test(text) ||
                                    /[A-Za-z0-9]\\*+[A-Za-z0-9]/.test(text) ||
                                    /\\d+\\/\\d+\\/\\d+/.test(text) ||
                                    /ago/.test(text)
                                )) break;
                            }

                            if (!container) return;
                            const fullText = container.innerText || '';
                            const lines = fullText.split('\\n').map(l => l.trim()).filter(l => l);

                            // Extract username - multiple patterns
                            let username = '';
                            // Pattern 1: masked username like J***n or a***2
                            const maskedMatch = fullText.match(/([A-Za-z0-9]\\*{2,}[A-Za-z0-9])/);
                            if (maskedMatch) username = maskedMatch[1];
                            // Pattern 2: short username-like string at start
                            if (!username && lines.length > 0) {
                                const firstLine = lines[0];
                                if (firstLine.length < 30 && firstLine.length > 1 && !/^\\d/.test(firstLine) && !firstLine.includes('Rating')) {
                                    username = firstLine;
                                }
                            }
                            if (!username) username = 'Anonymous';

                            // Extract date
                            let date = '';
                            const dateMatch = fullText.match(/(\\d{4}-\\d{2}-\\d{2})/);
                            if (dateMatch) {
                                date = dateMatch[1];
                            } else {
                                const relDateMatch = fullText.match(/(\\d+\\s*(?:day|week|month|year|hour|min)s?\\s*ago)/i);
                                if (relDateMatch) date = relDateMatch[1];
                            }

                            // Extract item variant
                            let itemVariant = '';
                            for (let i = 0; i < lines.length; i++) {
                                const line = lines[i];
                                if (/^Item:/i.test(line)) {
                                    itemVariant = line.replace(/^Item:\\s*/i, '');
                                    if (!itemVariant && lines[i+1]) itemVariant = lines[i+1];
                                    break;
                                }
                                if (/^(Color|Size|Variant|Style):/i.test(line)) {
                                    itemVariant = line;
                                    break;
                                }
                            }

                            // Extract review text - find the longest meaningful line
                            // that isn't a username, date, or metadata
                            let reviewText = '';
                            const skipPatterns = [
                                /^(Verified|Helpful|Reply|Report|Like|Share)/i,
                                /^(Item:|Color:|Size:|Variant:|Style:)/i,
                                /^\\d{4}-\\d{2}-\\d{2}$/,
                                /^\\d+\\s*(day|week|month|year|hour|min)/i,
                                /^\\d+$/,
                                /^Rating:/,
                                /^[A-Za-z0-9]\\*{2,}[A-Za-z0-9]$/,
                            ];

                            for (const line of lines) {
                                if (line.length < 5) continue;
                                if (line === username) continue;
                                if (line === itemVariant) continue;
                                if (line === date) continue;
                                let skip = false;
                                for (const pat of skipPatterns) {
                                    if (pat.test(line)) { skip = true; break; }
                                }
                                if (skip) continue;
                                // Take the longest line as review text
                                if (line.length > reviewText.length) {
                                    reviewText = line;
                                }
                            }

                            // Accept reviews with any text (even short ones like "Great!")
                            if (reviewText.length >= 1 || rating > 0) {
                                reviews.push({
                                    username,
                                    rating,
                                    review_text: reviewText || '(no text)',
                                    date,
                                    item_variant: itemVariant
                                });
                            }
                        } catch (e) {}
                    });

                    debug.extracted = reviews.length;
                    debug.method = 'aria-label ratings';
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

                # Save the full page HTML on the first page for debugging
                if current_page == 1:
                    try:
                        full_html = page.evaluate("() => document.documentElement.outerHTML")
                        job['_page_html'] = full_html
                        # Also save to file so it persists
                        with open('/tmp/last_page.html', 'w', encoding='utf-8') as f:
                            f.write(full_html)
                        print(f"[{job_id}] Saved full page HTML ({len(full_html)} chars) to /tmp/last_page.html")
                    except Exception as html_err:
                        print(f"[{job_id}] Failed to save page HTML: {html_err}")

                # Scroll down to ensure pagination is in view
                page.evaluate("""() => {
                    // Find the last rating element and scroll past it
                    const ratings = document.querySelectorAll('[aria-label*="Rating:"][aria-label*="out of 5 stars"]');
                    if (ratings.length > 0) {
                        const last = ratings[ratings.length - 1];
                        last.scrollIntoView({block: 'start'});
                        window.scrollBy(0, 300);
                    } else {
                        window.scrollBy(0, 600);
                    }
                }""")
                page.wait_for_timeout(1000)

                next_page = current_page + 1

                # Use page.locator to find and click the Next button with Playwright's
                # built-in waiting and actionability checks
                clicked = False
                click_method = ''

                try:
                    # Try clicking a button/link containing "Next" text
                    next_btn = page.locator('button:has-text("Next"), a:has-text("Next")').first
                    if next_btn.count() > 0 and next_btn.is_visible():
                        next_btn.scroll_into_view_if_needed()
                        next_btn.click()
                        clicked = True
                        click_method = 'locator_next_text'
                except Exception as e:
                    print(f"[{job_id}] Locator 'Next' failed: {e}")

                if not clicked:
                    try:
                        # Try clicking the page number directly
                        page_btn = page.locator(f'button:has-text("{next_page}"), a:has-text("{next_page}")').first
                        if page_btn.count() > 0 and page_btn.is_visible():
                            page_btn.scroll_into_view_if_needed()
                            page_btn.click()
                            clicked = True
                            click_method = f'locator_page_{next_page}'
                    except Exception as e:
                        print(f"[{job_id}] Locator page number failed: {e}")

                if not clicked:
                    try:
                        # Try aria-label approach
                        next_aria = page.locator('[aria-label*="next" i], [aria-label*="Next"]').first
                        if next_aria.count() > 0 and next_aria.is_visible():
                            next_aria.scroll_into_view_if_needed()
                            next_aria.click()
                            clicked = True
                            click_method = 'locator_aria_next'
                    except Exception as e:
                        print(f"[{job_id}] Locator aria next failed: {e}")

                if not clicked:
                    # Fallback: use evaluate to find and click
                    click_result = page.evaluate(f"""(targetPage) => {{
                        const allClickable = document.querySelectorAll('button, a, [role="button"]');

                        // Find anything with "next" in text
                        for (const el of allClickable) {{
                            const text = (el.innerText || el.textContent || '').toLowerCase().trim();
                            if (text.includes('next')) {{
                                const rect = el.getBoundingClientRect();
                                if (rect.width > 0 && rect.height > 0) {{
                                    el.scrollIntoView({{block: 'center'}});
                                    el.click();
                                    return 'js_next: ' + text.substring(0, 30);
                                }}
                            }}
                        }}

                        // Find page number
                        for (const el of allClickable) {{
                            const text = (el.innerText || '').trim();
                            if (text === String(targetPage)) {{
                                const rect = el.getBoundingClientRect();
                                if (rect.width > 0 && rect.width < 100 && rect.height > 0) {{
                                    el.scrollIntoView({{block: 'center'}});
                                    el.click();
                                    return 'js_page_number';
                                }}
                            }}
                        }}

                        // Find arrow characters
                        for (const el of allClickable) {{
                            const text = (el.innerText || '').trim();
                            if (text === '>' || text === '›' || text === '»' || text === '→') {{
                                const rect = el.getBoundingClientRect();
                                if (rect.width > 0 && rect.height > 0) {{
                                    el.scrollIntoView({{block: 'center'}});
                                    el.click();
                                    return 'js_arrow: ' + text;
                                }}
                            }}
                        }}

                        return null;
                    }}""", next_page)

                    if click_result:
                        clicked = True
                        click_method = click_result

                if clicked:
                    print(f"[{job_id}] Pagination click: {click_method} -> page {next_page}")
                    # Wait for new reviews to load
                    page.wait_for_timeout(2000)
                    # Verify page actually changed by checking if new reviews appeared
                    for wait_attempt in range(6):
                        rating_count = page.evaluate("""() => {
                            return document.querySelectorAll('[aria-label*="Rating:"][aria-label*="out of 5 stars"]').length;
                        }""")
                        if rating_count >= 2:
                            print(f"[{job_id}] Page loaded: {rating_count} ratings found")
                            break
                        page.wait_for_timeout(500)
                    page.wait_for_timeout(1000)
                    current_page += 1
                else:
                    print(f"[{job_id}] No pagination found after page {current_page}")
                    # Save HTML for debugging on failure too
                    try:
                        full_html = page.evaluate("() => document.documentElement.outerHTML")
                        job['_page_html'] = full_html
                    except Exception:
                        pass
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
    """Return stored DOM debug info from the scraping loop."""
    if job_id not in scrape_jobs:
        return jsonify({'error': 'Job not found'}), 404

    job = scrape_jobs[job_id]
    return jsonify({
        'dom_debug': job.get('_dom_debug', {}),
        'status': job.get('status'),
        'current_page': job.get('current_page'),
        'review_count': job.get('review_count'),
    })


@app.route('/debug-html/<job_id>')
def debug_html(job_id):
    """Return the saved page HTML for debugging pagination."""
    html = ''
    if job_id in scrape_jobs:
        html = scrape_jobs[job_id].get('_page_html', '')

    # Fallback: read from file
    if not html:
        try:
            with open('/tmp/last_page.html', 'r', encoding='utf-8') as f:
                html = f.read()
        except Exception:
            pass

    if not html:
        return Response('No HTML saved yet', status=404)

    return Response(html, mimetype='text/html')


@app.route('/debug-html-raw')
def debug_html_raw():
    """Return the last saved page HTML (no job_id needed)."""
    try:
        with open('/tmp/last_page.html', 'r', encoding='utf-8') as f:
            html = f.read()
        return Response(html, mimetype='text/html')
    except Exception:
        return Response('No HTML saved yet', status=404)


@app.route('/health')
def health():
    """Health check endpoint."""
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'true').lower() == 'true'
    app.run(debug=debug, host='0.0.0.0', port=port, threaded=True)
