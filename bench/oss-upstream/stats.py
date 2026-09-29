import json,glob,os,re,subprocess
out=[]
for d in sorted(glob.glob(os.path.expanduser('~/orbi-bench/runs/*/'))):
    n=os.path.basename(d.rstrip('/')); sc=d+'score.txt'
    if not os.path.exists(sc): continue
    s=open(sc).read()
    ho=os.path.exists(d+'task')
    if ho: task=open(d+'task').read().strip(); v=n.rsplit('-',2)[1]; ok='RESULT exit=0' in s
    else: task={'py':'pyinfra','cl':'chainloop','fe':'fedify'}[n[:2]]; v=n.split('-')[1]; ok='RESULT exit=0' in s
    tok={'total':0,'input':0,'output':0,'cacheRead':0}; roles={}
    for f in glob.glob(d+'repo/.worktrees/*/.pi-session*/*.jsonl')+glob.glob(d+'ws/**/.pi-session*/*.jsonl',recursive=True):
        for l in open(f,errors='ignore'):
            if '"usage"' not in l: continue
            try: m=json.loads(l)['message']
            except Exception: continue
            u=m.get('usage') or {}
            for k in tok:
                kk='totalTokens' if k=='total' else k
                tok[k]+=u.get(kk,0) or 0
    m=re.search(r'elapsed=(\d+)m',open(d+'run.out').read()) if os.path.exists(d+'run.out') else None
    dm=re.search(r'(\d+) insertions?\(\+\)',s); 
    log=open(d+'runner.log',errors='ignore').read() if os.path.exists(d+'runner.log') else ''
    rounds=len(set(re.findall(r'review_round[=": ]+(\d+)',log)))
    fixes=len(re.findall(r'role=fix',log))
    out.append(dict(inst=n,heldout=ho,task=task,variant=v,ok=ok,minutes=int(m.group(1)) if m else None,tokens=tok,
                    ins=int(dm.group(1)) if dm else None,fails=[l for l in s.splitlines() if l.startswith('FAIL')][:4]))
json.dump(out,open(os.path.expanduser('~/orbi-bench/stats.json'),'w'),indent=1)
print(len(out))
