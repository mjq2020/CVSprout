"""Usage: VISIONFORGE_API_KEY=... python predict.py image.jpg"""
import os
import sys
import httpx
with open(sys.argv[1], 'rb') as image:
    response = httpx.post(os.getenv('VISIONFORGE_URL', 'http://127.0.0.1:8080') + '/predict',
        headers={'Authorization': 'Bearer ' + os.environ['VISIONFORGE_API_KEY']},
        files={'file': image}, timeout=120, trust_env=False)
response.raise_for_status()
print(response.json())
