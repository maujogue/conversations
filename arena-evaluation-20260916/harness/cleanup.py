import json,os,shutil,signal,socket,subprocess,time
from pathlib import Path
R=Path('/tmp/arena-review-UuyMR9NE')
D=Path('/tmp/arena-evaluation-20260916-UuyMR9NE')
assert R.parent==Path('/tmp') and R.name=='arena-review-UuyMR9NE' and (D/'REPORT.zh-CN.md').is_file()
stopped=[]
for name in ['backend','frontend','backend-real','frontend-real','mock-llm','minio']:
    p=R/(name+'.pid')
    if not p.exists():continue
    pid=int(p.read_text())
    cmdfile=Path('/proc')/str(pid)/'cmdline'
    if not cmdfile.exists():continue
    cmd=cmdfile.read_bytes()
    if str(R).encode() not in cmd:raise RuntimeError('PID ownership check failed: '+name)
    os.killpg(pid,signal.SIGTERM);stopped.append({'name':name,'pid':pid})
pg=R/'tools/postgres/pgserver/pginstall/bin/pg_ctl'
p=subprocess.run([str(pg),'-D',str(R/'data/postgres'),'-m','fast','-w','stop'],capture_output=True,text=True)
if p.returncode:raise RuntimeError('PostgreSQL shutdown failed: '+p.stdout+p.stderr)
# Stop only browser binaries installed for this evaluation.
for proc in Path('/proc').iterdir():
    if not proc.name.isdigit():continue
    try:
        argv=(proc/'cmdline').read_bytes().split(b'\0')
        if argv and argv[0].startswith(str(R/'browsers').encode()+b'/'):
            os.kill(int(proc.name),signal.SIGTERM);stopped.append({'name':'review-browser','pid':int(proc.name)})
    except (FileNotFoundError,PermissionError,ProcessLookupError):pass
ports=[25432,28071,28072,28900,29000,29001,23000,23001]
def open_ports():
    opened=[]
    for port in ports:
        with socket.socket() as s:
            s.settimeout(.2)
            if s.connect_ex(('127.0.0.1',port))==0:opened.append(port)
    return opened
for _ in range(30):
    remaining=open_ports()
    if not remaining:break
    time.sleep(.2)
if remaining:raise RuntimeError('Review ports still open: '+str(remaining))
shutil.copy2(R/'cleanup.py',D/'harness/cleanup.py')
for name in ['backend-server.log','backend-real-server.log','postgres-server.log']:
    shutil.copy2(R/'logs'/name,D/'logs'/name)
shutil.rmtree(R)
status=subprocess.check_output(['git','status','--porcelain=v1'],cwd='/home/lozhao/Projects/arena',text=True)
result={'stopped':stopped,'postgresql_stopped':True,'review_ports_still_open':open_ports(),'removed_workdir':str(R),'workdir_exists':R.exists(),'source_git_status':status,'remaining_deliverable_directory':str(D),'global_package_installs':False,'windows_host_access':False}
(D/'evidence/cleanup.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result,indent=2))
