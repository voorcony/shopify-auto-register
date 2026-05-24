#!/usr/bin/env python3
"""Modify sneaker and bags stores with gallery/ATC/footer changes."""

import re, sys, shutil

FOOTER = '''  <!-- Footer -->
  <div x-show="!product" style="background:#fafafa;border-top:1px solid #eee;margin-top:24px;padding:32px 16px 80px">
    <div style="max-width:480px;margin:0 auto;text-align:center">
      <div style="margin-bottom:20px">
        <p style="font-size:0.8125rem;color:#666;margin:0 0 6px">Contact Us</p>
        <p style="font-size:0.875rem;color:#111;margin:0 0 4px">service@superfiremarket.com</p>
        <p style="font-size:0.875rem;color:#111;margin:0">WhatsApp: +1 (369) 237-7862</p>
      </div>
      <div style="margin-bottom:16px">
        <p style="font-size:0.75rem;color:#999;margin:0 0 10px;text-transform:uppercase;letter-spacing:0.06em">We Accept</p>
        <div style="display:flex;justify-content:center;gap:8px;flex-wrap:wrap">
          <svg viewBox="0 0 48 32" width="38" height="25" fill="none" xmlns="http://www.w3.org/2000/svg"><rect width="48" height="32" rx="4" fill="#1A1F71"/><path d="M19.5 22.5h-3.5l2.5-13h3.5l-2.5 13zM29 12.5c-1.5-.5-3-1-4.5-.5-1.5.5-2.5 1.5-2.5 3 0 1.5 1.5 2.5 2.5 3 1 .5 1.5 1.5 1 2.5-.5 1-1.5 1.5-2.5 1.5-1.5 0-3-.5-4-1l-.5 2.5c1.5.5 3 1 4.5 1 2 0 3.5-1 4-2.5.5-1.5 0-3-1.5-4-1-.5-2-1-2-2 0-.5.5-1 1.5-1.5 1-.5 2-.5 3 0l.5-2z" fill="#fff"/><path d="M36 13c0 0-1.5 4-2 5.5l-.5-3c0-1-.5-2-1.5-2.5l1.5 7.5h3l2-7.5h-2.5z" fill="#fff"/><path d="M16 10c-1 0-2 .5-2.5 1.5l-3.5 11h3.5l.5-1.5h4l.5 1.5h3.5l-3-13H16zm-1 8.5l1.5-5 1 5h-2.5z" fill="#fff"/><rect x="0" y="0" width="48" height="32" rx="4" fill="none" stroke="#ddd" stroke-width="0.5"/></svg>
          <svg viewBox="0 0 48 32" width="38" height="25" fill="none" xmlns="http://www.w3.org/2000/svg"><rect width="48" height="32" rx="4" fill="#fff"/><circle cx="18" cy="16" r="9" fill="#EB001B"/><circle cx="30" cy="16" r="9" fill="#F79E1B"/><path d="M24 9a9 9 0 000 14 9 9 0 000-14z" fill="#FF5F00"/><rect x="0" y="0" width="48" height="32" rx="4" fill="none" stroke="#ddd" stroke-width="0.5"/></svg>
          <svg viewBox="0 0 48 32" width="38" height="25" fill="none" xmlns="http://www.w3.org/2000/svg"><rect width="48" height="32" rx="4" fill="#2E77BC"/><path d="M8 15h4l2 3 2-3h12v11h-3v-8l-2 3-2-3v8h-3v-8l-2 3-2-3H8v11H5V15h3z" fill="#fff"/><rect x="0" y="0" width="48" height="32" rx="4" fill="none" stroke="#ddd" stroke-width="0.5"/></svg>
          <svg viewBox="0 0 48 32" width="38" height="25" fill="none" xmlns="http://www.w3.org/2000/svg"><rect width="48" height="32" rx="4" fill="#fff"/><path d="M17.5 10.5h-4.5l-2 13h3l.5-3.5h2.5c2.5 0 4.5-2 5-4.5.5-2.5-1-5-3.5-5h-1z" fill="#003087"/><path d="M18 11h-2.5l-1.5 9h2.5c2 0 3.5-1.5 4-3.5.5-2-.5-4.5-2.5-5.5z" fill="#009CDE"/><rect x="0" y="0" width="48" height="32" rx="4" fill="none" stroke="#ddd" stroke-width="0.5"/></svg>
          <svg viewBox="0 0 48 32" width="38" height="25" fill="none" xmlns="http://www.w3.org/2000/svg"><rect width="48" height="32" rx="4" fill="#fff"/><circle cx="15" cy="16" r="9" fill="#F58220"/><rect x="27" y="11" width="16" height="10" rx="2" fill="#231F20"/><rect x="0" y="0" width="48" height="32" rx="4" fill="none" stroke="#ddd" stroke-width="0.5"/></svg>
        </div>
      </div>
      <p style="font-size:0.6875rem;color:#bbb;margin:0">&copy; 2026 SOLEVORA. All rights reserved.</p>
    </div>
  </div>'''

