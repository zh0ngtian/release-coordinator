import argparse
import http.client
import json
from pathlib import Path
import socket
import sys
import time

from .service import serve


def default_root():
    return Path.home()/('Library/Application Support/ReleaseCoordinator' if sys.platform=='darwin' else '.local/state/release-coordinator')


class Connection(http.client.HTTPConnection):
    def __init__(self,path,timeout=15):super().__init__('localhost',timeout=timeout);self.path=str(path)
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.settimeout(self.timeout);self.sock.connect(self.path)


def call(root,method,path,data=None,timeout=15):
    conn=Connection(Path(root)/'service.sock',timeout)
    try:
        conn.request(method,path,json.dumps(data) if data is not None else None,{'Content-Type':'application/json'})
        response=conn.getresponse();result=json.loads(response.read())
        if response.status>=400:raise ValueError(result.get('error',str(response.status)))
        return result
    finally:conn.close()


def main(argv=None):
    p=argparse.ArgumentParser(description='Project-isolated batch release coordinator')
    p.add_argument('--state-dir',default=str(default_root()))
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('serve');sub.add_parser('health')
    project=sub.add_parser('project');project.add_argument('--config',required=True)
    register=sub.add_parser('register');register.add_argument('--project',required=True);register.add_argument('--task');register.add_argument('--urgent',action='store_true');register.add_argument('--separate',action='store_true');register.add_argument('--repairs-batch');register.add_argument('--corrects-task')
    ready=sub.add_parser('ready');ready.add_argument('--task',required=True);ready.add_argument('--sha',required=True);ready.add_argument('--validation',required=True)
    hb=sub.add_parser('heartbeat');hb.add_argument('--task',required=True);hb.add_argument('--state',choices=['active','closing','blocked','withdrawn'])
    status=sub.add_parser('status');status.add_argument('--project');status.add_argument('--task')
    batch=sub.add_parser('batch');batch.add_argument('--id',required=True)
    resume=sub.add_parser('resume');resume.add_argument('--batch',required=True)
    wait=sub.add_parser('wait');wait.add_argument('--task',required=True);wait.add_argument('--timeout',type=float,default=45)
    args=p.parse_args(argv)
    try:
        root=Path(args.state_dir).expanduser()
        if args.command=='serve':serve(root);return 0
        if args.command=='health':result=call(root,'GET','/health')
        elif args.command=='project':result=call(root,'POST','/project',json.loads(Path(args.config).expanduser().read_text()))
        elif args.command=='register':result=call(root,'POST','/task',{'project':args.project,'id':args.task,'urgent':args.urgent,'separate':args.separate,'repairsBatch':args.repairs_batch,'correctsTask':args.corrects_task})
        elif args.command=='ready':result=call(root,'POST','/ready',{'id':args.task,'sha':args.sha,'validation':json.loads(Path(args.validation).expanduser().read_text())},timeout=300)
        elif args.command=='heartbeat':result=call(root,'POST','/heartbeat',{'id':args.task,'state':args.state})
        elif args.command=='batch':result=call(root,'GET','/batch/'+args.id)
        elif args.command=='resume':result=call(root,'POST','/resume',{'batch':args.batch})
        elif args.command=='wait':
            deadline=time.monotonic()+args.timeout
            while True:
                snap=call(root,'GET','/status');task=next((t for t in snap['tasks'] if t['id']==args.task),None)
                if not task:raise ValueError('Unknown task')
                batch=next((b for b in snap['batches'] if b['id']==(task.get('recovered_by') or task['batch'])),None)
                if task['state'] in ('complete','recovered') or (batch and (batch['state']=='failed' or batch['data'].get('recovery',{}).get('attentionRequired'))) or time.monotonic()>=deadline:
                    result={'task':task,'batch':batch};break
                time.sleep(min(2,max(0,deadline-time.monotonic())))
        else:
            result=call(root,'GET','/status')
            if args.project:
                result={k:[x for x in xs if x.get('project',x.get('id'))==args.project] for k,xs in result.items()}
            if args.task:
                task=next((t for t in result['tasks'] if t['id']==args.task),None)
                if not task:raise ValueError('Unknown task')
                result={'task':task,'batch':next((b for b in result['batches'] if b['id']==(task.get('recovered_by') or task['batch'])),None)}
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except (ValueError,OSError) as e:
        print('release-coordinator: '+str(e),file=sys.stderr);return 1
