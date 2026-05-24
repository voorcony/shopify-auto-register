#!/bin/bash
cloudflared tunnel --url http://localhost:8087 2>&1 | tee /tmp/wp-tunnel-url.txt
