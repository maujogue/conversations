import json
from pathlib import Path
import sys
import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).parent
config = dotenv_values('/home/lozhao/Projects/choix_model/common')
base = config['AI_BASE_URL'].rstrip('/')
key = config['AI_API_KEY']
with httpx.Client(timeout=40, headers={'Authorization': 'Bearer ' + key}) as client:
    response = client.get(base + '/models')
    if response.status_code != 200:
        print('Model inventory HTTP status:', response.status_code)
        sys.exit(1)
    inventory = response.json()
    (ROOT / 'artifacts/real-model-inventory.json').write_text(json.dumps(inventory, ensure_ascii=False, indent=2))
    print('Configured model:', config.get('AI_MODEL'))
    print('Available model IDs:')
    for item in inventory.get('data', []):
        print(item.get('id'))
