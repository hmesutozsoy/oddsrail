"""Local product preview with an isolated, paper-only backend.

Run with the project's Python environment: python tools/preview_site.py
No production accounts, keys, or mail configuration are inherited.
"""
import atexit
import http.server
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
PORT = int(os.environ.get('ODDSRAIL_PREVIEW_PORT', '8898'))
BACKEND_PORT = PORT + 1
BACKEND = f'http://127.0.0.1:{BACKEND_PORT}'
DATA = ROOT / 'work' / 'preview-data'
DATA.mkdir(parents=True, exist_ok=True)
env = {k: v for k, v in os.environ.items() if not k.startswith(('ODDSRAIL_', 'POLYMARKET_', 'KALSHI_', 'OPENAI_', 'ANTHROPIC_'))}
env.update(ODDSRAIL_CLOUD_URL=BACKEND, ODDSRAIL_CLOUD_PORT=str(BACKEND_PORT),
           ODDSRAIL_CLOUD_DATA=str(DATA), ODDSRAIL_SITE_URL=f'http://127.0.0.1:{PORT}',
           PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1')
log = open(DATA / 'backend.log', 'a')
backend = subprocess.Popen([sys.executable, '-m', 'oddsrail.cloud.app'], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
atexit.register(backend.terminate)
for attempt in range(100):
    try:
        with urllib.request.urlopen(BACKEND + '/healthz', timeout=1) as response:
            if response.status == 200:
                break
    except Exception:
        if backend.poll() is not None:
            raise SystemExit('Backend stopped; inspect work/preview-data/backend.log')
        time.sleep(.2)
else:
    raise SystemExit('Backend startup timed out')

class Preview(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / 'site'), **kwargs)

    def proxy(self):
        path = self.path.removeprefix('/api')
        if not path.startswith('/') or path.startswith('//'):
            self.send_error(400)
            return
        headers = {'content-type': self.headers.get('content-type', 'application/json')}
        if self.headers.get('authorization'):
            headers['authorization'] = self.headers['authorization']
        length = int(self.headers.get('content-length', '0'))
        if length > 100000:
            self.send_error(413)
            return
        data = self.rfile.read(length) if length else None
        request = urllib.request.Request(BACKEND + path, data=data, headers=headers, method=self.command)
        try:
            response = urllib.request.urlopen(request, timeout=140)
        except urllib.error.HTTPError as exc:
            response = exc
        except Exception:
            self.send_error(502, 'Local API unavailable')
            return
        with response:
            content = response.read()
            self.send_response(response.status)
            self.send_header('Content-Type', response.headers.get('content-type', 'application/json'))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    def do_GET(self):
        if self.path.startswith('/api/'):
            return self.proxy()
        path = urlsplit(self.path).path
        resolved = Path(self.translate_path(path))
        if path.endswith('/'):
            resolved = resolved / 'index.html'
        elif not resolved.suffix:
            resolved = resolved.with_suffix('.html')
        if resolved.suffix == '.html' and resolved.is_file():
            content = resolved.read_text().replace('<head>', '<head><script>window.ODDSRAIL_API="/api";</script>', 1).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        return super().do_GET()

    def do_POST(self):
        if self.path.startswith('/api/'):
            return self.proxy()
        self.send_error(404)

    do_DELETE = do_POST

print(f'OddsRail preview: http://127.0.0.1:{PORT}', flush=True)
try:
    http.server.ThreadingHTTPServer(('127.0.0.1', PORT), Preview).serve_forever()
finally:
    backend.terminate()
    log.close()
