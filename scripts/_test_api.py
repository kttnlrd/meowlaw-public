"""Test script to explore legislation.gov.au API."""
from curl_cffi import requests
import re
import json

# Check the Details page for embedded data
url = 'https://www.legislation.gov.au/Details/C2004A00266'
r = requests.get(url, impersonate='chrome120', verify=False)
html = r.text

# Look for compilation-related data in HTML
patterns = ['currentCompilation', 'compilationNumber', 'compilationDate', 'compilation']
for p in patterns:
    count = html.lower().count(p.lower())
    if count > 0:
        print(f'Pattern "{p}" found {count} times')
        indices = [i for i in range(len(html)) if html.lower().startswith(p.lower(), i)]
        for idx in indices[:5]:
            start = max(0, idx - 80)
            end = min(len(html), idx + 150)
            print(f'  At {idx}: ...{html[start:end]}...')

# Check script tags for any JSON data
scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
for i, s in enumerate(scripts):
    s = s.strip()
    if not s or s.startswith('{') == False:
        continue
    if 'compilation' in s.lower() or 'current' in s.lower():
        print(f'\nScript {i} has relevant data ({len(s)} chars):')
        try:
            data = json.loads(s)
            print(json.dumps(data, indent=2)[:1000])
        except:
            print(s[:500])

# Also try the API with the Format/Download endpoint which might provide JSON
urls_to_try = [
    # These are the download/API-style URLs
    'https://www.legislation.gov.au/Details/C2004A00266/Download',
    'https://www.legislation.gov.au/Latest/C2004A00266',
    'https://www.legislation.gov.au/Series/C2004A00266',
]

for url in urls_to_try:
    r = requests.get(url, impersonate='chrome120', verify=False)
    ct = r.headers.get('content-type', '')
    print(f'\n{url}')
    print(f'  Status: {r.status_code}, CT: {ct}')
    if 'json' in ct.lower():
        try:
            data = r.json()
            if isinstance(data, dict):
                print(f'  Keys: {list(data.keys())[:20]}')
                print(json.dumps(data, indent=2)[:1000])
        except:
            pass
