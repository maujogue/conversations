import concurrent.futures
import json
from pathlib import Path
import time
import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).parent
config = dotenv_values('/home/lozhao/Projects/choix_model/common')
tasks = [
    ('mail', 'Réécris ce brouillon en un courriel professionnel de 100 mots maximum. Ne crée aucune information. Brouillon : Bonjour Madame Martin. On a reçu votre demande de formation. Il manque votre justificatif. Merci de l’envoyer avant le 21 septembre 2026 à 17 h. Votre dossier sera étudié après réception. Aucune décision d’acceptation n’a été prise. Signer : Le service formation.'),
    ('actions', 'À partir des notes suivantes, rends uniquement un tableau Markdown avec les colonnes Action, Responsable, Échéance, Statut. Écris « Non précisé » quand une information manque. Notes : Réunion du 14 septembre 2026. Léa doit vérifier les 18 dossiers avant le 18 septembre. Karim préparera une synthèse, mais aucune date n’a été fixée. Le responsable de la mise à jour du guide reste à désigner ; livraison prévue le 25 septembre. La formation du 22 septembre est annulée, ne la présente pas comme une action à réaliser.'),
    ('calculation', 'Cas fictif. Une administration utilise 80 licences à 12 € par mois et par licence, pendant 12 mois. Une remise de 15 % porte uniquement sur les licences. La migration coûte 1 800 € une seule fois. Tous les montants sont hors taxes ; ne calcule pas la TVA. Calcule le coût des licences avant et après remise, le total de première année, le total de deuxième année si rien ne change, et le dépassement éventuel du budget de première année de 11 000 €. Montre les opérations, en moins de 160 mots.'),
]
models = [config['AI_MODEL'], 'mistral-small-3-2-24b-instruct-2506', 'ministral-3-8b-instruct-2512']
system = 'Tu es un assistant pour des agents publics. Réponds en français, de manière concise et exacte. Respecte les seules informations fournies et les contraintes de la demande.'

def call(model, task):
    name, prompt = task
    result = {'model': model, 'task': name, 'prompt': prompt, 'system': system, 'synthetic': True}
    started = time.perf_counter()
    first = None
    text = []
    reasoning = []
    try:
        with httpx.Client(timeout=httpx.Timeout(180, connect=20), headers={'Authorization': 'Bearer ' + config['AI_API_KEY']}) as client:
            with client.stream('POST', config['AI_BASE_URL'].rstrip('/') + '/chat/completions', json={'model': model, 'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': prompt}], 'stream': True, 'stream_options': {'include_usage': True}, 'temperature': 0, 'max_tokens': 2048}) as response:
                result['http_status'] = response.status_code
                if response.status_code != 200:
                    result['error'] = response.read().decode(errors='replace')[:1000]
                else:
                    for line in response.iter_lines():
                        if not line.startswith('data: '): continue
                        if line[6:] == '[DONE]': break
                        chunk = json.loads(line[6:])
                        if chunk.get('usage'): result['usage'] = chunk['usage']
                        for choice in chunk.get('choices', []):
                            delta = choice.get('delta', {})
                            content = delta.get('content')
                            if content:
                                if first is None: first = time.perf_counter()
                                text.append(content if isinstance(content, str) else json.dumps(content, ensure_ascii=False))
                            if delta.get('reasoning_content'): reasoning.append(delta['reasoning_content'])
                            if choice.get('finish_reason'): result['finish_reason'] = choice['finish_reason']
        result['answer'] = ''.join(text)
        result['reasoning_characters'] = sum(map(len, reasoning))
    except Exception as exc:
        result['error'] = type(exc).__name__
    result['duration_s'] = round(time.perf_counter() - started, 3)
    result['first_text_s'] = round(first - started, 3) if first else None
    return result

results = []
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    futures = [pool.submit(call, model, task) for task in tasks for model in models]
    for future in concurrent.futures.as_completed(futures):
        row = future.result(); results.append(row)
        (ROOT / 'artifacts/real-benchmark.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
        print(row['model'], row['task'], 'HTTP', row.get('http_status'), 'duration', row['duration_s'], 'first_text', row['first_text_s'], 'characters', len(row.get('answer', '')), flush=True)
print('Completed', len(results), 'controlled synthetic requests. This is a smoke evaluation, not a statistically valid ranking.', flush=True)
