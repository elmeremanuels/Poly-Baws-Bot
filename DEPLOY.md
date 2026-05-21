# Hetzner Deployment Guide — Poly-Baws-Bot

Server: Hetzner CPX11 (Ubuntu 22.04 LTS)
Architecture: two systemd services — trading bot + Streamlit dashboard — behind Caddy TLS proxy.

---

## Prerequisites

- Domain (or subdomain) pointing to your server IP via A record, e.g. `poly.yourdomain.com`
- Your Polymarket private key and proxy wallet address
- SSH access to the server

---

## 1 — Initial server setup

```bash
ssh root@<your-server-ip>

# System updates
apt update && apt upgrade -y

# Create a dedicated user — never run the bot as root
useradd -m -s /bin/bash polybot
passwd polybot       # set a strong password, only used for su
```

---

## 2 — Install system dependencies

```bash
apt install -y python3.11 python3.11-venv python3.11-dev git curl ufw
```

---

## 3 — Install Caddy

```bash
apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install caddy
```

---

## 4 — Configure firewall (UFW)

```bash
ufw allow OpenSSH
ufw allow 80/tcp    # Caddy HTTP (redirects to HTTPS)
ufw allow 443/tcp   # Caddy HTTPS

# Block direct access to internal ports from the internet
# (8001 Streamlit and 8501 Alpaca are only accessible via Caddy)
ufw enable
ufw status
```

> **Note:** Do NOT open port 8001 or 8501 in UFW or in the Hetzner Cloud Firewall.
> Both dashboards are served only through Caddy (ports 80/443).
> Currently your Alpaca dashboard (8501) may be publicly reachable — close it after Caddy is up.

---

## 5 — Clone the repository

```bash
su - polybot   # switch to the polybot user
cd ~

git clone https://github.com/elmeremanuels/poly-baws-bot.git /opt/poly-baws-bot
# or if private: git clone git@github.com:...

cd /opt/poly-baws-bot/backend
```

---

## 6 — Python virtual environment & dependencies

```bash
cd /opt/poly-baws-bot/backend
python3.11 -m venv /opt/poly-baws-bot/venv
/opt/poly-baws-bot/venv/bin/pip install --upgrade pip
/opt/poly-baws-bot/venv/bin/pip install -r requirements.txt
```

---

## 7 — Configure credentials

```bash
cp config/.env.example config/.env
nano config/.env
```

Fill in:
```
POLYMARKET_PRIVATE_KEY=0x<your-private-key>
POLYMARKET_PROXY_ADDRESS=0x<your-proxy-wallet>   # omit if using EOA directly
DASHBOARD_AUTH_PASS=<strong-password>            # used by Caddy basicauth
DASHBOARD_AUTH_USER=admin
```

Set strict permissions:
```bash
chmod 600 config/.env
```

---

## 8 — Verify connectivity (quick smoke test)

```bash
cd /opt/poly-baws-bot/backend
/opt/poly-baws-bot/venv/bin/python -c "
import asyncio, sys
sys.path.insert(0, '.')
from src.orders import check_credentials
result = asyncio.run(check_credentials())
print('Credentials OK:', result)
"
```

---

## 9 — Install systemd services

```bash
# Back to root for systemd
exit   # exit polybot, back to root

cp /opt/poly-baws-bot/poly-baws-bot.service      /etc/systemd/system/
cp /opt/poly-baws-bot/poly-baws-streamlit.service /etc/systemd/system/

systemctl daemon-reload

# Enable both services (auto-start on boot)
systemctl enable poly-baws-bot
systemctl enable poly-baws-streamlit

# Start them
systemctl start poly-baws-bot
systemctl start poly-baws-streamlit

# Check status
systemctl status poly-baws-bot
systemctl status poly-baws-streamlit
```

Expected output: both show `Active: active (running)`.

Check logs if something fails:
```bash
journalctl -u poly-baws-bot -f         # bot logs (live)
journalctl -u poly-baws-streamlit -f   # dashboard logs (live)
```

---

## 10 — Generate Caddy BasicAuth password hash

```bash
caddy hash-password
# Enter your DASHBOARD_AUTH_PASS when prompted
# Copy the output hash (looks like $2a$14$...)
```

---

## 11 — Configure Caddy

```bash
nano /etc/caddy/Caddyfile
```

Replace the entire file with:

```caddyfile
poly.yourdomain.com {
    basicauth /* {
        admin <PASTE_HASH_HERE>
    }
    reverse_proxy 127.0.0.1:8001
}

alpaca.yourdomain.com {
    basicauth /* {
        admin <PASTE_HASH_HERE>
    }
    reverse_proxy 127.0.0.1:8501
}
```

