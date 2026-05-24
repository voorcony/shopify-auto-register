#!/usr/bin/env python3
"""Update footer on all 3 stores: remove x-show, replace payment icons."""

import subprocess
import sys

HOST = "ubuntu@43.130.2.243"
PASS = "ZHOUjiahao1!"
SSH = ["sshpass", "-p", PASS, "ssh", HOST]

FILES = [
    "/var/www/sneakers-saas/index.html",
    "/var/www/bags-saas/index.html",
    "/var/www/landing/landing.html",
]

NEW_ICONS = '''        <div style="display:flex;justify-content:center;gap:4px;flex-wrap:wrap;align-items:center">
          <!-- Visa -->
          <svg viewBox="0 0 38 24" width="38" height="24" xmlns="http://www.w3.org/2000/svg" role="img"><title>Visa</title><rect width="38" height="24" rx="3" fill="#1A1F71"/><path d="M15.2 16.8H12l2-10.5h3.2l-2 10.5zM23.2 8.4c-1.2-.4-2.4-.8-3.6-.4-1.2.4-2 1.2-2 2.4 0 1.2 1.2 2 2 2.4.8.4 1.2 1.2.8 2-.4.8-1.2 1.2-2 1.2-1.2 0-2.4-.4-3.2-.8l-.4 2c1.2.4 2.4.8 3.6.8 1.6 0 2.8-.8 3.2-2 .4-1.2 0-2.4-1.2-3.2-.8-.4-1.6-.8-1.6-1.6 0-.4.4-.8 1.2-1.2.8-.4 1.6-.4 2.4 0l.4-2zM28.8 8.4c0 0-1.2 3.2-1.6 4.4l-.4-2.4c0-.8-.4-1.6-1.2-2l1.2 6h2.4l1.6-6h-2zM10.4 6.4c-.8 0-1.6.4-2 .8L6 17.2h3.2l.4-1.2h3.2l.4 1.2h3.2l-2.8-10.8h-2.8zm-.4 6l1.2-4 .8 4H10z" fill="#fff"/></svg>
          <!-- Mastercard -->
          <svg viewBox="0 0 38 24" width="38" height="24" xmlns="http://www.w3.org/2000/svg" role="img"><title>Mastercard</title><rect width="38" height="24" rx="3" fill="#fff"/><circle cx="14" cy="12" r="7" fill="#EB001B" opacity=".9"/><circle cx="24" cy="12" r="7" fill="#F79E1B" opacity=".9"/><path d="M19 7.2c1.6 1.2 2.6 3 2.6 4.8s-1 3.6-2.6 4.8c-1.6-1.2-2.6-3-2.6-4.8s1-3.6 2.6-4.8z" fill="#FF5F00"/><rect width="38" height="24" rx="3" fill="none" stroke="#ddd" stroke-width=".5"/></svg>
          <!-- American Express -->
          <svg viewBox="0 0 38 24" width="38" height="24" xmlns="http://www.w3.org/2000/svg" role="img"><title>American Express</title><rect width="38" height="24" rx="3" fill="#2E77BC"/><path d="M6 9.5h4l1.5 2.5 1.5-2.5h4v2.5h-1V10l-1.5 2.5H13L11.5 10v2H9.5v-2L8 12.5H6.5L5 10v2.5H4V9.5h2zm13 0h5l1.5 2.5L27 9.5h4l-3 5.5 3 5.5h-4l-1.5-2.5L24 20.5h-5l3-5.5-3-5.5z" fill="#fff"/><rect width="38" height="24" rx="3" fill="none" stroke="#ddd" stroke-width=".5"/></svg>
          <!-- Discover -->
          <svg viewBox="0 0 38 24" width="38" height="24" xmlns="http://www.w3.org/2000/svg" role="img"><title>Discover</title><rect width="38" height="24" rx="3" fill="#fff"/><circle cx="13" cy="12" r="7" fill="#F58220"/><rect x="23" y="8" width="12" height="8" rx="1.5" fill="#231F20"/><text x="24" y="14" font-size="5" fill="#fff" font-weight="bold" font-family="sans-serif">DISCOVER</text><rect width="38" height="24" rx="3" fill="none" stroke="#ddd" stroke-width=".5"/></svg>
          <!-- PayPal -->
          <svg viewBox="0 0 38 24" width="38" height="24" xmlns="http://www.w3.org/2000/svg" role="img"><title>PayPal</title><rect width="38" height="24" rx="3" fill="#fff"/><path d="M12.5 6.5H9l-1.5 10h2.5l.5-3h2c2 0 3.5-1.5 4-3.5s-.5-4-2.5-4h-1.5zm.5 3c0 1.5-1 2.5-2 2.5h-1.5l.5-3h1.5c1 0 1.5.5 1.5 1.5z" fill="#003087"/><path d="M16 14l1-5.5c.5-2.5 2-3.5 4-3.5h1.5l-.5 2h-1c-1.5 0-2.5 1-3 2.5L17 14h-1z" fill="#009CDE"/><rect width="38" height="24" rx="3" fill="none" stroke="#ddd" stroke-width=".5"/></svg>
          <!-- Apple Pay -->
          <svg viewBox="0 0 38 24" width="38" height="24" xmlns="http://www.w3.org/2000/svg" role="img"><title>Apple Pay</title><rect width="38" height="24" rx="3" fill="#000"/><path d="M10.8 8.8c-.5.6-1.3 1-2 1-.2-.8.2-1.6.6-2.1.5-.5 1.2-.9 1.9-.9.2.7-.1 1.5-.5 2zm.5 1c-1 0-1.8.5-2.3.5-.5 0-1.2-.5-2-.5-1.7 0-3.3 1.5-3.3 3.7 0 2.2 1.6 4.5 2.8 4.5.7 0 1.2-.5 2-.5s1.3.5 2.2.5c1.6 0 3-2.2 3-4.3 0-.1 0-.2-.1-.3-1-.5-1.7-1.5-1.7-2.7 0-1.2.6-2.2 1.5-2.7-.3-.5-1-.9-1.6-.9zM17.5 8c0 .2-.1.4-.2.6-1.2 1-2 2.5-2 4.1 0 1.8.9 3.4 2.2 4.3-.2.6-.4 1.1-.7 1.6-.4.8-1 1.8-1.8 1.8-.3 0-.6-.1-.9-.3l-.3-.1c-.3-.1-.6-.2-.9-.2-.4 0-.7.1-1 .2l-.3.1c-.3.1-.5.2-.8.2-.9 0-1.7-1.1-2.2-1.9C8.8 18 8 15.9 8 13.9c0-3.3 2.1-5 4.2-5 .8 0 1.5.3 2.1.5.4.1.7.2 1 .2.3 0 .7-.1 1-.2.5-.2 1-.4 1.4-.4h.3c.3 0 .4.1.5.2v.1-.1zM23 14.7l-2.5-6.7h1.5l1.5 4.2c.2.5.3 1 .5 1.5h.1c.1-.5.3-1 .5-1.5l1.5-4.2h1.5l-2.5 6.7V18H23v-3.3z" fill="#fff"/><rect width="38" height="24" rx="3" fill="none" stroke="#ddd" stroke-width=".5"/></svg>
          <!-- Google Pay -->
          <svg viewBox="0 0 38 24" width="38" height="24" xmlns="http://www.w3.org/2000/svg" role="img"><title>Google Pay</title><rect width="38" height="24" rx="3" fill="#fff"/><path d="M9 10.5v-2h4.5c.5 0 1 .2 1.4.5l1.1-1.1C15 7 13.5 6.3 12 6.3c-2.4 0-4.5 1.7-4.5 4.2s2.1 4.2 4.5 4.2c1.2 0 2.3-.4 3.1-1.2.9-.8 1.4-2 1.4-3.2 0-.4 0-.7-.1-1H9zm7 2.5c-.1.3-.3.6-.5.9-.4.4-1 .7-1.7.7-1.2 0-2.2-.8-2.5-2h4.7v.4zM20 7.3c-1.5 0-2.8.7-3.6 1.8l1.4 1c.5-.7 1.3-1.1 2.2-1.1.7 0 1.3.2 1.7.6.4-.2.8-.3 1.2-.6 0 0-.1 1.2-1.6 1.2-.7 0-1.2-.2-1.7-.5l-1.3 1c.8.7 1.9 1.1 3 1.1 1.7 0 3.1-.9 3.7-2.5.3-.7.4-1.4.4-1.9 0-.2 0-.4-.1-.5h-3.3v1.4z" fill="#4285F4"/><path d="M28 14.8l-3-6.5h1.5l2.2 5 2.2-5h1.5l-3 6.5H28z" fill="#000"/><rect width="38" height="24" rx="3" fill="none" stroke="#ddd" stroke-width=".5"/></svg>
          <!-- Shop Pay -->
          <svg viewBox="0 0 38 24" width="38" height="24" xmlns="http://www.w3.org/2000/svg" role="img"><title>Shop Pay</title><rect width="38" height="24" rx="3" fill="#5E8E3E"/><path d="M12 7c-1.5 1-2.5 2.5-2.5 4.5S10.5 16 12 17c1.5-1 2.5-2.5 2.5-4.5S13.5 8 12 7z" fill="#fff"/><path d="M19.5 7c-1.5 1-2.5 2.5-2.5 4.5S18 16 19.5 17c1.5-1 2.5-2.5 2.5-4.5S21 8 19.5 7z" fill="#fff" opacity=".5"/><rect width="38" height="24" rx="3" fill="none" stroke="#ddd" stroke-width=".5"/></svg>
        </div>'''


