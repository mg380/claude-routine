#!/bin/bash
# Fetch a URL with a browser-like User-Agent to bypass bot-detection CDNs.
# Usage: bash curl_browser.sh <url>
curl -s --max-time 30 -L \
  -A "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36" \
  "$1"
