#!/usr/bin/env python3
"""Update sneaker store checkout function."""
import sys

path = '/var/www/sneakers-saas/index.html'
with open(path, 'r') as f:
    data = f.read()

old = '''    async checkout() {
      if (this.checkingOut || this.cart.length === 0) return;
      this.checkingOut = true;
      try {
        var names = this.cart.map(i => i.qty + 'x ' + i.name).join('; ');
        var r = await fetch('https://dashborad.onewpay.com/api/public/checkout', {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify({ api_key: 'solevora-secret-2025', product_name: names, amount: this.cartTotal })
        });
        var data = await r.json();
        if (data.checkout_url) window.location.href = data.checkout_url;
        else { this.toast = 'Error'; this.checkingOut = false; }
      } catch(e) { this.toast = 'Connection error'; this.checkingOut = false; }
    },

    init() {
      var self = this;
      fetch('/data/products.json').then(r=>r.json()).then(d=>{ self.products = d; self.buildCats(); self.handlePath(); }).catch(function(){});
      window.addEventListener('popstate', function(){ self.handlePath(); });
      try { var c = localStorage.getItem('sneakers_cart'); if (c) self.cart = JSON.parse(c); } catch(e){}
      setInterval(function(){ localStorage.setItem('sneakers_cart', JSON.stringify(self.cart)); }, 1000);
    },'''

new = '''    async checkout() {
      if (this.checkingOut || this.cart.length === 0) return;
      this.checkingOut = true;
      try {
        var items = this.cart.map(function(i) {
          return { name: i.name, price: i.price, quantity: i.qty, image: i.img || '' };
        });
        var r = await fetch('/api/onewpay-checkout', {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify({ source: 'sneaker', session_id: '', items: items, amount: this.cartTotal, refer: '' })
        });
        var data = await r.json();
        if (data.checkout_url) window.location.href = data.checkout_url;
        else { this.toast = 'Error'; this.checkingOut = false; }
      } catch(e) { this.toast = 'Connection error'; this.checkingOut = false; }
    },

    init() {
      var self = this;
      fetch('/data/products.json').then(r=>r.json()).then(d=>{ self.products = d; self.buildCats(); self.handlePath(); }).catch(function(){});
      window.addEventListener('popstate', function(){ self.handlePath(); });
      try { var c = localStorage.getItem('sneakers_cart'); if (c) self.cart = JSON.parse(c); } catch(e){}
      setInterval(function(){ localStorage.setItem('sneakers_cart', JSON.stringify(self.cart)); }, 1000);
    },'''

if old in data:
    data = data.replace(old, new, 1)
    with open(path, 'w') as f:
        f.write(data)
    print('Sneaker store updated')
else:
    print('ERROR: Could not find replacement in sneaker store')
    sys.exit(1)
