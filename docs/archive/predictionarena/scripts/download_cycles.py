import json
import requests

endpoint = "https://www.predictionarena.ai/api/polymarket/cycles?offset=0&limit=200"

headers = {"Accept-Encoding": "gzip, deflate"}

result = requests.get(endpoint, headers=headers)
result.raise_for_status()
with open("polymarket_cycles.json", "w") as f:
    json.dump(result.json(), f, indent=4)
