FROM python:3.11-slim

# Install system dependencies needed by Playwright
RUN apt-get update && apt-get install -y \
    wget \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browser binaries (Chromium only)
# Note: For Scraping Browser we connect remotely, but install locally for fallback
RUN playwright install chromium && playwright install-deps chromium

# Copy app code
COPY app.py .
COPY templates/ templates/

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PORT=5000

# Expose port
EXPOSE 5000

# Run the app with gunicorn
CMD ["/bin/sh", "-c", "gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 300 app:app"]
