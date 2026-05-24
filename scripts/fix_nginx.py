#!/usr/bin/env python3
import sys

with open('/etc/nginx/sites-enabled/sneakers-saas', 'r') as f:
    data = f.read()

old = '    location /data/ {\n        add_header Cache-Control "public, max-age=3600";\n    }\n\n    location / {\n        try_files $uri $uri/ /index.html;\n    }\n}'

new = '    location /data/ {\n        add_header Cache-Control "public, max-age=3600";\n    }\n\n    location /api/ {\n        proxy_pass http://127.0.0.1:8099;\n        proxy_set_header Host $host;\n        proxy_set_header X-Real-IP $remote_addr;\n        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n    }\n\n    location / {\n        try_files $uri $uri/ /index.html;\n    }\n}'

if old in data:
    data = data.replace(old, new, 1)
    with open('/etc/nginx/sites-enabled/sneakers-saas', 'w') as f:
        f.write(data)
    print('nginx config updated')
else:
    print('ERROR: Could not find replacement point')
    sys.exit(1)
