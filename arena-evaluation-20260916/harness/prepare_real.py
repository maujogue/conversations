from configurations import importer
importer.install()
import django
django.setup()
from chat.models import ArenaExperiment
from chat.factories import ArenaExperimentFactory,ArenaChallengerFactory
ArenaExperiment.objects.update(is_active=False)
e=ArenaExperimentFactory(name='Albert API — smoke test réel, prix non renseignés',is_active=True,champion_model_hrid='default-model',sampling_rate=1,daily_cap_per_user=100,champion_input_price_eur_per_mtok=0,champion_output_price_eur_per_mtok=0)
ArenaChallengerFactory(experiment=e,model_hrid='challenger-model',input_price_eur_per_mtok=0,output_price_eur_per_mtok=0)
print('Created separate real-API experiment:',e.pk)
