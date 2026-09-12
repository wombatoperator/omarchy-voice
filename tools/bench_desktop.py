#!/usr/bin/env python3
"""Opt-in paid Live/real-desktop benchmark with a persistent USD ledger.

No microphone capture. Fixture windows are temporary; original windows are
protected from closes and focus is restored. Logs are owner-only. Typed input
measures backend task latency, not acoustic turn latency.
"""
import argparse, asyncio, base64, contextlib, fcntl, hashlib, json, os, re, subprocess, sys, time, tempfile
from pathlib import Path
from unittest import mock
from urllib.parse import quote, urlparse

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'src'))
from omarchy_voice import config, feedback, live, realtime
from omarchy_voice.tools import Executor, Result

RATES={'gpt-5.6-terra':(2,.2,2.5,12), 'gpt-5.6-luna':(.2,.02,.25,1.2), 'gpt-6-astra':(10,1,12.5,50)}
LEDGER=ROOT/'benchmarks'/'budget.json'
CASES={
 'health': "How is this computer running? Check CPU activity, memory use, temperature and power. Give a short factual summary.",
 'shortcuts': "What are my shortcuts for toggling voice and opening the scratchpad?",
 'apps': "Open my Stocks application and the Premier League website.",
 'close': "Close the windows named OMA QA Alpha and OMA QA Beta. Leave all other windows open.",
 'page': "Tell me the five game names on the OMA QA Rankings page, in order.",
 'panel': "The audio panel is covering OMA QA Rankings. Dismiss that panel and show me the Rankings window.",
 'news': "Open my curated news feed together on a new workspace.",
 'replace': 'Close OMA QA Alpha and OMA QA Beta, then open my Stocks application. Leave all other windows open.',
 'steer': "First find my voice shortcut, then check memory usage. Keep the answer brief.",
}

def save(path,data):
 path.parent.mkdir(parents=True,exist_ok=True)
 tmp=path.with_suffix('.tmp')
 with open(tmp,'w') as f: os.chmod(tmp,0o600);json.dump(data,f,indent=2)
 tmp.replace(path)

def price(usage,model):
 i,c,w,o=RATES[model];details=usage.get('input_tokens_details') or {}
 cached=details.get('cached_tokens',0);written=details.get('cache_write_tokens',0)
 plain=max(0,usage.get('input_tokens',0)-cached-written)
 return (plain*i+cached*c+written*w+usage.get('output_tokens',0)*o)/1e6

class QuietSpeaker:
 async def start(self): pass
 def check_error(self): pass
 async def write(self,pcm): pass
 async def close(self): pass
 async def interrupt(self): pass
 def level_now(self): return 0

class Budget:
 def __init__(self):
  LEDGER.parent.mkdir(parents=True,exist_ok=True)
  self.lock=os.open(LEDGER.with_suffix('.lock'),os.O_CREAT|os.O_RDWR,0o600)
  try:fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:
   self.close();raise RuntimeError('Another benchmark owns the budget ledger')
  self.data=json.loads(LEDGER.read_text()) if LEDGER.exists() else {'cap_usd':3.0,'runs':[]}
  if not 0 < self.data['cap_usd'] <= 3:raise RuntimeError('Ledger cap exceeds authorized $3 budget')
  if any(r.get('status')=='running' for r in self.data['runs']):
   raise RuntimeError('Unfinalized run in ledger; reconcile its logs before spending more')
 def close(self):
  if getattr(self,'lock',None) is not None:
   os.close(self.lock);self.lock=None
 def __del__(self):self.close()
 def used(self): return sum(r.get('charged_estimate',r.get('reserve',0)) for r in self.data['runs'])
 def begin(self,entry):
  if entry['model'] not in RATES:raise RuntimeError('No verified pricing for this model; no paid run started')
  # Each session is bounded to 45 seconds and six backend responses. Reserve
  # conservatively for cold-cache input; stop well before the account cap.
  reserve=({'gpt-5.6-terra': .65, 'gpt-5.6-luna': .15, 'gpt-6-astra': .35}[entry['model']]) * (2 if entry.get('tier')=='priority' else 1)
  if entry['model']=='gpt-6-astra':
   if entry.get('source')!='synthetic' or entry.get('case')!='article':raise RuntimeError('Astra reserve supports only the fixed three-response protocol fixture')
   reserve=.25
  if entry.get('variant') == 'astra': reserve = .40  # 3 bounded Terra + 3 bounded Astra responses
  if self.used()+reserve>self.data['cap_usd']-.10: raise RuntimeError('Budget reserve exhausted')
  entry.update(status='running',reserve=reserve)
  self.data['runs'].append(entry);save(LEDGER,self.data)
  return entry
 def finish(self,entry,result):
  entry.update(result);entry['status']='complete';save(LEDGER,self.data)

