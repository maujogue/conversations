import json,time,uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import httpx
from configurations import importer
importer.install()
import django
django.setup()
from django.db import transaction
from django.utils import timezone
from chat import arena,arena_results
from chat.factories import ChatConversationFactory,ArenaExperimentFactory,ArenaChallengerFactory,ArenaComparisonFactory
from chat.models import ArenaComparison
from core.models import User

ROOT=Path(__file__).parent
AUTH=json.loads((ROOT/'artifacts/live-auth.json').read_text())
BASE='http://127.0.0.1:28071/api/v1.0/chats/'
user=User.objects.get(pk=AUTH['user_id'])
csrf='abcdefghijklmnopqrstuvwx12345678'
client=httpx.Client(cookies={AUTH['cookie_name']:AUTH['cookie_value'],'csrftoken':csrf},headers={'X-CSRFToken':csrf,'Origin':'http://127.0.0.1:23000'},timeout=60)
results={}
def save(name,data):
    results[name]=data
    (ROOT/'artifacts/live-probes.json').write_text(json.dumps(results,indent=2,default=str,ensure_ascii=False))
    print(name,json.dumps(data,default=str,ensure_ascii=False),flush=True)
def fresh(label):
    conv=ChatConversationFactory(owner=user,title='QA '+label,messages=[],pydantic_messages=[],model_hrid='default-model')
    r=client.post(BASE+str(conv.pk)+'/arena/draw/',json={})
    r.raise_for_status()
    return conv,ArenaComparison.objects.get(pk=r.json()['comparison_id'])
def side(c,champion=True):return c.champion_side if champion else ('right' if c.champion_side=='left' else 'left')
def stream(conv,c,prompt,s=None):
    body={'messages':[{'id':str(uuid.uuid4()),'role':'user','parts':[{'type':'text','text':prompt}]}]}
    url=BASE+str(conv.pk)+'/conversation/'
    if c:url+=f'?arena_comparison={c.pk}&arena_side={s or side(c)}'
    return client.post(url,json=body)
def vote(conv,c,value):return client.post(BASE+str(conv.pk)+f'/arena/{c.pk}/vote/',json={'side':value})
def reqs(marker):
    return [json.loads(line)['request'] for line in (ROOT/'logs/mock-requests.jsonl').read_text().splitlines() if marker in line]

conv,c=fresh('history')
marker='QA_HISTORY_'+uuid.uuid4().hex
r1=stream(conv,c,marker,side(c));r2=stream(conv,c,marker,side(c,False))
requests=reqs(marker)
save('history_contamination',{'http':[r1.status_code,r2.status_code],'provider_requests':[{'model':r['model'],'roles':[m['role'] for m in r['messages']],'same_prompt_occurrences':sum(marker in str(m.get('content','')) for m in r['messages'])} for r in requests]})

conv2,c2=fresh('duplicate-stream')
marker2='QA_DUP_'+uuid.uuid4().hex+' [SLOW]'
with ThreadPoolExecutor(2) as pool:
    futures=[pool.submit(stream,conv2,c2,marker2,side(c2)) for _ in range(2)]
    responses=[f.result() for f in futures]
conv2.refresh_from_db()
save('duplicate_stream',{'http':[r.status_code for r in responses],'provider_requests':len(reqs(marker2)),'conversation_roles':[m.role for m in conv2.messages]})

conv3,c3=fresh('late-result')
marker3='QA_LATE_'+uuid.uuid4().hex+' [SLOW]'
with ThreadPoolExecutor(1) as pool:
    future=pool.submit(stream,conv3,c3,marker3,side(c3))
    for _ in range(100):
        if reqs(marker3):break
        time.sleep(.05)
    abandon=vote(conv3,c3,None)
    c3.refresh_from_db();before=c3.status
    newer=stream(conv3,None,'QA_NEWER_'+uuid.uuid4().hex)
    conv3.refresh_from_db();middle=[m.model_dump(mode='json') for m in conv3.messages]
    late=future.result()
conv3.refresh_from_db();c3.refresh_from_db()
save('late_completion',{'abandon_http':abandon.status_code,'closed_status':before,'new_turn_http':newer.status_code,'late_stream_http':late.status_code,'final_status':c3.status,'middle_messages':middle,'final_messages':[m.model_dump(mode='json') for m in conv3.messages],'champion_committed':c3.champion_committed})

conv4,c4=fresh('identity')
marker4='QA_IDENTITY_'+uuid.uuid4().hex+' [IDENTITY]'
r=stream(conv4,c4,marker4)
(ROOT/'artifacts/identity-stream.txt').write_text(r.text)
requests=reqs('QA_IDENTITY_')
model_tool_payloads=[m['content'] for rq in requests for m in rq['messages'] if m['role']=='tool']
save('identity_leak',{'http':r.status_code,'tool_exposed_to_model':any('review-champion' in str(t) for t in model_tool_payloads),'identity_present_in_client_stream':'review-champion' in r.text,'tool_payloads':model_tool_payloads})

with ThreadPoolExecutor(2) as pool:
    votes=[pool.submit(vote,conv,c,side(c)) for _ in range(2)]
    save('duplicate_vote',{'http':sorted(f.result().status_code for f in votes)})
# Deletion acts on our own synthetic conversation only.
r=client.delete(BASE+str(conv.pk)+'/')
c.refresh_from_db()
save('deletion_retention',{'delete_http':r.status_code,'conversation_fk':c.conversation_id,'retains_prompt':marker in json.dumps(c.champion_payload),'retains_challenger':bool(c.challenger_payload),'user_fk_remains':c.user_id is not None})

with transaction.atomic():
    exp=ArenaExperimentFactory(name='QA metrics transaction',is_active=False,champion_model_hrid='default-model',min_votes_for_conclusion=100)
    challenger=ArenaChallengerFactory(experiment=exp,model_hrid='challenger-model',input_price_eur_per_mtok=1,output_price_eur_per_mtok=2)
    for i in range(100):
        ArenaComparisonFactory(experiment=exp,champion_model_hrid='default-model',challenger_model_hrid='challenger-model',status='voted' if i<10 else 'pending',winner='challenger' if i<10 else '',champion_prompt_tokens=100,champion_completion_tokens=100,challenger_prompt_tokens=100,challenger_completion_tokens=100)
    result=arena_results.build_results(exp)
    cost_before=result['header']['total_cost_eur']
    challenger.input_price_eur_per_mtok=10;challenger.output_price_eur_per_mtok=20;challenger.save()
    result2=arena_results.build_results(exp)
    save('metrics',{'total':result['header']['total'],'voted':result['header']['voted'],'pending':result['header']['pending'],'displayed_vote_rate':result['header']['vote_rate'],'cost_before_price_edit':cost_before,'historical_cost_after_price_edit':result2['header']['total_cost_eur'],'51_of_100':arena_results._rate_block(51,100,100)})
    transaction.set_rollback(True)
client.close()
