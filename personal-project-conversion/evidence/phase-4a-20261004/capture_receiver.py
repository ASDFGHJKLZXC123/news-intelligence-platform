"""Loopback-only UI form to retain captured synthetic browser proof."""
import base64
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys
from urllib.parse import parse_qs

OUT=Path(__file__).resolve().parent
ALLOWED={'browser-final-retry.jpg','browser-final-retry.txt','browser-today-saved.jpg','browser-today-saved.txt','browser-briefs.jpg','browser-briefs.txt','browser-saved-restart.jpg','browser-saved-restart.txt','browser-retry.jpg','browser-retry.txt'}
class Receiver(BaseHTTPRequestHandler):
    def do_GET(self):
        html='<title>Owned synthetic capture</title><form method="post"><label>Filename<select name="filename">'+''.join('<option>'+n+'</option>' for n in sorted(ALLOWED))+'</select></label><label>Captured evidence<textarea name="capture"></textarea></label><button>Save captured evidence</button></form>'
        self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(html.encode())
    def do_POST(self):
        size=int(self.headers.get('Content-Length','0'))
        if not 0<size<4000000:
            self.send_error(413);return
        values=parse_qs(self.rfile.read(size).decode());name=values.get('filename',[''])[0]
        if name not in ALLOWED:
            self.send_error(404);return
        path=OUT/name
        if path.exists():
            self.send_error(409);return
        content=values.get('capture',[''])[0]
        payload=base64.b64decode(content,validate=True) if name.endswith('.jpg') else content.encode()
        with path.open('xb') as handle:handle.write(payload)
        self.send_response(201);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(('<p>Saved '+name+'</p><a href="/">Capture another</a>').encode())
HTTPServer(('127.0.0.1',int(sys.argv[1])),Receiver).serve_forever()
