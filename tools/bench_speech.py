#!/usr/bin/env python3
"""Generate fixed synthetic speech fixtures, reserving $0.02 per clip."""
import argparse,json,os,sys,urllib.request
from pathlib import Path
from bench_desktop import Budget,ROOT,save
sys.path.insert(0,str(ROOT/'src'))
from omarchy_voice import config
TEXT={
 'health':'Oma, how is this computer running? Check CPU activity, memory usage, temperature and power.',
 'steer':'Oma, first find my voice shortcut, then check memory usage.',
 'correction':'Actually, skip the memory check. Only tell me the system time.',
}
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--connect',action='store_true')
if not parser.parse_args().connect:parser.error('--connect required for paid speech generation')
config.load_env_file();cfg=config.load()
budget=Budget()
for name,text in TEXT.items():
 path=ROOT/'benchmarks'/'speech'/(name+'.pcm');path.parent.mkdir(parents=True,exist_ok=True)
 if path.exists():continue
 if budget.used()+.02>2.9:raise RuntimeError('budget exhausted')
 entry={'id':'speech-'+name,'model':'gpt-4o-mini-tts','status':'running','reserve':.02}
 budget.data['runs'].append(entry);save(budget_path:=ROOT/'benchmarks'/'budget.json',budget.data)
 req=urllib.request.Request('https://api.openai.com/v1/audio/speech',data=json.dumps({'model':'gpt-4o-mini-tts','voice':'coral','input':text,'response_format':'pcm','instructions':'Speak naturally and clearly at a normal conversational pace.'}).encode(),headers={'Authorization':'Bearer '+os.environ[cfg.api_key_env],'Content-Type':'application/json'})
 with urllib.request.urlopen(req,timeout=45) as response:data=response.read()
 if not data or len(data)%2:raise RuntimeError('invalid PCM')
 path.write_bytes(data);path.chmod(0o600)
 budget.finish(entry,{'charged_estimate':.02,'cost_basis':'conservative reservation; speech endpoint returned audio without usage','seconds':len(data)/48000})
 print(name,len(data)/48000,'seconds; total reserved spend',budget.used(),flush=True)
