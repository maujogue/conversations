import json
from pathlib import Path
from configurations import importer
importer.install()
import django
django.setup()
from chat.models import ArenaExperiment,ArenaComparison
ROOT=Path(__file__).parent
fields=['id','conversation_id','status','winner','champion_error','challenger_error','champion_finished_at','challenger_finished_at','champion_committed','champion_latency_ms','challenger_latency_ms','champion_first_token_ms','challenger_first_token_ms','champion_prompt_tokens','champion_completion_tokens','challenger_prompt_tokens','challenger_completion_tokens','champion_co2_impact','challenger_co2_impact']
real=list(ArenaComparison.objects.filter(experiment__name__startswith='Albert API').values(*fields))
failures=list(ArenaComparison.objects.exclude(challenger_error='').values(*fields))
(ROOT/'artifacts/real-app-records.json').write_text(json.dumps(real,default=str,indent=2))
(ROOT/'artifacts/failure-classification.json').write_text(json.dumps(failures,default=str,indent=2))
print('Real integration rows:',[(str(r['id']),r['status'],r['winner']) for r in real])
print('Failure rows:',[(str(r['id']),r['status'],r['challenger_error']) for r in failures])
auth=json.loads((ROOT/'artifacts/live-auth.json').read_text())
ArenaExperiment.objects.update(is_active=False)
ArenaExperiment.objects.filter(pk=auth['experiment_id']).update(is_active=True)
