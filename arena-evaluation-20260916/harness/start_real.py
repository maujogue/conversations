import json,subprocess
from pathlib import Path
from dotenv import dotenv_values
from qa import env,ROOT,BACKEND,FRONTEND,PYTHON,NODE
c=dotenv_values('/home/lozhao/Projects/choix_model/common')
config=json.loads((ROOT/'live-models.json').read_text())
for m in config['models']:
    m['model_name']= 'mistral-small-3-2-24b-instruct-2506' if m['hrid']=='challenger-model' else c['AI_MODEL']
    m['human_readable_name']=m['model_name'];m['provider_name']='review-albert';m['supports_image']=False
    m['settings']={'temperature':0,'max_tokens':2048}
    m['system_prompt']='Tu aides les agents publics dans leurs tâches professionnelles. Réponds en français, respecte exactement les contraintes et n’invente pas de faits.'
config['providers']=[{'hrid':'review-albert','base_url':'environ.REVIEW_ALBERT_URL','api_key':'environ.REVIEW_ALBERT_KEY','kind':'openai','co2_handling':'albert'}]
(ROOT/'real-models.json').write_text(json.dumps(config))
env.update(REVIEW_ALBERT_URL=c['AI_BASE_URL'].rstrip('/')+'/',REVIEW_ALBERT_KEY=c['AI_API_KEY'],PYTHONPATH=str(ROOT)+':'+str(BACKEND),DJANGO_SETTINGS_MODULE='live_settings',DJANGO_CONFIGURATION='Review',FEATURE_FLAG_ARENA='ENABLED',LLM_CONFIGURATION_FILE_PATH=str(ROOT/'real-models.json'),PYTHON_SERVER_MODE='async',AWS_S3_DOMAIN_REPLACE='http://127.0.0.1:29000',VITE_API_ORIGIN='http://127.0.0.1:28072')
for name,args,cwd in [('backend-real',[PYTHON,'-B','-m','uvicorn','conversations.asgi:application','--host','127.0.0.1','--port','28072','--lifespan','off'],BACKEND),('frontend-real',[NODE,FRONTEND/'node_modules/vite/bin/vite.js','--host','127.0.0.1','--port','23001'],FRONTEND/'apps/conversations')]:
    f=(ROOT/'logs'/f'{name}-server.log').open('w')
    p=subprocess.Popen([str(x) for x in args],cwd=cwd,env=env,stdout=f,stderr=subprocess.STDOUT,start_new_session=True)
    (ROOT/f'{name}.pid').write_text(str(p.pid));print(name,p.pid)
