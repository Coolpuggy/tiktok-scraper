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
RUN playwright install chromium && playwright install-deps chromium

# Copy app code
COPY app.py .

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PORT=5000

# Expose port
EXPOSE 5000

# Use gevent worker for proper SSE streaming support
CMD ["/bin/sh", "-c", "gunicorn --bind 0.0.0.0:$PORT --worker-class gthread --workers 1 --threads 8 --timeout 300 app:app"]
