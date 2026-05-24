#!/bin/bash
cloudflared tunnel --url http://localhost:8087 2>&1 | tee /home/ubuntu/wp-tunnel-latest.txt
