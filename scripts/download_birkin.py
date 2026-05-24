import json, urllib.request, os, re
from concurrent.futures import ThreadPoolExecutor

os.makedirs('/home/agentuser/imported_images', exist_ok=True)

# ===== BIRKIN 30 - rare.gallery =====
birkin_images = [
    ('https://cdn.shopify.com/s/files/1/0569/8201/2109/files/202509-008_1.webp?v=1758253321', 'birkin30-togo-etoupe'),
    ('https://cdn.shopify.com/s/files/1/0569/8201/2109/files/202509-008_2.webp?v=1758253321', 'birkin30-epsom-black'),
    ('https://cdn.shopify.com/s/files/1/0569/8201/2109/files/202509-008_3.webp?v=1758253321', 'birkin30-togo-gold'),
    ('https://cdn.shopify.com/s/files/1/0569/8201/2109/files/202509-008_4.webp?v=1758253321', 'birkin30-togo-orange'),
    ('https://cdn.shopify.com/s/files/1/0569/8201/2109/files/202509-008_5.webp?v=1758253321', 'birkin30-clemence-white'),
    ('https://cdn.shopify.com/s/files/1/0569/8201/2109/files/202509-008_6.webp?v=1758253321', 'birkin30-color-6'),
    ('https://cdn.shopify.com/s/files/1/0569/8201/2109/files/202509-008_7.webp?v=1758253321', 'birkin30-color-7'),
    ('https://cdn.shopify.com/s/files/1/0569/8201/2109/files/202509-008_8.webp?v=1758253321', 'birkin30-color-8'),
]

def download(url, name):
    path = f'/home/agentuser/imported_images/{name}.webp'
    try:
        urllib.request.urlretrieve(url, path)
        size = os.path.getsize(path)
        print(f'✅ {name}: {size/1024:.0f}KB')
    except Exception as e:
        print(f'❌ {name}: {e}')

with ThreadPoolExecutor(max_workers=4) as pool:
    pool.map(lambda args: download(*args), birkin_images)

print(f'\nTotal: {len(os.listdir("/home/agentuser/imported_images"))} images downloaded')
