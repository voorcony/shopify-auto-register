import os

from qcloud_cos import CosConfig, CosS3Client

config = CosConfig(
    Secret_id=os.environ['COS_SECRET_ID'],
    Secret_key=os.environ['COS_SECRET_KEY'],
    Region='ap-hongkong',
    Token=None,
    Scheme='https'
)
client = CosS3Client(config)
bucket = 'image-1305079617'

# Upload all downloaded images to COS
import os as os_mod
local_dir = '/home/agentuser/imported_images'
for fname in sorted(os_mod.listdir(local_dir)):
    local_path = os_mod.path.join(local_dir, fname)
    if not os_mod.path.isfile(local_path):
        continue
    cos_path = f'bag-variations/{fname}'
    with open(local_path, 'rb') as f:
        resp = client.put_object(Bucket=bucket, Body=f, Key=cos_path)
    status = resp.get('ETag', 'OK')[:20]
    print(f'✅ {fname} -> COS')

# Verify they're accessible
for fname in sorted(os_mod.listdir(local_dir)):
    url = f'https://image-1305079617.cos.ap-hongkong.myqcloud.com/bag-variations/{fname}'
    import urllib.request
    try:
        req = urllib.request.Request(url, method='HEAD')
        resp = urllib.request.urlopen(req, timeout=5)
        print(f'🔗 {fname}: {resp.status}')
    except Exception as e:
        print(f'❌ {fname}: {e}')

print('\nDone!')