ATC_BAR = '''    <!-- Fixed ATC Bar -->
    <div class="pd-atc-bar" x-show="product">
      <div class="qty-row">
        <button @click="qty=Math.max(1,qty-1)">−</button>
        <span x-text="qty"></span>
        <button @click="qty++">+</button>
      </div>
      <button class="atc-btn" @click="addToCart()" :disabled="!selVar">
        <span x-text="selVar ? 'Add to Bag — $'+(selVar.p*qty).toFixed(2) : 'Select a Size'"></span>
      </button>
    </div>'''

ATC_BAR_BAGS = '''    <!-- Fixed ATC Bar -->
    <div class="pd-atc-bar" x-show="product">
      <div class="qty-row">
        <button @click="qty=Math.max(1,qty-1)">−</button>
        <span x-text="qty"></span>
        <button @click="qty++">+</button>
      </div>
      <button class="atc-btn" @click="addToCart()" :disabled="!selVar">
        <span x-text="selVar ? 'Add to Bag — $'+(selVar.p*qty).toFixed(2) : 'Select an Option'"></span>
      </button>
    </div>'''

PD_ATC_CSS = '''/* Fixed ATC bar on product detail */
.pd-atc-bar{position:fixed;bottom:0;left:0;right:0;z-index:90;background:#fff;border-top:1px solid #eee;padding:10px 16px;display:flex;align-items:center;gap:12px;padding-bottom:calc(10px + env(safe-area-inset-bottom,0px))}
.pd-atc-bar .qty-row{display:flex;align-items:center;gap:0;border:1px solid #ddd;border-radius:4px;flex-shrink:0}
.pd-atc-bar .qty-row button{width:36px;height:36px;border:none;background:none;color:#111;font-size:1rem;cursor:pointer}
.pd-atc-bar .qty-row span{width:32px;text-align:center;font-size:0.8125rem;font-weight:500}
.pd-atc-bar .atc-btn{flex:1;padding:12px;background:#111;color:#fff;font-size:0.875rem;font-weight:600;border-radius:4px;border:none;cursor:pointer}
.pd-atc-bar .atc-btn:disabled{opacity:0.25;cursor:default}
.pd-atc-bar .atc-btn:active{opacity:0.85}

'''


