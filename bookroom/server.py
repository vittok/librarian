"""Loopback-only HTTP interface, with per-session request authentication."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import urllib.parse
import webbrowser

from .core import Library

STATIC = Path(__file__).parent/'static'


def make_server(library, port=8765):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def reply(self, status, data, content_type='application/json'):
            raw = json.dumps(data).encode() if content_type=='application/json' else data
            self.send_response(status)
            self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(raw)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers(); self.wfile.write(raw)

        def authorized_host(self):
            return self.headers.get('Host') in {f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}

        def dispatch(self):
            if not self.authorized_host(): return self.reply(403,{'error':'Only local access is allowed'})
            parsed = urllib.parse.urlsplit(self.path)
            route = parsed.path
            if route.startswith('/api/'):
                if not secrets.compare_digest(self.headers.get('X-Bookroom-Token',''),token):
                    return self.reply(403,{'error':'Session expired; reload Bookroom'})
                origin = self.headers.get('Origin')
                if origin and origin not in {f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}'}:
                    return self.reply(403,{'error':'Cross-origin requests are not allowed'})
            try:
                if self.command=='GET':
                    query = urllib.parse.parse_qs(parsed.query)
                    def arg(key, default=''): return query.get(key,[default])[0]
                    if route=='/api/stats': return self.reply(200,library.stats())
                    if route=='/api/books': return self.reply(200,library.catalogue(arg('q'),arg('format'),arg('duplicates')=='1',int(arg('page','1'))))
                    if route=='/api/jobs': return self.reply(200,library.jobs())
                    if route=='/api/folders':
                        folder = Path(arg('path') or str(Path.home())).expanduser().resolve()
                        if not folder.is_dir(): raise ValueError('Folder does not exist')
                        children = sorted((p for p in folder.iterdir() if p.is_dir() and not p.name.startswith('.') and not p.is_symlink()),key=lambda p:p.name.casefold())
                        return self.reply(200,dict(path=str(folder),parent=str(folder.parent),children=[dict(name=p.name,path=str(p)) for p in children][:2000]))
                    if route=='/':
                        raw = (STATIC/'index.html').read_bytes().replace(b'__SESSION_TOKEN__',token.encode())
                        return self.reply(200,raw,'text/html; charset=utf-8')
                    if route in {'/app.js','/style.css'}:
                        content_type = 'text/javascript; charset=utf-8' if route.endswith('.js') else 'text/css; charset=utf-8'
                        return self.reply(200,(STATIC/route[1:]).read_bytes(),content_type)
                    return self.reply(404,{'error':'Not found'})
                if self.command=='POST' and route.startswith('/api/'):
                    length = int(self.headers.get('Content-Length','0'))
                    if length<=0 or length>1_000_000: raise ValueError('Invalid request size')
                    data = json.loads(self.rfile.read(length))
                    if not isinstance(data,dict): raise ValueError('Expected a JSON object')
                    if route=='/api/scan': result = library.scan(data.get('folder',''))
                    elif route=='/api/lookup': result = library.lookup(str(data.get('title','')),str(data.get('author','')))
                    elif route=='/api/metadata': result = library.update_metadata(int(data['id']),data)
                    elif route=='/api/plan': result = library.plan(data)
                    elif route=='/api/execute': result = library.execute(str(data.get('plan_id','')))
                    else: return self.reply(404,{'error':'Not found'})
                    return self.reply(200,result)
                return self.reply(405,{'error':'Method not allowed'})
            except (ValueError,KeyError,TypeError,OSError) as e: self.reply(400,{'error':str(e)})
            except Exception as e:
                print(f'Bookroom request failed: {e}',file=sys.stderr)
                self.reply(500,{'error':'Request failed. See the terminal for details.'})

        do_GET = dispatch
        do_POST = dispatch

    server = ThreadingHTTPServer(('127.0.0.1',port),Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description='Bookroom — a local ebook workshop')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--data-dir',type=Path,default=Path(os.environ.get('BOOKROOM_DATA_DIR',Path.home()/'.local'/'share'/'bookroom')))
    parser.add_argument('--no-browser',action='store_true')
    parser.add_argument('--converter',type=Path,help='Path to Calibre ebook-convert; remembered for this catalogue')
    args = parser.parse_args()
    settings_path = args.data_dir.expanduser() / 'settings.json'
    if args.converter:
        candidate = args.converter.expanduser().resolve()
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            parser.exit(1, f'Converter is not an executable file: {candidate}\n')
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps({'converter':str(candidate)}))
    elif not os.environ.get('BOOKROOM_CONVERTER') and settings_path.is_file():
        try:
            candidate = json.loads(settings_path.read_text()).get('converter','')
            if candidate and Path(candidate).is_file():
                os.environ['BOOKROOM_CONVERTER'] = candidate
        except (ValueError,OSError):
            pass
    if args.converter:
        os.environ['BOOKROOM_CONVERTER'] = str(candidate)
    library = Library(args.data_dir.expanduser())
    try: server = make_server(library,args.port)
    except OSError as e:
        library.close(); parser.exit(1,f'Could not start Bookroom: {e}. Try --port 8766.\n')
    url = f'http://127.0.0.1:{server.server_port}'
    print(f'Bookroom is running at {url}\nCatalogue: {library.db_path}\nPress Ctrl+C to stop.',flush=True)
    if not args.no_browser: threading.Timer(0.5,lambda:webbrowser.open(url)).start()
    try: server.serve_forever()
    except KeyboardInterrupt: print('\nFinishing queued work before shutdown…',flush=True)
    finally: server.server_close(); library.close()