async def run(args):
 config.load_env_file()
 cfg=config.load(notify=False,dry_run=False)
 cfg.live_browser_enabled=args.variant=='astra'
 if args.variant=='astra':
  cfg.live_browser_max_turns=3
  cfg.live_browser_max_output_tokens=512
  cfg.live_browser_timeout_seconds=35
  CASES['page']='Open the rankings panel on OMA QA Astra Rankings '+time.strftime('%H%M%S')+', then tell me its five game names in order.'
 cfg.live_backend_model=args.model
 cfg.live_reasoning_effort=args.reasoning
 cfg.live_service_tier=args.tier
 if args.synthetic:cfg.news_sources=['https://apnews.com/','https://www.reuters.com/','https://www.nytimes.com/'] if args.case=='news' else []
 cfg.live_typed_idle_seconds=3 if args.audio else 1
 cfg.live_max_session_seconds=45
 cfg.live_max_output_tokens=512 if args.variant=="astra" else 1024
 cfg.max_turns=5
 budget=Budget()
 identity=time.strftime('%Y%m%d-%H%M%S')+'-'+args.case+'-'+args.variant
 folder=ROOT/'benchmarks'/identity;folder.mkdir(parents=True,exist_ok=True);os.chmod(folder,0o700)
 ex=Executor(cfg)
 initial=[] if args.synthetic else ex._query_json('clients');initial_ids={w['address'] for w in initial}
 previous=next((w['address'] for w in initial if w.get('focusHistoryID')==0),None)
 fixtures=[]
 fixture_processes=[]
 fixture_profile=None
 owned={'0xa1','0xa2','0xa3'} if args.synthetic else set()
 expected_hosts=set()
 entry=budget.begin({'id':identity,'case':args.case,'variant':args.variant,'model':args.model,'source':'synthetic' if args.synthetic else 'desktop','input':'speech' if args.audio else 'text','routing':'completion-handoff','reasoning':args.reasoning,'tier':args.tier,'code_sha256':hashlib.sha256(b''.join(p.read_bytes() for p in sorted((ROOT/'src'/'omarchy_voice').glob('*.py')))+(ROOT/'tools'/'bench_prompt.txt').read_bytes()).hexdigest(),'commit':subprocess.check_output(['git','rev-parse','--short','HEAD'],cwd=ROOT,text=True).strip()})
 result={};events=[];delegation_events=[];usage=[];calls=[];outputs=[];errors=[];response_count=0;first_action=None;completed_at=None;sent_at=None;start=time.monotonic();steered=False;audio_end=None;first_audio=None;first_call_ready=None;correction=asyncio.Event();correction_at=None
 patches=[mock.patch.object(config,'STATE_DIR',folder),mock.patch.object(feedback,'STATE_DIR',folder),mock.patch.object(feedback,'RUNTIME_DIR',folder),mock.patch.object(feedback,'LOG_FILE',folder/'session.log'),mock.patch.object(feedback,'STATE_FILE',folder/'state.json'),mock.patch.object(feedback,'LEVEL_FILE',folder/'level')]
 if args.synthetic:
  patches.extend([mock.patch.object(live.capabilities,'manifest',return_value='Hyprland supports hl.dsp.focus and hl.dsp.window.close with explicit address selectors.'),mock.patch.object(live.capabilities,'live_state',return_value=('Synthetic desktop: empty workspace 5.' if args.case in ('health','steer','shortcuts') else 'Synthetic desktop, workspace 5. Windows: address 0xa1, class chromium, title OMA QA Alpha; address 0xa2, class chromium, title OMA QA Beta; address 0xa3, class chromium, title OMA QA Rankings.')),mock.patch.object(live.capabilities,'installed_apps',return_value='Stocks (stocks), System Monitor (btop)')])
 for patch in patches:patch.start()
 try:
  if not args.synthetic and args.case in ('close','replace','page','panel'):
   for title in (['OMA QA Alpha','OMA QA Beta'] if args.case in ('close','replace') else ['OMA QA Rankings']):
    if args.variant=='astra':title=CASES['page'].split(' on ',1)[1].split(',',1)[0]
    page='<title>'+title+'</title><h1>'+title+'</h1><p>QA fixture, not real rankings.</p><ol>'+''.join('<li>'+n+'</li>' for n in ['Amber Quest','Blue Harbor','Copper Sky','Delta Racing','Emerald Valley'])+'</ol><p>End of five entries.</p>'
    if args.variant=='astra':
     page='<title>'+title+'</title><h1>'+title+'</h1><button style="font-size:24px" onclick="this.nextElementSibling.hidden=false;this.hidden=true">Open rankings</button><div hidden>'+page+'</div>'
    before={w['address'] for w in ex._query_json('clients')}
    if fixture_profile is None:fixture_profile=tempfile.TemporaryDirectory(prefix='oma-desktop-qa-')
    fixture_processes.append(subprocess.Popen(['/opt/google/chrome/chrome','--ozone-platform=x11','--user-data-dir='+fixture_profile.name,'--no-first-run','--no-default-browser-check','--new-window','data:text/html,'+quote(page)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True))
    deadline=time.monotonic()+12
    address=None
    while time.monotonic()<deadline and not address:
     time.sleep(.15)
     address=next((w['address'] for w in ex._query_json('clients') if w['address'] not in before and title in w.get('title','')),None)
    if not address: raise RuntimeError('fixture did not open')
    fixtures.append(address);owned.add(address)
  if not args.synthetic and args.case=='panel': ex._shell(['omarchy','shell','shell','summon','omarchy.audio','{}'])
  session=live.LiveSession(cfg);session.loop=asyncio.get_running_loop();session.speaker=QuietSpeaker()
  original_start=session._session_start
  async def session_start():
   payload=await original_start()
   if args.variant=='compact':
    # Use a candidate prompt file without changing the installed baseline.
    full=payload['session']['delegation']['responses']['instructions']
    full=full.replace(live.BACKEND_PROMPT,(ROOT/'tools'/'bench_prompt.txt').read_text())
    payload['session']['delegation']['responses']['instructions']=full
   return payload
  session._session_start=session_start
  original_send=session._send
  async def send(payload):
   nonlocal sent_at
   if payload['type']=='response.create' and sent_at is None: sent_at=time.monotonic()
   if payload['type']=='response.create' and response_count>=(3 if args.variant=='astra' else 6):
    session._close_requested.set();return
   await original_send(payload)
  session._send=send
  original_event=session._on_event
  async def on_event(event):
   nonlocal response_count,completed_at,sent_at,first_audio,first_call_ready
   nested=event.get('event',{})
   if event['type']=='session.delegation.created':delegation_events.append(event)
   if nested.get('type')=='response.output_item.done' and nested.get('item',{}).get('type')=='function_call' and first_call_ready is None:first_call_ready=time.monotonic()
   if event['type']=='session.output_audio.delta' and first_audio is None and realtime.frame_level(base64.b64decode(event.get('delta',''))) > 0:first_audio=time.monotonic()
   if event['type']=='session.delegation.created' and sent_at is None:sent_at=time.monotonic()
   if event['type']=='response.event' and nested.get('type')=='response.created':
    response_count+=1
    if response_count>6:errors.append('response cap reached');session._close_requested.set()
   if event['type']=='response.event' and nested.get('type') in ('response.completed','response.failed','response.incomplete','response.cancelled'):
    u=nested.get('response',{}).get('usage')
    if u:usage.append(u)
    if nested['type']!='response.completed':errors.append(nested['type'])
   if event['type']=='error':errors.append(event.get('error'))
   await original_event(event)
   if event['type']=='response.event' and nested.get('type')=='response.completed':
    record=session._responses.get(nested.get('response',{}).get('id'))
    if record and not record.calls and not session._steering_text: completed_at=time.monotonic()
  session._on_event=on_event
  original_execute=session._execute
  browser_attempts=0
  async def execute(name,arguments,**kwargs):
   nonlocal first_action,steered,browser_attempts
   if args.variant=='astra' and name=='browser_task':
    browser_attempts+=1
    if browser_attempts>1:return 'ERROR: benchmark allows only one bounded Astra task. Report the partial result.'
   if first_action is None:first_action=time.monotonic()
   calls.append({'name':name,'args':arguments,'parallel':kwargs.get('parallel',False),'at_ms':round((time.monotonic()-start)*1000,1)})
   # Bench tasks may close only windows created since the benchmark started.
   if name=='hypr_dispatch' and 'close' in arguments.get('lua',''):
    addresses=re.findall(r'address:(0x[0-9a-fA-F]+)',arguments['lua'])
    if not addresses or any(a not in owned for a in addresses):
     errors.append('protected original window close blocked');return 'ERROR: benchmark protects pre-existing windows'
   if name=='omarchy_cli' and arguments.get('command')!='launch stocks':
    errors.append('unexpected Omarchy route');return 'ERROR: route outside benchmark scenario'
   if name not in {'omarchy_cli','hypr_query','hypr_dispatch','omarchy_help','system_query','launch_app','open_page','web_search','read_page_text','read_screen','reveal_window','compose_windows','browser_task'}:
    errors.append('unexpected tool '+name);return 'ERROR: tool outside this benchmark scenario'
   if args.case=='steer' and not steered:
    steered=True
    if args.audio:
     correction.set()
     await asyncio.sleep(7)  # A controlled slow query gives a spoken correction time to arrive.
    else:
     session._last_input_at=time.monotonic()-2
     await session._on_event({'type':'session.input_transcript.delta','delta':'Actually skip memory. Only check the system time and report it.'})
   if args.synthetic:
    topic=arguments.get('topic','')
    data={'memory':'8 GiB of 32 GiB used','processes':'browser 12% CPU','temperature':'CPU 35 C','battery':'mains power','time':'Tuesday 15:00 UTC','shortcuts':'SUPER SHIFT V: Voice toggle; SUPER ALT S: Scratchpad'}
    out=data.get(topic,'Synthetic query succeeded')
    if name=='hypr_query':out='Workspace 5: OMA QA Alpha address 0xa1; OMA QA Beta address 0xa2; OMA QA Rankings address 0xa3. All are chromium windows.'
    if name=='read_page_text':out='OMA QA Rankings. Five entries, in order: Amber Quest; Blue Harbor; Copper Sky; Delta Racing; Emerald Valley. End of entries.'
    if name=='reveal_window':out='Focused OMA QA Rankings at 0xa3. Audio panel hide requested. Visible text: OMA QA Rankings.'
    if name in ('launch_app','open_page'):out='Launch request sent successfully.'
    if name=='compose_windows':out='Opened and arranged all three requested source pages in columns on workspace 6.'
    if name=='hypr_dispatch':out='Requested addressed window close completed successfully.'
   else:out=await original_execute(name,arguments,**kwargs)
   hints=[]
   if (name=='launch_app' and arguments.get('app','').removesuffix('.desktop')=='stocks') or (name=='omarchy_cli' and arguments.get('command')=='launch stocks'):hints=['tradingview.com']
   if name=='open_page':hints=[urlparse(arguments.get('url','')).hostname or '']
   if name=='compose_windows':hints=[urlparse(p.get('target','')).hostname or '' for p in arguments.get('panes',[]) if p.get('kind')=='web']
   expected_hosts.update(h for h in hints if h)
   if expected_hosts and not args.synthetic:
    for w in ex._query_json('clients'):
     if w['address'] not in initial_ids and any(h and h in w.get('class','') for h in expected_hosts):owned.add(w['address'])
   outputs.append({'name':name,'output':out});return out
  session._execute=execute
  if args.audio:
   session.active=True;session._wanted.set()
   async def audio_loop():
    nonlocal audio_end,correction_at
    pending=(ROOT/'benchmarks'/'speech'/(args.case+'.pcm')).read_bytes()
    step=cfg.live_sample_rate//10*2
    corrected=False
    while not session._closing and not session._close_requested.is_set():
     if correction.is_set() and not corrected and not pending:
      pending=(ROOT/'benchmarks'/'speech'/'correction.pcm').read_bytes();corrected=True;correction_at=time.monotonic()
     chunk,pending=pending[:step],pending[step:]
     if chunk:audio_end=time.monotonic()
     await session._send({'type':'session.input_audio.append','audio':base64.b64encode(chunk or bytes(step)).decode()})
     if completed_at and not pending and time.monotonic()-session._last_activity>3:
      session._close_requested.set()
     await asyncio.sleep(.1)
   session._audio_loop=audio_loop
  else:await session._inject(CASES[args.case])
  worker=asyncio.create_task(session._tool_worker())
  try:
   await asyncio.wait_for(session._connection({'Authorization':'Bearer '+os.environ[cfg.api_key_env],'OpenAI-Safety-Identifier':realtime._safety_identifier()}),65)
  except Exception as exc:errors.append(type(exc).__name__+': '+str(exc))
  finally:
   worker.cancel();await asyncio.gather(worker,return_exceptions=True)
  for line in (folder/'session.log').read_text().splitlines():
   if 'perf    ' in line:events.append(json.loads(line.split('perf    ',1)[1]))
  final_windows=[] if args.synthetic else ex._query_json('clients')
  # Browser class names may settle after a launch command returns. Reconcile
  # all requested hosts after the response, before verification and cleanup.
  for w in final_windows:
   if w['address'] not in initial_ids and any(h in w.get('class','') for h in expected_hosts):owned.add(w['address'])
  remaining={w['address'] for w in final_windows}
  texts=' '.join(o['output'] for o in outputs)
  passed=bool(completed_at) and not errors
  if args.case=='close':passed &= all(any(a in c['args'].get('lua','') for c in calls if c['name']=='hypr_dispatch') for a in ('0xa1','0xa2')) if args.synthetic else all(a not in remaining for a in fixtures)
  if args.case=='replace':passed &= not any('OMA QA Alpha' in w.get('title','') or 'OMA QA Beta' in w.get('title','') for w in final_windows) and any(w['address'] in owned and 'tradingview.com' in w.get('class','') for w in final_windows)
  if args.case=='page':passed &= all(n in texts for n in ['Amber Quest','Blue Harbor','Copper Sky','Delta Racing','Emerald Valley'])
  if args.case=='panel':passed &= any(c['name']=='reveal_window' and c['args'].get('panel')=='audio' for c in calls) and any(o['name']=='reveal_window' and not o['output'].startswith('ERROR:') for o in outputs)
  if args.case=='panel' and not args.synthetic:passed &= any(w['address'] in fixtures and w.get('focusHistoryID')==0 for w in ex._query_json('clients'))
  if args.case=='health':passed &= all(any(c['name']=='system_query' and c['args'].get('topic')==t for c in calls) for t in ['memory','processes','temperature','battery'])
  if args.case=='steer':passed &= sum(c['name']=='system_query' and c['args'].get('topic')=='time' for c in calls)==1 and not any(c['args'].get('topic')=='memory' and (correction_at is None or c['at_ms']>(correction_at-start)*1000) for c in calls)
  if args.case=='apps':passed &= any((c['name']=='launch_app' and c['args'].get('app','').removesuffix('.desktop')=='stocks') or (c['name']=='omarchy_cli' and c['args'].get('command')=='launch stocks') for c in calls) and not any(c['name']=='web_search' for c in calls) and (any(c['name']=='open_page' and 'premierleague.com' in c['args'].get('url','') and c['args'].get('read') is False for c in calls) if args.synthetic else len(owned & remaining)>=2)
  if args.case=='news':passed &= any(c['name']=='compose_windows' and len(c['args'].get('panes',[]))==3 and c['args'].get('workspace','next')=='next' for c in calls)
  if args.case=='news' and not args.synthetic:passed &= len(owned & remaining)>=3
  browser_events=[json.loads(line) for line in (folder/'live-trace.jsonl').read_text().splitlines() if line.strip()]
  astra_cost=sum(e.get('usd_estimate',0) for e in browser_events if e.get('event')=='browser_response')
  cost=astra_cost+sum(price(u,args.model) for u in usage)*(2 if args.tier=='priority' else 1)+session._usage_seconds*.05/60
  # Missing final usage keeps the full reservation charged to the experiment.
  charged=cost if session._usage_final and usage and not any(e.get('event')=='browser_cancelled' or (e.get('event')=='browser_finished' and not e.get('usage_complete',True)) for e in browser_events) else entry['reserve']
  result={'passed':bool(passed),'final_close':session._usage_final,'voice_seconds':session._usage_seconds,'charged_estimate':round(charged,6),'backend_responses':response_count,'astra_usd_estimate':round(astra_cost,6),'astra_responses':sum(e.get('event')=='browser_response' for e in browser_events),'call_ready_to_action_ms':round((first_action-first_call_ready)*1000,1) if first_action and first_call_ready else None,'first_action_ms':round((first_action-sent_at)*1000,1) if first_action and sent_at else None,'correction_at_ms':round((correction_at-start)*1000,1) if correction_at else None,'speech_to_action_ms':round((first_action-audio_end)*1000,1) if first_action and audio_end else None,'speech_to_completion_ms':round((completed_at-audio_end)*1000,1) if completed_at and audio_end else None,'first_audio_ms':round((first_audio-audio_end)*1000,1) if first_audio and audio_end else None,'completion_ms':round((completed_at-sent_at)*1000,1) if completed_at and sent_at else None,'elapsed_ms':round((time.monotonic()-start)*1000,1),'parallel_calls':sum(c['parallel'] for c in calls),'tools':[c['name'] for c in calls],'errors':errors,'input_tokens':sum(u.get('input_tokens',0) for u in usage),'output_tokens':sum(u.get('output_tokens',0) for u in usage)}
  save(folder/'detail.json',{'prompt':{'variant':args.variant,'model':args.model,'reasoning':args.reasoning,'tier':args.tier},'metrics':result,'initial_windows':initial,'final_windows':final_windows,'owned_windows':sorted(owned),'delegation_events':delegation_events,'calls':calls,'outputs':outputs,'events':events,'usage':usage,'history':session._history})
  budget.finish(entry,result)
  print(json.dumps({**entry,'budget_used':round(budget.used(),6)}),flush=True)
 finally:
  if entry.get('status')=='running':
   started_api=(folder/'live-trace.jsonl').exists()
   budget.finish(entry,{'passed':False,'charged_estimate':entry['reserve'] if started_api else 0,
                        'error':'Benchmark exited before result finalization'})
  budget.close()
  for patch in reversed(patches):patch.stop()
  if not args.synthetic and args.case=='panel':ex._shell(['omarchy','shell','shell','hide','omarchy.audio'])
  # Only benchmark-created windows; never close original user windows.
  for window in ([] if args.synthetic else ex._query_json('clients')):
   if window['address'] in owned and window['address'] not in initial_ids:
    ex._dispatch_lua('hl.dsp.window.close({ window = "address:'+window['address']+'" })')
  if not args.synthetic:
   deadline=time.monotonic()+3
   while any(w['address'] in owned for w in ex._query_json('clients')):
    if time.monotonic()>deadline:raise RuntimeError('Benchmark windows have not closed; stop before another run')
    await asyncio.sleep(.03)
  for process in fixture_processes:
   if process.poll() is None:
    process.terminate()
    try:process.wait(timeout=3)
    except subprocess.TimeoutExpired:process.kill();process.wait()
  if fixture_profile:fixture_profile.cleanup()
  if previous:ex._dispatch_lua('hl.dsp.focus({ window = "address:'+previous+'" })')

if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--connect',action='store_true')
 parser.add_argument('--audio',action='store_true',help='Stream generated speech, without microphone or speakers (synthetic health/steer only)')
 parser.add_argument('--synthetic',action='store_true',help='Use fabricated context and tool results; never read or change desktop')
 parser.add_argument('--case',choices=CASES,required=True)
 parser.add_argument('--variant',choices=['baseline','compact','astra'],default='baseline')
 parser.add_argument('--reasoning',choices=['none','low'],default='low')
 parser.add_argument('--tier',choices=['default','priority'],default='default')
 parser.add_argument('--model',choices=['gpt-5.6-terra','gpt-5.6-luna'],default='gpt-5.6-terra')
 args=parser.parse_args()

 if args.variant=='astra' and (args.case!='page' or args.synthetic or args.audio):parser.error('Astra variant uses only the real desktop page fixture')
 if args.audio and args.case not in ('health','steer'):parser.error('Speech fixtures support health and steer only')
 if not args.connect:parser.error('--connect is required for paid desktop tests')
 asyncio.run(run(args))
