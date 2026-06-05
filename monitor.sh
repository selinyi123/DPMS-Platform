
#!/bin/bash

echo "=== $(date) ==="

echo "--- Memory ---"

docker stats --no-stream --format "table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}"

echo "--- Redis Pending ---"

docker compose exec -T redis redis-cli XPENDING lottery_tasks workers

echo "--- Chromium Count ---"

ps -ef | grep -c "[c]hromium"

echo "--- Container Status ---"

docker compose ps
