"""KAN-110 disposable-kind-only HTTP fixture; never deployed by production overlays."""
import http.server
import json
import os
import time
import urllib.error
import urllib.request


DELAY = 0


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global DELAY
        started = time.monotonic()
        if self.path.startswith('/delay/'):
            DELAY = int(self.path.removeprefix('/delay/'))
            status = 200
        elif self.path.startswith('/caller'):
            try:
                with urllib.request.urlopen('http://target:8080/health', timeout=1) as response:
                    status = response.status
            except (urllib.error.URLError, TimeoutError):
                status = 502
        else:
            time.sleep(DELAY / 1000)
            status = 200
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'elapsed_ms': round((time.monotonic() - started) * 1000, 3)}).encode())

    def log_message(self, format, *args):
        pass


http.server.ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
