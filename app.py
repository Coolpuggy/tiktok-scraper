"""
TikTok Shop Review Scraper - Web Interface
Flask app with real-time progress updates
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
CORS(app)  # Enable CORS for Framly to call this API

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
    """Scrape reviews and update progress in real-time."""
    job = scrape_jobs[job_id]
    job['status'] = 'starting'
    job['message'] = 'Starting browser...'

    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.chrome.options import Options
        from webdriver_manager.chrome import ChromeDriverManager
    except ImportError:
        job['status'] = 'error'
        job['message'] = 'Selenium not installed. Run: pip install selenium webdriver-manager'
        return

    # Setup Chrome - configured for both local and Railway deployment
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-software-rasterizer")
    chrome_options.add_argument("--remote-debugging-port=9222")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option('useAutomationExtension', False)
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    # Check if running in Docker/Railway (Chrome installed at system level)
    chrome_bin = os.environ.get('CHROME_BIN') or os.environ.get('GOOGLE_CHROME_BIN')
    if chrome_bin:
        chrome_options.binary_location = chrome_bin

    try:
        # Try to use system Chrome first (for Railway), fall back to webdriver-manager
        try:
            from selenium.webdriver.chrome.service import Service as ChromeService
            driver = webdriver.Chrome(options=chrome_options)
        except Exception:
            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=chrome_options)
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    except Exception as e:
        job['status'] = 'error'
        job['message'] = f'Failed to start browser: {str(e)}'
        return

    reviews = []
    seen_reviews = set()
    product_info = {'title': '', 'image': ''}

    try:
        job['status'] = 'loading'
        job['message'] = 'Loading product page...'
        driver.get(product_url)
        time.sleep(3)  # Reduced from 5s

        # Extract product title and image
        job['message'] = 'Extracting product info...'
        product_info = driver.execute_script("""
            let title = '';
            let image = '';

            // Try to get title
            const titleEl = document.querySelector('h1') ||
                           document.querySelector('[class*="title"]') ||
                           document.querySelector('[class*="Title"]');
            if (titleEl) title = titleEl.innerText.trim().split('\\n')[0];

            // Try to get main product image
            const imgEl = document.querySelector('[class*="ProductImage"] img') ||
                         document.querySelector('[class*="product-image"] img') ||
                         document.querySelector('[class*="gallery"] img') ||
                         document.querySelector('[class*="slider"] img') ||
                         document.querySelector('img[class*="product"]');
            if (imgEl) image = imgEl.src || imgEl.getAttribute('data-src') || '';

            // Fallback: get any large image on the page
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
        """) or {'title': '', 'image': ''}

        job['product_title'] = product_info.get('title', '')
        job['product_image'] = product_info.get('image', '')

        # Scroll to reviews
        job['message'] = 'Finding reviews section...'
        for i in range(5):
            driver.execute_script(f"window.scrollTo(0, document.body.scrollHeight * {0.3 + i*0.15});")
            time.sleep(0.4)  # Reduced from 0.8s

        driver.execute_script("""
            const ratingEl = document.querySelector('[aria-label*="Rating:"][aria-label*="out of 5 stars"]');
            if (ratingEl) ratingEl.scrollIntoView({block: 'center', behavior: 'smooth'});
        """)
        time.sleep(1)  # Reduced from 2s

        # Scroll to bottom for pagination
        for _ in range(10):
            driver.execute_script("window.scrollBy(0, 800);")
            time.sleep(0.2)  # Reduced from 0.4s

        job['status'] = 'scraping'
        current_page = 1

        while current_page <= max_pages:
            job['current_page'] = current_page
            job['max_pages'] = max_pages
            job['message'] = f'Scraping page {current_page}...'
            job['progress'] = int((current_page / max_pages) * 100)

            # Extract reviews via JavaScript
            review_data = driver.execute_script("""
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

                        // Extract item/variant info (Color, Size, etc.)
                        let itemVariant = '';
                        const lines = fullText.split('\\n');
                        for (let i = 0; i < lines.length; i++) {
                            const trimmed = lines[i].trim();
                            // Look for "Item:" label and get the next line as the variant
                            if (trimmed === 'Item:' || trimmed === 'Item' || trimmed.toLowerCase() === 'item:') {
                                // Get next non-empty line as the variant value
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
                            // Also handle "Item: Value" on same line
                            if (/^Item:\\s*.+/i.test(trimmed)) {
                                itemVariant = trimmed.replace(/^Item:\\s*/i, '');
                                break;
                            }
                            // Look for lines like "Color: Black" or "Size: M"
                            if (/^(Color|Size|Variant|Style):/i.test(trimmed)) {
                                itemVariant = trimmed;
                                break;
                            }
                            // Also catch variant patterns like "Black-Carrying Handle" or "Luxury Black-Built-in Handle"
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
                            // Skip if it's the item variant we already captured
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
            """)

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

            # Click Next button - prioritize the right arrow/chevron since page numbers slide
            driver.execute_script("window.scrollBy(0, 400);")
            time.sleep(0.8)

            clicked = False
            retry_count = 0
            max_retries = 3
            next_page_num = current_page + 1

            while not clicked and retry_count < max_retries:
                clicked = driver.execute_script(f"""
                    const nextPageNum = {next_page_num};
                    const currentPageNum = {current_page};

                    // Helper to click reliably
                    function clickEl(el) {{
                        el.scrollIntoView({{block: 'center'}});
                        el.click();
                        el.dispatchEvent(new MouseEvent('click', {{bubbles: true, cancelable: true, view: window}}));
                        return true;
                    }}

                    // Find the pagination container first - look for elements with page numbers
                    let paginationContainer = null;
                    const allElements = document.querySelectorAll('button, a, span, div, li, [role="button"]');

                    // Find where page numbers are to locate the pagination area
                    for (const el of allElements) {{
                        const text = el.innerText?.trim();
                        if (text === String(currentPageNum) || text === '1') {{
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0 && rect.top > 300) {{
                                // Found a page number, get its parent container
                                paginationContainer = el.parentElement?.parentElement?.parentElement;
                                break;
                            }}
                        }}
                    }}

                    // Strategy 1 (PRIORITY): Find the LAST clickable element in pagination (right arrow/next)
                    // TikTok pagination typically has: [<] [1] [2] [3] ... [>]
                    // The last clickable element is usually the "next" arrow
                    if (paginationContainer) {{
                        const paginationButtons = paginationContainer.querySelectorAll('button, a, span[role="button"], li, [role="button"]');
                        const visibleButtons = [];
                        for (const btn of paginationButtons) {{
                            const rect = btn.getBoundingClientRect();
                            if (rect.width > 10 && rect.height > 10) {{
                                visibleButtons.push(btn);
                            }}
                        }}
                        // The rightmost button (last one) should be "next"
                        if (visibleButtons.length > 0) {{
                            const lastBtn = visibleButtons[visibleButtons.length - 1];
                            const text = lastBtn.innerText?.trim();
                            // Make sure it's not a page number (it should be arrow or empty for SVG)
                            if (!text || text === '›' || text === '»' || text === '→' || text === '>' || !/^\\d+$/.test(text)) {{
                                return clickEl(lastBtn);
                            }}
                        }}
                    }}

                    // Strategy 2: Look for SVG-based arrow buttons (common in modern UIs)
                    // Find buttons that contain SVG with a rightward-pointing path
                    const buttonsWithSvg = document.querySelectorAll('button, [role="button"]');
                    for (const btn of buttonsWithSvg) {{
                        const svg = btn.querySelector('svg');
                        if (svg) {{
                            const rect = btn.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0 && rect.top > 300) {{
                                // Check if this button is to the RIGHT of page numbers
                                // by checking if there's a page number element to its left
                                const btnCenter = rect.left + rect.width / 2;
                                let hasPageNumToLeft = false;
                                for (const el of allElements) {{
                                    const t = el.innerText?.trim();
                                    if (/^\\d+$/.test(t)) {{
                                        const r = el.getBoundingClientRect();
                                        if (r.width > 0 && r.left < btnCenter && Math.abs(r.top - rect.top) < 50) {{
                                            hasPageNumToLeft = true;
                                            break;
                                        }}
                                    }}
                                }}
                                if (hasPageNumToLeft) {{
                                    return clickEl(btn);
                                }}
                            }}
                        }}
                    }}

                    // Strategy 3: Look for right arrow/chevron text symbols
                    for (const el of allElements) {{
                        const text = el.innerText?.trim();
                        if (text === '›' || text === '»' || text === '→' || text === '>') {{
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0 && rect.top > 300) {{
                                return clickEl(el);
                            }}
                        }}
                    }}

                    // Strategy 4: Click next page number if visible (sliding window shows nearby pages)
                    for (const el of allElements) {{
                        const text = el.innerText?.trim();
                        if (text === String(nextPageNum)) {{
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0 && rect.top > 300) {{
                                return clickEl(el);
                            }}
                        }}
                    }}

                    // Strategy 5: Find buttons/links with aria-label containing "next"
                    for (const el of allElements) {{
                        const ariaLabel = (el.getAttribute('aria-label') || '').toLowerCase();
                        if (ariaLabel.includes('next') && !ariaLabel.includes('prev')) {{
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0 && rect.top > 300) {{
                                return clickEl(el);
                            }}
                        }}
                    }}

                    // Strategy 6: Find any visible page number higher than current
                    for (const el of allElements) {{
                        const text = el.innerText?.trim();
                        if (/^\\d+$/.test(text)) {{
                            const pageNum = parseInt(text);
                            if (pageNum === currentPageNum + 1 || pageNum === currentPageNum + 2) {{
                                const rect = el.getBoundingClientRect();
                                if (rect.width > 0 && rect.height > 0 && rect.top > 300) {{
                                    return clickEl(el);
                                }}
                            }}
                        }}
                    }}

                    return false;
                """)

                if not clicked:
                    retry_count += 1
                    time.sleep(0.5)
                    driver.execute_script("window.scrollBy(0, 200);")

            if clicked:
                time.sleep(1.5)  # Wait for new page to load
                current_page += 1
            else:
                # Try scroll-based or stop
                job['message'] = 'No more pages found'
                break

        job['status'] = 'complete'
        job['message'] = f'Done! Found {len(reviews)} reviews'
        job['progress'] = 100

    except Exception as e:
        job['status'] = 'error'
        job['message'] = f'Error: {str(e)}'
    finally:
        try:
            driver.quit()
        except:
            pass


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
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'true').lower() == 'true'
    app.run(debug=debug, host='0.0.0.0', port=port, threaded=True)
