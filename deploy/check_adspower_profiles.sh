#!/bin/bash
ssh administrator@101.33.123.45 'curl -s "http://localhost:50325/api/v1/user/list?page=1&page_size=20"'
EOF
