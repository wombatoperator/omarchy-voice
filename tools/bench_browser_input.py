#!/usr/bin/env python3
"""Free exact-text test in a disposable Chrome window; restores prior focus."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from omarchy_voice.config import Config
from omarchy_voice.tools import Executor

parser=argparse.ArgumentParser(); parser.add_argument('--output',default='benchmarks/browser-input.json');parser.add_argument('--prime-wtype',action='store_true');args=parser.parse_args()
received=[]
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_GET(self):
  content=b'''<!doctype html><title>OMA input probe</title><h1>OMA local input probe</h1><textarea autofocus style="width:90%;height:200px;font-size:24px" id="field"></textarea><script>field.addEventListener('input',()=>fetch('/value',{method:'POST',body:JSON.stringify({value:field.value})}));</script>'''
  self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(content)
 def do_POST(self):
  size=int(self.headers['Content-Length']);received.append(json.loads(self.rfile.read(size)));self.send_response(204);self.end_headers()
server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
ex=Executor(Config()); previous,_=ex._window_geometry('activewindow');window=None;results=[]
try:
 window,why=ex._open_web_window(f'http://127.0.0.1:{server.server_port}/','OMA input probe')
 if not window:raise RuntimeError(why)
 target='address:'+window['address'];ex._dispatch_lua(f'hl.dsp.focus({{ window = "{target}" }})');time.sleep(.5)
 for expected in ['Ben Shelton versus Tiafoe score','OMA test 42: café — naïve!','-quoted "value" & punctuation?\nsecond line']:
  if args.prime_wtype:
   ex._shell(['wtype','--','Ben Shelton versus Tiafoe score'])
  if results or args.prime_wtype:
   result=ex._tool_send_shortcut('CTRL','a',target)
   if not result.ok:raise RuntimeError(result.output)
  received.clear();start=time.monotonic();result=ex._tool_type_text(expected);deadline=time.monotonic()+2
  while time.monotonic()<deadline and (not received or received[-1]['value']!=expected):time.sleep(.02)
  actual=received[-1]['value'] if received else None
  results.append(dict(expected=expected,actual=actual,matched=actual==expected,tool_ok=result.ok,message=result.output,milliseconds=round((time.monotonic()-start)*1000,1),xwayland=window.get('xwayland')))
 print(json.dumps(results,indent=2,ensure_ascii=False));out=Path(args.output);out.parent.mkdir(exist_ok=True);out.write_text(json.dumps(results,indent=2,ensure_ascii=False))
finally:
 if window:ex._dispatch_lua(f'hl.dsp.window.close({{ window = "address:{window["address"]}" }})')
 if previous:ex._dispatch_lua(f'hl.dsp.focus({{ window = "address:{previous["address"]}" }})')
 server.shutdown();server.server_close()
raise SystemExit(0 if results and all(x['matched'] for x in results) else 1)