def run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ERROR: {result.stderr}")
        return None
    return result.stdout


def update_file(filepath):
    print(f"\n=== Updating {filepath} ===")
    
    # Read the file
    cat_cmd = SSH + ["cat", filepath]
    content = run(cat_cmd)
    if content is None:
        return False
    
    # 1. Remove x-show="!product" from footer div
    # The current div: <div x-show="!product" style="background:#fafafa;border-top:1px solid #eee;margin-top:24px;padding:32px 16px 80px">
    old_footer_tag = '<div x-show="!product" style="background:#fafafa;border-top:1px solid #eee;margin-top:24px;padding:32px 16px 80px">'
    new_footer_tag = '<div style="background:#fafafa;border-top:1px solid #eee;margin-top:24px;padding:32px 16px 80px">'
    
    if old_footer_tag in content:
        content = content.replace(old_footer_tag, new_footer_tag)
        print("  ✓ Removed x-show=\"!product\" from footer div")
    else:
        print("  ✗ Could not find footer div with x-show=\"!product\"")
        print(f"    Searching for: {old_footer_tag[:60]}...")
        return False
    
    # 2. Replace payment icons
    # Find the old icons block: starts with <div style="display:flex;justify-content:center;gap:8px;flex-wrap:wrap">
    # and ends with </div> (the closing div of the icons container)
    # But we need to match the exact multi-line content
    old_icons_start = '<div style="display:flex;justify-content:center;gap:8px;flex-wrap:wrap">'
    old_icons_end = '        </div>'
    
    if old_icons_start in content:
        # Find start and end position
        start_idx = content.find(old_icons_start)
        # Find the matching end div after the start
        # The old icons block has 5 svg lines + one closing div
        search_from = start_idx + len(old_icons_start)
        end_idx = content.find(old_icons_end, search_from)
        
        if end_idx != -1:
            # Include the newline after the closing div
            old_block = content[start_idx:end_idx + len(old_icons_end)]
            content = content.replace(old_block, NEW_ICONS, 1)
            print("  ✓ Replaced payment icons with new Shopify-style colored SVGs")
        else:
            print("  ✗ Could not find end of old icons block")
            return False
    else:
        print("  ✗ Could not find old payment icons block")
        return False
    
    # Write the file back
    # Use base64 to avoid escaping issues
    import base64
    encoded = base64.b64encode(content.encode()).decode()
    write_cmd = SSH + [f"echo '{encoded}' | base64 -d > {filepath}"]
    result = run(write_cmd)
    if result is not None:
        print(f"  ✓ Successfully wrote updated {filepath}")
        return True
    return False


success = True
for f in FILES:
    if not update_file(f):
        success = False
        print(f"  ✗ FAILED to update {f}")

if success:
    print("\n✅ All 3 stores updated successfully!")
else:
    print("\n❌ Some updates failed!")
    sys.exit(1)
