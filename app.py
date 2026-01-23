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
    chrome_options.add_argument("--disable-software-rasterizer")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option('useAutomationExtension', False)
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    chrome_options.add_argument("--ignore-certificate-errors")

    # Configure residential proxy if available (Bright Data format)
    # BRIGHT_DATA_PROXY format: username:password@host:port
    proxy_url = os.environ.get('BRIGHT_DATA_PROXY')
    local_proxy_port = None
    if proxy_url:
        # Parse proxy URL
        if '@' in proxy_url:
            creds_part, host_part = proxy_url.rsplit('@', 1)
        else:
            creds_part, host_part = None, proxy_url

        # Use port 22225 for CONNECT support
        host_part = host_part.replace(':33335', ':22225')

        # Start a local forwarding proxy that adds auth headers
        import socket
        import select
        import base64

        # Find a free port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('127.0.0.1', 0))
        local_proxy_port = sock.getsockname()[1]
        sock.close()

        # Build auth header
        auth_header = ''
        if creds_part:
            auth_b64 = base64.b64encode(creds_part.encode()).decode()
            auth_header = f'Proxy-Authorization: Basic {auth_b64}\r\n'

        upstream_host, upstream_port = host_part.split(':')
        upstream_port = int(upstream_port)

        def run_local_proxy(listen_port, up_host, up_port, auth_hdr):
            """Simple forwarding proxy that adds auth to upstream CONNECT."""
            import socketserver

            class ProxyHandler(socketserver.BaseRequestHandler):
                def handle(self):
                    upstream = None
                    try:
                        # Read Chrome's CONNECT request
                        data = b''
                        while b'\r\n\r\n' not in data:
                            chunk = self.request.recv(4096)
                            if not chunk:
                                return
                            data += chunk

                        header_end = data.index(b'\r\n\r\n')
                        request_line = data[:data.index(b'\r\n')].decode()

                        # Connect to upstream Bright Data proxy
                        upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        upstream.settimeout(60)
                        upstream.connect((up_host, up_port))

                        if request_line.startswith('CONNECT'):
                            # HTTPS tunnel request
                            # Forward CONNECT with auth header to upstream
                            target = request_line.split(' ')[1]
                            connect_req = (
                                f'CONNECT {target} HTTP/1.1\r\n'
                                f'Host: {target}\r\n'
                                f'{auth_hdr}'
                                f'\r\n'
                            )
                            upstream.sendall(connect_req.encode())

                            # Read upstream's response to CONNECT
                            resp = b''
                            while b'\r\n\r\n' not in resp:
                                chunk = upstream.recv(4096)
                                if not chunk:
                                    return
                                resp += chunk

                            # Check if upstream accepted
                            status_line = resp[:resp.index(b'\r\n')].decode()
                            if '200' in status_line:
                                # Tell Chrome the tunnel is established
                                self.request.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
                            else:
                                # Forward error to Chrome
                                self.request.sendall(resp)
                                return
                        else:
                            # Regular HTTP request - inject auth and forward
                            headers = data[:header_end].decode('utf-8', errors='replace')
                            body = data[header_end + 4:]
                            lines = headers.split('\r\n')
                            lines.insert(1, auth_hdr.rstrip('\r\n'))
                            modified = '\r\n'.join(lines) + '\r\n\r\n'
                            upstream.sendall(modified.encode() + body)

                        # Bidirectional forwarding
                        self.request.setblocking(False)
                        upstream.setblocking(False)

                        while True:
                            readable, _, _ = select.select(
                                [self.request, upstream], [], [], 60
                            )
                            if not readable:
                                break
                            for s in readable:
                                try:
                                    chunk = s.recv(65536)
                                    if not chunk:
                                        return
                                    if s is self.request:
                                        upstream.sendall(chunk)
                                    else:
                                        self.request.sendall(chunk)
                                except (ConnectionError, OSError):
                                    return
                    except Exception as e:
                        print(f"[proxy] Error: {e}")
                    finally:
                        try:
                            if upstream:
                                upstream.close()
                        except:
                            pass

            class ThreadedServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
                allow_reuse_address = True
                daemon_threads = True

            server = ThreadedServer(('127.0.0.1', listen_port), ProxyHandler)
            server.serve_forever()

        # Start local proxy in background thread
        proxy_thread = threading.Thread(
            target=run_local_proxy,
            args=(local_proxy_port, upstream_host, upstream_port, auth_header),
            daemon=True
        )
        proxy_thread.start()
        time.sleep(0.5)  # Let it start

        chrome_options.add_argument(f"--proxy-server=http://127.0.0.1:{local_proxy_port}")
        print(f"[{job_id}] Local proxy on :{local_proxy_port} -> {host_part}")
    else:
        print(f"[{job_id}] No proxy configured (set BRIGHT_DATA_PROXY env var)")

    # Check if running in Docker/Railway (Chrome installed at system level)
    chrome_bin = os.environ.get('CHROME_BIN') or os.environ.get('GOOGLE_CHROME_BIN')
    if chrome_bin:
        chrome_options.binary_location = chrome_bin

    try:
        try:
            driver = webdriver.Chrome(options=chrome_options)
        except Exception:
            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=chrome_options)

        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    except Exception as e:
        job['status'] = 'error'
        job['message'] = f'Failed to start browser: {str(e)}'
        import traceback
        traceback.print_exc()
        return

    reviews = []
    seen_reviews = set()
    product_info = {'title': '', 'image': ''}

    try:
        job['status'] = 'loading'
        job['message'] = 'Loading product page...'
        print(f"[{job_id}] Loading URL: {product_url}")
        driver.get(product_url)
        time.sleep(4)  # Wait for page to load

        # Log page title to verify page loaded
        page_title = driver.title
        print(f"[{job_id}] Page loaded. Title: {page_title}")

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
        print(f"[{job_id}] Product title: {job['product_title'][:50] if job['product_title'] else 'Not found'}")

        # Scroll to reviews
        job['message'] = 'Finding reviews section...'
        print(f"[{job_id}] Scrolling to find reviews...")
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
        print(f"[{job_id}] Starting to scrape reviews...")

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
                # Original working approach - focus on Next button and arrows, not page numbers
                clicked = driver.execute_script("""
                    const allElements = document.querySelectorAll('button, a, span, div, li, [role="button"]');

                    // Strategy 1: Look for pagination buttons with "Next" text
                    for (const el of allElements) {
                        const text = el.innerText?.trim();
                        if (text === 'Next' || text === 'Next →' || text === 'Next→' || text === 'next') {
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0 && rect.top > 200) {
                                el.scrollIntoView({block: 'center'});
                                el.click();
                                el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
                                return true;
                            }
                        }
                    }

                    // Strategy 2: Look for arrow symbols
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

                    // Strategy 3: Look for pagination container and find active page, then click next
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

                    // Strategy 4: Look for SVG arrow icons in buttons
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

                    // Strategy 5: Find by aria-label containing "next"
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
        print(f"[{job_id}] ERROR: {str(e)}")
        import traceback
        traceback.print_exc()
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
    return jsonify({
        'status': 'ok',
        'proxy_configured': bool(os.environ.get('BRIGHT_DATA_PROXY'))
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'true').lower() == 'true'
    app.run(debug=debug, host='0.0.0.0', port=port, threaded=True)
