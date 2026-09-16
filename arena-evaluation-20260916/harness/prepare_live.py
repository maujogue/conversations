import json
from pathlib import Path
from configurations import importer
importer.install()
import django
django.setup()
from django.test import Client
from django.conf import settings
from core.factories import UserFactory
from chat.factories import ArenaExperimentFactory, ArenaChallengerFactory
from chat.models import ArenaExperiment

root = Path(__file__).parent
user = UserFactory(sub='arena-review-admin', email='arena-review@example.test', full_name='Évaluation Arena', short_name='Évaluation', language='fr-fr', is_staff=True, is_superuser=True, allow_conversation_analytics=False, allow_smart_web_search=False)
ArenaExperiment.objects.update(is_active=False)
experiment = ArenaExperimentFactory(name='Évaluation technique — réponses simulées', champion_model_hrid='default-model', is_active=True, sampling_rate=1, daily_cap_per_user=10000, min_votes_for_conclusion=100, champion_input_price_eur_per_mtok='2', champion_output_price_eur_per_mtok='6')
ArenaChallengerFactory(experiment=experiment, model_hrid='challenger-model', input_price_eur_per_mtok='1', output_price_eur_per_mtok='2')
client = Client()
client.force_login(user, backend='django.contrib.auth.backends.ModelBackend')
cookie = client.cookies[settings.SESSION_COOKIE_NAME].value
(root / 'artifacts/live-auth.json').write_text(json.dumps({'cookie_name': settings.SESSION_COOKIE_NAME, 'cookie_value': cookie, 'user_id': str(user.pk), 'experiment_id': str(experiment.pk)}))
print('Created isolated review user and 100% sampling experiment:', experiment.pk)
