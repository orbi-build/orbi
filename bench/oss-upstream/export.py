import json,os,re,glob
out=[]
for d in sorted(glob.glob(os.path.expanduser('~/orbi-bench/runs/*/'))):
    n=os.path.basename(d.rstrip('/')); f=d+'score.txt'
    if not os.path.exists(f): continue
    s=open(f).read()
    repo={'py':'pyinfra','cl':'chainloop','fe':'fedify'}[n[:2]]; v=n.split('-')[1]
    fails=[l.strip() for l in s.splitlines() if l.startswith('FAIL')]
    elapsed=None
    ro=d+'run.out'
    if os.path.exists(ro):
        m=re.search(r'elapsed=(\d+)m',open(ro).read()); elapsed=int(m.group(1)) if m else None
    out.append({'inst':n,'repo':repo,'variant':v,'core':'RESULT exit=0' in s,'upstream':'UPSTREAM exit=0' in s,
                'strict_newline':('newline kept' in s) if repo=='chainloop' else None,'fails':fails,'minutes':elapsed})
json.dump(out,open(os.path.expanduser('~/orbi-bench/results.json'),'w'),ensure_ascii=False,indent=1)
print(len(out))
