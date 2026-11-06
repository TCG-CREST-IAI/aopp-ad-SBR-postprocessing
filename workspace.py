"""One-party dashboard gateway. Input values are never fetched from the peer.

Run one gateway per role with its own operator configuration and access token.
The local HTTP listener is loopback-only; deployed frontend requests use HTTPS.
"""
import argparse
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from transport import Client,load_config

ROOT=Path(__file__).resolve().parent


class WorkspaceHandler(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass

    def respond(self,status,data,content_type='application/json'):
        raw=json.dumps(data,allow_nan=False).encode() if content_type=='application/json' else data
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        paths={'/':'workspace.html','/workspace.html':'workspace.html','/workspace.js':'workspace.js','/workspace.css':'workspace.css'}
        name=paths.get(self.path.split('?')[0])
        if name is None:
            self.respond(404,{'error':'Unknown route'})
            return
        types={'.html':'text/html; charset=utf-8','.js':'application/javascript','.css':'text/css'}
        self.respond(200,(ROOT/'web'/name).read_bytes(),types[Path(name).suffix])

    def do_POST(self):
        try:
            expected=os.environ.get('WORKSPACE_TOKEN','')
            actual=self.headers.get('Authorization','').removeprefix('Bearer ').strip()
            if len(expected)<24:
                self.respond(503,{'error':'Configure WORKSPACE_TOKEN, WORKSPACE_ROLE and OPERATOR_CONFIG on this gateway'})
                return
            if not hmac.compare_digest(actual,expected):
                self.respond(401,{'error':'Invalid workspace token'})
                return
            if self.path!='/api':
                raise ValueError('Unknown route')
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=2000000:
                raise ValueError('Request size limit exceeded')
            payload=json.loads(self.rfile.read(size))
            if not isinstance(payload,dict) or payload.get('action') not in ('prepare','upload','status','aopp','select_ad','ad','sbi','erase'):
                raise ValueError('Unknown workspace action')
            role=os.environ.get('WORKSPACE_ROLE')
            filename=os.environ.get('OPERATOR_CONFIG')
            if role not in ('alice','bob') or not filename:
                self.respond(503,{'error':'Endpoint gateway configuration is incomplete'})
                return
            config=load_config(filename)
            client=Client(config[role+'_url'],config['ca'],config['cert'],config['key'],config[role+'_cert'],timeout=180)
            self.respond(200,client.post('/pipeline',payload))
        except (OSError,ValueError,KeyError,TypeError) as error:
            self.respond(400,{'error':str(error)})


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--port',type=int,default=8081)
    a=p.parse_args()
    print(f'Endpoint workspace: http://127.0.0.1:{a.port}',flush=True)
    ThreadingHTTPServer(('127.0.0.1',a.port),WorkspaceHandler).serve_forever()
