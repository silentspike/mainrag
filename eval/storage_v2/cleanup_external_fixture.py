"""Owned HTTP protocol fixture; it is not a real Qdrant acceptance server."""
from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FixtureQdrant:
    def __enter__(self):
        owner = self
        self.collections = {'fixture_old': 1, 'fixture_keep': 2}
        self.aliases = {'fixture_old_alias': 'fixture_old', 'fixture_keep_alias': 'fixture_keep'}
        self.mutations = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *unused):
                pass

            def send(self, value):
                raw = json.dumps({'status': 'ok', 'result': value}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                path = urllib.parse.urlparse(self.path).path
                if path == '/collections':
                    self.send({'collections': [{'name': name} for name in sorted(owner.collections)]})
                elif path == '/aliases':
                    self.send({'aliases': [{'alias_name': name, 'collection_name': collection}
                                           for name, collection in sorted(owner.aliases.items())]})
                elif path.startswith('/collections/') and path[len('/collections/'):] in owner.collections:
                    self.send({'status': 'green', 'config': {'params': {'vectors': {'size': 2, 'distance': 'Dot'}}}})
                else:
                    self.send_error(404)

            def do_POST(self):
                value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                path = urllib.parse.urlparse(self.path).path
                if path.endswith('/points/count'):
                    name = path.split('/')[2]
                    self.send({'count': owner.collections[name]})
                elif path == '/collections/aliases':
                    name = value['actions'][0]['delete_alias']['alias_name']
                    del owner.aliases[name]
                    owner.mutations.append(('alias', name))
                    self.send(True)
                else:
                    self.send_error(404)

            def do_DELETE(self):
                name = urllib.parse.urlparse(self.path).path.split('/')[2]
                if name not in owner.collections or name in owner.aliases.values():
                    self.send_error(409)
                    return
                del owner.collections[name]
                owner.mutations.append(('collection', name))
                self.send(True)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *unused):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
