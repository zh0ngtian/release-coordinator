#!/usr/bin/env python3
"""Demonstration adapter: real projects replace this with their own release protocol."""
import argparse
import json
from pathlib import Path

p=argparse.ArgumentParser();p.add_argument('action',choices=['check','run']);p.add_argument('phase');p.add_argument('--context',required=True)
a=p.parse_args();context=json.loads(Path(a.context).read_text())
marker=Path(context['workspace'])/'.git'
# Use the coordinator's private state, never commit demonstration result markers.
root=Path(a.context).parent.parent/'demo-results'/context['id'];root.mkdir(parents=True,exist_ok=True)
path=root/(a.phase+'.json')
if a.action=='run':path.write_text(json.dumps({'phase':a.phase,'demonstrationOnly':True}))
print(json.dumps({'done':path.exists(),'result':json.loads(path.read_text()) if path.exists() else {}}))