def modify_sneaker(filepath):
    with open(filepath, 'r') as f:
        content = f.read()
    
    # 1. Remove position:sticky from .pd-gallery
    content = content.replace(
        '.pd-gallery{position:sticky;top:52px;z-index:10;background:#f5f5f5}',
        '.pd-gallery{z-index:10;background:#f5f5f5}'
    )
    
    # 2. Change pd-body padding 100px -> 80px
    content = content.replace(
        '.pd-body{padding:20px 16px 100px}',
        '.pd-body{padding:20px 16px 80px}'
    )
    
    # 3. Remove old pd-body .qty-row CSS and .atc-btn CSS
    # Remove the qty-row CSS block under pd-body
    old_qty_css = ".pd-body .qty-row{display:flex;align-items:center;gap:0;margin-bottom:16px;border:1px solid #ddd;border-radius:4px;width:fit-content}\n.pd-body .qty-row button{width:40px;height:40px;border:none;background:none;color:#111;font-size:1.125rem;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:background .12s}\n.pd-body .qty-row button:active{background:#f5f5f5}\n.pd-body .qty-row span{width:40px;text-align:center;font-size:0.875rem;font-weight:500;border-left:1px solid #ddd;border-right:1px solid #ddd;line-height:40px}"
    # Also need to remove the blank line before it
    content = content.replace('\n' + old_qty_css, '')
    
    old_atc_css = ".pd-body .atc-btn{width:100%;padding:14px;background:#111;color:#fff;font-size:0.9375rem;font-weight:600;letter-spacing:0.02em;border-radius:4px;border:none;cursor:pointer;transition:opacity .12s}\n.pd-body .atc-btn:active{opacity:0.85}\n.pd-body .atc-btn:disabled{opacity:0.25;cursor:default}"
    content = content.replace('\n' + old_atc_css, '')
    
    # 4. Add new pd-atc-bar CSS before the accordion trust section
    content = content.replace(
        '\n/* Accordion Trust */',
        PD_ATC_CSS + '/* Accordion Trust */'
    )
    
    # 5. Remove old qty-row HTML from pd-body
    old_qty_html = '''      <div class="qty-row">
        <button @click="qty=Math.max(1,qty-1)">−</button>
        <span x-text="qty"></span>
        <button @click="qty++">+</button>
      </div>
'''
    if old_qty_html in content:
        content = content.replace(old_qty_html, '')
    else:
        print(f"WARNING: Could not find old qty-row HTML in {filepath}")
        # Try without trailing newline
        content = content.replace(old_qty_html.rstrip(), '')
    
    # 6. Remove old atc-btn HTML from pd-body
    old_atc_html = '''      <button class="atc-btn" @click="addToCart()" :disabled="!selVar">
        <span x-text="selVar ? 'Add to Bag — $'+(selVar.p*qty).toFixed(2) : 'Select a Size'"></span>
      </button>
'''
    if old_atc_html in content:
        content = content.replace(old_atc_html, '')
    else:
        print(f"WARNING: Could not find old atc-btn HTML in {filepath}")
        # Try alternative
        alt_atc_html = '''      <button class="atc-btn" @click="addToCart()" :disabled="!selVar">
        <span x-text="selVar ? 'Add to Bag — $'+(selVar.p*qty).toFixed(2) : 'Select a Size'"></span>
      </button>'''
        if alt_atc_html in content:
            content = content.replace(alt_atc_html, '')
            content = content.replace('\n\n    </div>', '\n    </div>')
    
    # 7. Add fixed ATC bar after pd-body closing div
    # Find pattern: closing of pd-body then related section
    content = content.replace(
        '    </div>\n\n    <div class="related"',
        '    </div>\n' + ATC_BAR + '\n\n    <div class="related"'
    )
    
    # 8. Add footer before </div>\n\n<script> (closing x-data div)
    footer_insert = '\n' + FOOTER + '\n'
    content = content.replace(
        '\n</div>\n\n<script>',
        footer_insert + '\n</div>\n\n<script>'
    )
    
    with open(filepath, 'w') as f:
        f.write(content)
    
    print(f"✅ Modified {filepath}")


