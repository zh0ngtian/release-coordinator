"""Owner-only HTTP over a Unix socket; no public listening port."""
import fcntl
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import signal
import socketserver
import threading
import time

from . import __version__
from .engine import Scheduler
from .store import Conflict, Store


class Server(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
    daemon_threads=True


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass

    def request(self):
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length<0 or length>1024*1024:raise ValueError('Request too large')
            payload=json.loads(self.rfile.read(length)) if length else {}
            store=self.server.store;path=self.path.split('?')[0]
            if self.command=='GET' and path=='/health':
                result={'ok':True,'version':__version__,'pid':os.getpid(),'startedAt':self.server.started_at}
            elif self.command=='GET' and path=='/status':
                result=store.snapshot()
                for project in result['projects']:
                    collection=store.due(project['id'])
                    if collection:project['collection']={'due':collection['due'],'deadline':collection['deadline'],'tasks':[t['id'] for t in collection['tasks']]}
                    preflight=store.root/'preflight'/(project['id']+'.json')
                    if preflight.exists():project['preflight']=json.loads(preflight.read_text())
                for batch in result['batches']:
                    progress=store.root/'progress'/(batch['id']+'.json')
                    if progress.exists():batch['progress']=json.loads(progress.read_text())
            elif self.command=='GET' and path.startswith('/batch/'):
                result=store.batch(path.split('/')[-1])
                progress=store.root/'progress'/(result['id']+'.json')
                if progress.exists():result['progress']=json.loads(progress.read_text())
            elif self.command=='POST' and path=='/project':result=store.add_project(payload)
            elif self.command=='POST' and path=='/task':result=store.register(payload['project'],payload.get('id'),payload.get('urgent',False),payload.get('separate',False))
            elif self.command=='POST' and path=='/ready':result=store.ready(payload['id'],payload['sha'],payload['validation'])
            elif self.command=='POST' and path=='/heartbeat':result=store.touch(payload['id'],payload.get('state'))
            elif self.command=='POST' and path=='/resume':result=store.resume(payload['batch'])
            else:
                self.send_error(404);return
            data=json.dumps(result,ensure_ascii=False).encode();self.send_response(200)
        except (ValueError,KeyError,Conflict) as e:
            data=json.dumps({'error':str(e)}).encode();self.send_response(409 if isinstance(e,Conflict) else 400)
        except Exception as e:
            data=json.dumps({'error':type(e).__name__+': '+str(e)}).encode();self.send_response(500)
        self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)

    do_GET=request
    do_POST=request


def serve(root):
    store=Store(root);root=store.root
    with (root/'service.lock').open('a') as lock:
        os.chmod(root/'service.lock',0o600)
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        sock=root/'service.sock'
        if sock.exists():sock.unlink()
        server=Server(str(sock),Handler);server.store=store;server.started_at=time.time()
        os.chmod(sock,0o600)
        scheduler=Scheduler(store);stopped=threading.Event()
        def stop(*_):
            stopped.set();scheduler.shutdown()
            threading.Thread(target=server.shutdown,daemon=True).start()
        signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            while not stopped.wait(1):scheduler.tick()
        finally:
            scheduler.shutdown(wait=True);server.server_close();sock.unlink(missing_ok=True)
