#!/bin/bash
# Measures the resilience cost of the Cloudflare Tunnel dependency.
# Prerequisites:
#   1. Lumi is running (main.py)
#   2. Tunnel is running: python scripts/setup_tunnel.py run
#   3. AUTO_START_TUNNEL is disabled in constants.py (see pre-flight below)

PUBLIC_URL="https://lumi.example.com"   # <-- your hostname
LOCAL_REMOTE="http://127.0.0.1:5080"    # tunnel listener (reduced remote tier)
LOCAL_FULL="http://127.0.0.1:5000"      # full local access (never tunneled)

now() { date +%s.%N; }
el() { awk "BEGIN{printf \"%.2f\", $1 - $2}"; }

echo "== baseline =="
curl -s -o /dev/null -w "public  /healthz : %{http_code}  %{time_total}s\n" --max-time 5 "$PUBLIC_URL/healthz" || echo "public: unreachable"
curl -s -o /dev/null -w "local :5080      : %{http_code}  %{time_total}s\n" "$LOCAL_REMOTE/healthz"
curl -s -o /dev/null -w "local :5000      : %{http_code}  %{time_total}s\n" "$LOCAL_FULL/healthz"

echo "== killing cloudflared =="
pkill -f "cloudflared tunnel" || { echo "no cloudflared running — start it first"; exit 1; }
T_KILL=$(now)

echo "== polling public URL until unreachable =="
T_UNREACH=$T_KILL
for i in $(seq 1 120); do
  if ! curl -s -o /dev/null --max-time 2 "$PUBLIC_URL/healthz"; then
    T_UNREACH=$(now); break
  fi
  sleep 0.5
done
echo "public unreachable after $(el $T_UNREACH $T_KILL)s"

echo "== origin health during outage =="
curl -s -o /dev/null -w "local :5080      : %{http_code}  (remote tier still healthy)\n" "$LOCAL_REMOTE/healthz"
curl -s -o /dev/null -w "local :5000      : %{http_code}  (full local access still healthy)\n" "$LOCAL_FULL/healthz"

echo "== restarting cloudflared =="
T_RESTART=$(now)
python scripts/setup_tunnel.py run > /tmp/cloudflared-restart.log 2>&1 &
until curl -s -o /dev/null --max-time 2 "$PUBLIC_URL/healthz"; do sleep 0.5; done
T_RECOVERED=$(now)
echo "public restored after $(el $T_RECOVERED $T_RESTART)s"