def modify_bags(filepath):
    """Same as sneaker but with 'Select an Option' text and no sale/compare-at prices in the ATC bar text."""
    with open(filepath, 'r') as f:
        content = f.read()
    
    # 1. Remove position:sticky from .pd-gallery
    content = content.replace(
        '.pd-gallery{position:sticky;top:52px;z-index:10;background:#f5f5f5}',
        '.pd-gallery{z-index:10;background:#f5f5f5}'
    )
    
    # 2. Change pd-body padding 100px -> 80px
    content = content.replace(
        '.pd-body{padding:20px 16px 100px}',
        '.pd-body{padding:20px 16px 80px}'
    )
    
    # 3. Remove old pd-body .qty-row CSS and .atc-btn CSS
    old_qty_css = ".pd-body .qty-row{display:flex;align-items:center;gap:0;margin-bottom:16px;border:1px solid #ddd;border-radius:4px;width:fit-content}\n.pd-body .qty-row button{width:40px;height:40px;border:none;background:none;color:#111;font-size:1.125rem;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:background .12s}\n.pd-body .qty-row button:active{background:#f5f5f5}\n.pd-body .qty-row span{width:40px;text-align:center;font-size:0.875rem;font-weight:500;border-left:1px solid #ddd;border-right:1px solid #ddd;line-height:40px}"
    content = content.replace('\n' + old_qty_css, '')
    
    old_atc_css = ".pd-body .atc-btn{width:100%;padding:14px;background:#111;color:#fff;font-size:0.9375rem;font-weight:600;letter-spacing:0.02em;border-radius:4px;border:none;cursor:pointer;transition:opacity .12s}\n.pd-body .atc-btn:active{opacity:0.85}\n.pd-body .atc-btn:disabled{opacity:0.25;cursor:default}"
    content = content.replace('\n' + old_atc_css, '')
    
    # 4. Add new pd-atc-bar CSS
    content = content.replace(
        '\n/* Accordion Trust */',
        PD_ATC_CSS + '/* Accordion Trust */'
    )
    
    # 5. Remove old qty-row HTML
    old_qty_html = '''      <div class="qty-row">
        <button @click="qty=Math.max(1,qty-1)">−</button>
        <span x-text="qty"></span>
        <button @click="qty++">+</button>
      </div>
'''
    if old_qty_html in content:
        content = content.replace(old_qty_html, '')
    else:
        print(f"WARNING: Could not find old qty-row HTML in {filepath}")
        content = content.replace(old_qty_html.rstrip(), '')
    
    # 6. Remove old atc-btn HTML
    old_atc_html = '''      <button class="atc-btn" @click="addToCart()" :disabled="!selVar">
        <span x-text="selVar ? 'Add to Bag — $'+(selVar.p*qty).toFixed(2) : 'Select an Option'"></span>
      </button>
'''
    if old_atc_html in content:
        content = content.replace(old_atc_html, '')
    else:
        print(f"WARNING: Could not find old atc-btn HTML in {filepath} (bags)")
    
    # 7. Add fixed ATC bar after pd-body closing div
    content = content.replace(
        '    </div>\n\n    <div class="related"',
        '    </div>\n' + ATC_BAR_BAGS + '\n\n    <div class="related"'
    )
    
    # 8. Add footer
    footer_insert = '\n' + FOOTER + '\n'
    content = content.replace(
        '\n</div>\n\n<script>',
        footer_insert + '\n</div>\n\n<script>'
    )
    
    with open(filepath, 'w') as f:
        f.write(content)
    
    print(f"✅ Modified {filepath}")


def modify_landing(filepath):
    """Just add footer to the luxury store."""
    with open(filepath, 'r') as f:
        content = f.read()
    
    # Add footer before </div>\n\n<script>
    footer_insert = '\n' + FOOTER + '\n'
    content = content.replace(
        '\n</div>\n\n<script>',
        footer_insert + '\n</div>\n\n<script>'
    )
    
    with open(filepath, 'w') as f:
        f.write(content)
    
    print(f"✅ Modified {filepath}")


if __name__ == '__main__':
    # Make backups
    for src in sys.argv[1:]:
        shutil.copy2(src, src + '.bak')
    
    for src in sys.argv[1:]:
        if 'sneakers' in src or 'sneaker' in src:
            modify_sneaker(src)
        elif 'bags' in src:
            modify_bags(src)
        elif 'landing' in src:
            modify_landing(src)
