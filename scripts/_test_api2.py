"""Test script to explore legislation.gov.au API v2."""
from curl_cffi import requests
import re
import json

# Get the Details page for ITAA 1997 (C2004A00266)
url = 'https://www.legislation.gov.au/Details/C2004A00266'
r = requests.get(url, impersonate='chrome120', verify=False)
html = r.text

# Extract the embedded JSON data - it's in a script tag
scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
for i, s in enumerate(scripts):
    s = s.strip()
    if not s:
        continue
    if s.startswith('{'):
        data = json.loads(s)
        # Look for API URLs
        for key in data:
            if 'api.prod' in key:
                print(f'API URL found: {key[:150]}')
                val = data[key]
                if isinstance(val, dict):
                    print(f'  Value keys: {list(val.keys())[:15]}')
                print()
                break

print('---')
print('Looking for title API data...')
for key in sorted(data.keys()):
    if 'titles' in key.lower() and 'C2004A00266' in key:
        print(f'\nKEY: {key[:200]}')
        val = data[key]
        if isinstance(val, dict):
            print(json.dumps(val, indent=2)[:2000])
        elif isinstance(val, list):
            print(f'  List of {len(val)} items')
            if val:
                print(json.dumps(val[0], indent=2)[:1000])
        else:
            print(f'  {val}')