Apply:
```bash
systemctl reload caddy
```

Caddy will automatically provision Let's Encrypt certificates for both domains.
Both dashboards will be live at `https://poly.yourdomain.com` and `https://alpaca.yourdomain.com`.

---

## 12 — DNS setup

In your DNS provider (Cloudflare, Namecheap, etc.):

| Type | Name | Value |
|---|---|---|
| A | poly | `<your-server-ip>` |
| A | alpaca | `<your-server-ip>` |

Wait for propagation (usually 1–5 min with Cloudflare).

---

## 13 — Verify end-to-end

```bash
# Bot heartbeat written to DB?
sqlite3 /opt/poly-baws-bot/backend/data/trades.db \
    "SELECT value FROM dashboard_state WHERE key='heartbeat';"

# Any errors in bot process?
journalctl -u poly-baws-bot --since "5 minutes ago"

# Dashboard reachable?
curl -I https://poly.yourdomain.com   # should return 401 (auth required)
```

Open `https://poly.yourdomain.com` in a browser — login with `admin` / your password.

---

## Ongoing operations

### View live bot logs
```bash
journalctl -u poly-baws-bot -f
```

### Restart after a code update
```bash
su - polybot
cd /opt/poly-baws-bot
git pull origin claude/polymarket-trading-bot-GEypG
exit

systemctl restart poly-baws-bot
systemctl restart poly-baws-streamlit
```

### Kill switch (CLI)
```bash
su - polybot
cd /opt/poly-baws-bot/backend
/opt/poly-baws-bot/venv/bin/python -m src.main --kill
```

### Reset kill switch (CLI)
```bash
/opt/poly-baws-bot/venv/bin/python -m src.main --reset-kill
```

### Backup SQLite DB
```bash
cp /opt/poly-baws-bot/backend/data/trades.db \
   /opt/poly-baws-bot/backend/data/trades.db.bak.$(date +%Y%m%d)
```

Add to crontab for daily backups:
```bash
crontab -e -u polybot
# Add:
0 3 * * * cp /opt/poly-baws-bot/backend/data/trades.db /opt/poly-baws-bot/backend/data/backups/trades.$(date +\%Y\%m\%d).db
```

---

## Two-process architecture

```
┌─────────────────────────────────────────────────────────┐
│  Hetzner server                                         │
│                                                         │
│  poly-baws-bot.service           systemd, port: none   │
│    └─ Python asyncio loop                               │
│       ├─ scanner (Gamma API)                            │
│       ├─ ws_client (Polymarket WS)                      │
│       ├─ per-coin trading loops                         │
│       ├─ command_poll_loop ◄── reads commands table     │
│       └─ heartbeat_loop    ──► writes dashboard_state   │
│                                         │               │
│                              SQLite (trades.db)         │
│                                         │               │
│  poly-baws-streamlit.service  systemd, 127.0.0.1:8001  │
│    └─ Streamlit app                                     │
│       ├─ reads DB every 5s (sync sqlite3)               │
│       └─ writes commands table (sync sqlite3)           │
│                                                         │
│  Caddy                         ports 80, 443           │
│    └─ https://poly.yourdomain.com → 127.0.0.1:8001     │
│    └─ https://alpaca.yourdomain.com → 127.0.0.1:8501   │
└─────────────────────────────────────────────────────────┘
```

The two processes share only the SQLite file — no network sockets between them.
If Streamlit crashes, the bot keeps trading. If the bot crashes, the dashboard still shows the last DB state.

---

## Fixing the existing Alpaca dashboard (security)

Your Alpaca Streamlit app currently listens on `0.0.0.0:8501` (publicly reachable).
After Caddy is set up, lock it to localhost:

Edit your Alpaca bot's startup command (wherever it's launched) to add:
```
--server.address=127.0.0.1
```

If it runs via tmux/screen (not systemd), create a systemd service for it too:

```ini
# /etc/systemd/system/alpaca-bot.service
[Unit]
Description=Alpaca Bot — Streamlit Dashboard
After=network.target

[Service]
Type=simple
User=trader     # or whatever user it runs under
WorkingDirectory=/path/to/alpaca-bot
ExecStart=/path/to/venv/bin/streamlit run app.py \
    --server.port=8501 \
    --server.address=127.0.0.1 \
    --server.headless=true
Restart=on-failure
RestartSec=5s
MemoryMax=400M

[Install]
WantedBy=multi-user.target
```

This also fixes the reboot-persistence issue (Alpaca bot restarts automatically after reboot).
