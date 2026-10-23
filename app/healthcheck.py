"""Container health probe: exit 0 iff /health/ready answers 200."""

import sys
import urllib.request

try:
    sys.exit(0 if urllib.request.urlopen("http://localhost:8000/health/ready", timeout=3).status == 200 else 1)
except Exception:
    sys.exit(1)
