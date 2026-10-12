#!/usr/bin/env python3
"""Install an immutable runtime and user-login or system-boot launchd service."""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import plistlib
import pwd
import shutil
import subprocess
import sys
import time


def install(source, home, python, user, system=False, plan=False):
    source=Path(source).resolve();home=Path(home).resolve()
    state=home/'Library/Application Support/ReleaseCoordinator'
    digest=hashlib.sha256()
    for file in sorted((source/'release_coordinator').glob('*.py')):
        digest.update(file.name.encode());digest.update(file.read_bytes())
    revision=digest.hexdigest()[:16]
    runtime=state/'runtime'/revision
    label='local.release-coordinator'
    uid=pwd.getpwnam(user).pw_uid
    plist=Path('/Library/LaunchDaemons')/(label+'.plist') if system else home/'Library/LaunchAgents'/(label+'.plist')
    domain='system' if system else 'gui/'+str(uid)
    command=[str(python),'-B','-m','release_coordinator','--state-dir',str(state),'serve']
    doc={'Label':label,'ProgramArguments':command,'WorkingDirectory':str(runtime),
         'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':10,'ExitTimeOut':30,
         'EnvironmentVariables':{'PATH':'/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin',
                                 'GH_CONFIG_DIR':str(home/'.config/gh')},
         'StandardOutPath':str(state/'service.log'),'StandardErrorPath':str(state/'service.log')}
    if system:doc.update(UserName=user,GroupName=pwd.getpwnam(user).pw_gid)
    # launchd GroupName expects a name rather than numeric gid.
    if system:
        import grp
        doc['GroupName']=grp.getgrgid(pwd.getpwnam(user).pw_gid).gr_name
    result={'runtime':str(runtime),'stateDir':str(state),'startup':'boot' if system else 'login','plist':str(plist),'job':doc}
    if plan:return result
    if system and os.geteuid()!=0:raise ValueError('System boot installation requires sudo; service runs as '+user+', not root')
    state.mkdir(parents=True,exist_ok=True,mode=0o700);state.chmod(0o700)
    runtime.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if not runtime.exists():
        runtime.mkdir(mode=0o700)
        shutil.copytree(source/'release_coordinator',runtime/'release_coordinator',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    elif any((runtime/'release_coordinator'/f.name).read_bytes()!=f.read_bytes() for f in (source/'release_coordinator').glob('*.py')):
        raise ValueError('Existing runtime does not match its digest')
    (state/'service.log').touch(mode=0o600,exist_ok=True)
    bin_dir=home/'.local/bin';bin_dir.mkdir(parents=True,exist_ok=True)
    wrapper=bin_dir/'releasectl'
    wrapper.write_text('#!'+str(python)+'\nimport sys\nsys.path.insert(0,'+repr(str(runtime))+')\nfrom release_coordinator.cli import main\nraise SystemExit(main())\n')
    wrapper.chmod(0o755)
    # Prevent login and boot variants competing for the same private socket.
    subprocess.run(['launchctl','bootout','gui/'+str(uid)+'/'+label],capture_output=True)
    if system or os.geteuid()==0:subprocess.run(['launchctl','bootout','system/'+label],capture_output=True)
    elif Path('/Library/LaunchDaemons',label+'.plist').exists():
        raise ValueError('A system service is installed; update it using sudo --system')
    if system:
        login_plist=home/'Library/LaunchAgents'/(label+'.plist')
        if login_plist.exists():
            previous=login_plist.read_bytes()
            if plistlib.loads(previous).get('Label')!=label:raise ValueError('Unexpected login plist owner')
            history=state/'startup-history';history.mkdir(exist_ok=True,mode=0o700)
            saved=history/('login-'+hashlib.sha256(previous).hexdigest()[:16]+'.plist')
            if not saved.exists():saved.write_bytes(previous)
            login_plist.unlink()
    plist.parent.mkdir(parents=True,exist_ok=True)
    plist.write_bytes(plistlib.dumps(doc));plist.chmod(0o644)
    if system:
        for path in [state,*state.rglob('*'),wrapper]:
            if not path.is_symlink():os.chown(path,uid,pwd.getpwnam(user).pw_gid)
        os.chown(plist,0,0)
    receipt={k:v for k,v in result.items() if k!='job'};receipt['runtimeDigest']=digest.hexdigest()
    (state/'installation.json').write_text(json.dumps(receipt,indent=2)+'\n');(state/'installation.json').chmod(0o600)
    if system:os.chown(state/'installation.json',uid,pwd.getpwnam(user).pw_gid)
    # bootout can return before launchd finishes unloading the old job.
    for attempt in range(12):
        started=subprocess.run(['launchctl','bootstrap',domain,str(plist)],capture_output=True,text=True)
        if started.returncode==0:break
        if attempt==11:raise ValueError('launchd bootstrap failed after bounded retries: '+started.stderr.strip())
        time.sleep(.5)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--system',action='store_true',help='Start at system boot before login; requires sudo')
    p.add_argument('--plan',action='store_true')
    p.add_argument('--user',default=getpass.getuser());p.add_argument('--home')
    p.add_argument('--python',default=sys.executable)
    args=p.parse_args()
    if sys.platform!='darwin':raise SystemExit('Use the documented systemd unit on Linux; this installer is for macOS')
    home=args.home or pwd.getpwnam(args.user).pw_dir
    try:result=install(Path(__file__).parent,home,args.python,args.user,args.system,args.plan)
    except (ValueError,subprocess.CalledProcessError) as e:raise SystemExit(str(e))
    print(json.dumps(result,indent=2))

if __name__=='__main__':main()
