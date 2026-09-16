import hashlib,importlib.metadata,json,shutil,subprocess,sys
from pathlib import Path
from dotenv import dotenv_values
R=Path(__file__).parent
D=Path('/tmp/arena-evaluation-20260916-UuyMR9NE')
D.mkdir(exist_ok=False)
(D/'evidence').mkdir();(D/'harness').mkdir();(D/'logs').mkdir()
for p in (R/'artifacts').iterdir():
    if p.name not in {'live-auth.json','arena-mobile.png','backend-arena.xml','backend-all.xml','backend-retest.xml'}:shutil.copy2(p,D/'evidence'/p.name)
p=D/'evidence/browser-findings.json';data=json.loads(p.read_text());data['mobile']={'superseded_by':'browser-mobile.json','reason':'Immediate resize measurements were transient; verified in fresh mobile context.'};p.write_text(json.dumps(data,indent=2,ensure_ascii=False))
for p in R.iterdir():
    if p.suffix in {'.py','.cjs','.json'} and not p.name.endswith('.pid'):shutil.copy2(p,D/'harness'/p.name)
for p in (R/'logs').iterdir():
    if p.is_file():shutil.copy2(p,D/'logs'/p.name)
packages=sorted([{'name':d.metadata['Name'],'version':d.version} for d in importlib.metadata.distributions()],key=lambda d:d['name'].lower())
metadata={'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd='/home/lozhao/Projects/arena',text=True).strip(),'source_status':subprocess.check_output(['git','status','--porcelain=v1'],cwd='/home/lozhao/Projects/arena',text=True),'python':sys.version,'packages':packages,'node':subprocess.check_output([R/'tools/node/bin/node','--version'],text=True).strip(),'postgresql':'16.2 portable + pg_trgm/unaccent/fuzzystrmatch built into portable prefix','minio_manifest':json.loads((R/'tools/minio-manifest.json').read_text()),'original_workdir':str(R),'report_timezone':'Europe/Paris','isolation':'Temporary git archive, local virtualenv and binary prefixes; no sudo or global package installs; no Windows paths accessed.'}
(D/'evidence/environment.json').write_text(json.dumps(metadata,indent=2))
key=dotenv_values('/home/lozhao/Projects/choix_model/common')['AI_API_KEY'].encode()
leaks=[str(p.relative_to(D)) for p in D.rglob('*') if p.is_file() and key in p.read_bytes()]
if leaks:raise RuntimeError('Sensitive value present in deliverables: '+str(leaks))
print('Bundle:',D,'files:',sum(p.is_file() for p in D.rglob('*')),'API key scan: no matches')
