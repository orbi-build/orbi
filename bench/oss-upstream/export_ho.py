import json,os,re,glob
out=[]
for d in sorted(glob.glob(os.path.expanduser('~/orbi-bench/runs/ho-*/'))):
    n=os.path.basename(d.rstrip('/')); f=d+'score.txt'
    if not os.path.exists(f): continue
    s=open(f).read(); task=open(d+'task').read().strip(); v=n.rsplit('-',2)[1]
    m=re.search(r'elapsed=(\d+)m',open(d+'run.out').read()) if os.path.exists(d+'run.out') else None
    out.append({'inst':n,'task':task,'variant':v,'pass':'RESULT exit=0' in s,
      'source':(re.search(r'^source: (.*)$',s,re.M) or [None,None])[1],
      'labels':(re.search(r'^labels: (.*)$',s,re.M) or [None,None])[1],
      'fails':[l.strip() for l in s.splitlines() if l.startswith('FAIL')][:5],'minutes':int(m.group(1)) if m else None})
json.dump(out,open(os.path.expanduser('~/orbi-bench/results_ho.json'),'w'),ensure_ascii=False,indent=1)
from collections import defaultdict
c=defaultdict(lambda:[0,0])
for x in out: c[(x['task'],x['variant'])][1]+=1; c[(x['task'],x['variant'])][0]+=x['pass']
for k in sorted(c): print(k, '%d/%d'%tuple(c[k]))
