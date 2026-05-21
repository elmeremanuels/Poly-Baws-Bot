# Deployment Guide — Poly-Baws-Bot op Hetzner + Tailscale

Server: `178.156.171.144` (Hetzner CPX11, Ubuntu 22.04)
Toegang: uitsluitend via Tailscale (geen publieke poorten voor dashboards)

---

## Architectuuroverzicht

```
Jouw laptop / telefoon
   └─ Tailscale VPN
        └─ https://<machine>.ts.net        → Polymarket dashboard (port 443)
        └─ https://<machine>.ts.net:8501   → Alpaca dashboard    (port 8501)

Server 178.156.171.144
   ├─ poly-baws-bot.service       (systemd) → trading engine
   ├─ poly-baws-streamlit.service (systemd) → Streamlit op 127.0.0.1:8001
   ├─ alpaca-bot.service          (systemd) → Streamlit op 127.0.0.1:8501
   └─ tailscale serve             → TLS-proxy, bereikbaar alleen via Tailscale
```

**Waarom Tailscale i.p.v. Caddy + publiek domein:**
- Geen domein nodig, geen DNS-beheer, geen Let's Encrypt
- TLS is gratis en automatisch via Tailscale's eigen CA (`*.ts.net`)
- Dashboard is niet bereikbaar vanaf het publieke internet, alleen vanaf jouw Tailscale-apparaten
- Tailscale versleutelt het verkeer op netwerkniveau — je hoeft niet te vertrouwen op firewall-regels alleen

---

## 0 — Voorbereiding: controleer de huidige situatie

SSH in op de server:
```bash
ssh root@178.156.171.144
```

Controleer Tailscale-status:
```bash
tailscale status
# Verwacht: jouw machine staat listed, status Connected
tailscale ip -4
# Noteert dit IP (100.x.x.x) — dit is jouw Tailscale IP
```

Controleer draaiende services:
```bash
ss -tlnp | grep -E '8501|8001|8000'
# Je ziet Streamlit op 8501 (Alpaca bot)
tmux ls && screen -ls 2>/dev/null || echo "geen tmux/screen"
# Kijk of Alpaca bot in een sessie draait i.p.v. systemd
systemctl list-units --type=service --state=running | grep -i bot
```

---

## 1 — System updates

```bash
apt update && apt upgrade -y
apt install -y python3.11 python3.11-venv python3.11-dev git curl ufw sqlite3
```

---

## 2 — Firewall instellen

```bash
ufw allow OpenSSH
ufw allow 41641/udp    # Tailscale WireGuard
# Dashboards NIET openzetten — die gaan via Tailscale
ufw enable
ufw status
```

> **Sluit port 8501 af voor het publiek.**
> Na deze stap is 8501 (Alpaca) alleen nog via Tailscale bereikbaar.
> `ufw deny 8501` is NIET nodig — poorten die niet in UFW openstaan zijn al geblokkeerd.
> Controleer dit door `ufw status` te lezen: als 8501 er niet in staat, is ie dicht.

---

## 3 — polybot gebruiker aanmaken

```bash
useradd -m -s /bin/bash polybot
# Geen wachtwoord nodig — login alleen via su of sudo
```

---

## 4 — Repo clonen en inrichten

```bash
# Als polybot
su - polybot
mkdir -p /opt/poly-baws-bot
git clone https://github.com/elmeremanuels/Poly-Baws-Bot.git /opt/poly-baws-bot

# Checkout de juiste branch
cd /opt/poly-baws-bot
git checkout claude/polymarket-trading-bot-GEypG
```

Python venv aanmaken en dependencies installeren:
```bash
python3.11 -m venv /opt/poly-baws-bot/venv
/opt/poly-baws-bot/venv/bin/pip install --upgrade pip
/opt/poly-baws-bot/venv/bin/pip install -r /opt/poly-baws-bot/backend/requirements.txt
```

> `streamlit>=1.37` wordt hier geïnstalleerd. Dit duurt 1-2 minuten.

Data-map aanmaken:
```bash
mkdir -p /opt/poly-baws-bot/backend/data/logs
mkdir -p /opt/poly-baws-bot/backend/data/backups
```

---

## 5 — Credentials configureren

```bash
cp /opt/poly-baws-bot/backend/config/.env.example \
   /opt/poly-baws-bot/backend/config/.env
nano /opt/poly-baws-bot/backend/config/.env
```

Vul in:
```env
POLYMARKET_PRIVATE_KEY=0x<jouw-private-key>
POLYMARKET_PROXY_ADDRESS=0x<jouw-proxy-wallet>
# POLYMARKET_PROXY_ADDRESS weglaten als je EOA (geen proxy) gebruikt
```

Zet stricte permissies:
```bash
chmod 600 /opt/poly-baws-bot/backend/config/.env
```

---

## 6 — Database initialiseren (smoke test)

```bash
cd /opt/poly-baws-bot/backend
/opt/poly-baws-bot/venv/bin/python -c "
import asyncio, sys
sys.path.insert(0, '.')
from src.logger import init_db
asyncio.run(init_db())
print('DB OK:', __import__('pathlib').Path('data/trades.db').exists())
"
```

Verwachte output: `DB OK: True`

Optioneel — API-credentials checken (vereist werkende .env):
```bash
/opt/poly-baws-bot/venv/bin/python -c "
import asyncio, sys
sys.path.insert(0, '.')
from src.orders import check_credentials
print('Credentials OK:', asyncio.run(check_credentials()))
"
```

---

## 7 — systemd services installeren

Ga terug naar root:
```bash
exit   # verlaat polybot-sessie
```

Kopieer de service-files:
```bash
cp /opt/poly-baws-bot/poly-baws-bot.service      /etc/systemd/system/
cp /opt/poly-baws-bot/poly-baws-streamlit.service /etc/systemd/system/
systemctl daemon-reload
```

Inschakelen en starten:
```bash
systemctl enable poly-baws-bot
systemctl enable poly-baws-streamlit
systemctl start poly-baws-bot
systemctl start poly-baws-streamlit
```

Status controleren:
```bash
systemctl status poly-baws-bot --no-pager
systemctl status poly-baws-streamlit --no-pager
```

Beide moeten `Active: active (running)` tonen.

Live logs bekijken:
```bash
journalctl -u poly-baws-bot -f            # Ctrl+C om te stoppen
journalctl -u poly-baws-streamlit -f
```

Verwachte bot-output (eerste start):
```
startup_begin
database_initialized
state_recovered open_trades=0
ws_connecting url=wss://ws-subscriptions-clob...
ws_connected
scanner_refreshed coin=BTC count=...
```

---

## 8 — Tailscale serve instellen

`tailscale serve` maakt jouw lokale services bereikbaar via HTTPS op jouw Tailscale-hostname (`<machine-name>.<tailnet>.ts.net`). TLS is automatisch.

### 8a — Tailscale HTTPS inschakelen (eenmalig)

Ga naar [https://login.tailscale.com/admin/dns](https://login.tailscale.com/admin/dns) en zet **MagicDNS** aan.
Ga naar [https://login.tailscale.com/admin/settings](https://login.tailscale.com/admin/settings) en zet **HTTPS Certificates** aan.

### 8b — Polymarket dashboard (port 443)

```bash
tailscale serve --bg https:443 / http://127.0.0.1:8001
```

### 8c — Alpaca dashboard (port 8501, nadat Alpaca is gemigreerd naar 127.0.0.1)

```bash
tailscale serve --bg https:8501 / http://127.0.0.1:8501
```

### 8d — Controleer de serve-configuratie

```bash
tailscale serve status
```

Verwachte output:
```
https://elmer-hetzner.staartvis.ts.net (of jouw machine-naam)
|-- / proxy http://127.0.0.1:8001

https://elmer-hetzner.staartvis.ts.net:8501
|-- / proxy http://127.0.0.1:8501
```

### 8e — Test vanaf jouw laptop

Installeer Tailscale op je laptop als je dat nog niet hebt, log in op hetzelfde Tailscale-account.

```
https://<machine-name>.ts.net          → Polymarket dashboard
https://<machine-name>.ts.net:8501     → Alpaca dashboard
```

De hostname kun je opzoeken via `tailscale status` op de server of via de [Tailscale admin console](https://login.tailscale.com/admin/machines).

> **Geen wachtwoord?** Dat klopt — Tailscale-toegang is de auth. Alleen apparaten op jouw Tailscale-account kunnen verbinding maken. Als je extra bescherming wilt, voeg dan een eenvoudig wachtwoordscherm toe in de Streamlit app (zie sectie 12).

---

## 9 — Alpaca bot fixen (security + persistentie)

Jouw Alpaca bot draait waarschijnlijk in een tmux/screen-sessie en luistert op `0.0.0.0:8501` (publiek bereikbaar). Dit fixen we nu.

### 9a — Controleer hoe Alpaca bot draait

```bash
tmux ls           # kijk of er een sessie is
ss -tlnp | grep 8501   # kijk welk process luistert
```

Als het in tmux draait, noteer het startcommando:
```bash
tmux attach -t <sessie-naam>
# Kijk welk commando draait, bijv:
# streamlit run /path/to/alpaca/app.py --server.port 8501
# Ctrl+B dan D om te detachen (NIET sluiten)
```

### 9b — Systemd service aanmaken voor Alpaca bot

Pas het pad aan naar jouw echte Alpaca bot directory en venv:
```bash
nano /etc/systemd/system/alpaca-bot.service
```

```ini
[Unit]
Description=Alpaca Bot — Streamlit Dashboard
After=network.target

[Service]
Type=simple
User=trader
Group=trader
WorkingDirectory=/home/trader/alpaca-bot    # PAS AAN
EnvironmentFile=/home/trader/alpaca-bot/.env    # als je een .env hebt
ExecStart=/home/trader/alpaca-bot/venv/bin/streamlit run app.py \
    --server.port=8501 \
    --server.address=127.0.0.1 \
    --server.headless=true \
    --browser.gatherUsageStats=false
Restart=on-failure
RestartSec=5s
MemoryMax=400M
StandardOutput=journal
StandardError=journal
SyslogIdentifier=alpaca-bot

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable alpaca-bot
systemctl start alpaca-bot
systemctl status alpaca-bot --no-pager
```

### 9c — Sluit de tmux-sessie nadat systemd overneemt

Controleer eerst dat de systemd service werkt (dashboard bereikbaar via Tailscale), stop dan de tmux-sessie:
```bash
tmux kill-session -t <sessie-naam>
```

---

## 10 — Verificatie einde-tot-einde

```bash
# 1. Bot schrijft heartbeat?
sqlite3 /opt/poly-baws-bot/backend/data/trades.db \
    "SELECT value FROM dashboard_state WHERE key='heartbeat';"
# Verwacht: ISO timestamp van minder dan 10 seconden geleden

# 2. Geen errors in bot?
journalctl -u poly-baws-bot --since "5 minutes ago" | grep -i error

# 3. Streamlit bereikbaar lokaal?
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8001/
# Verwacht: 200

# 4. Tailscale serve actief?
tailscale serve status

# 5. Mode correct in DB?
sqlite3 /opt/poly-baws-bot/backend/data/trades.db \
    "SELECT key, value FROM dashboard_state;"
```

Open dan `https://<machine-name>.ts.net` in jouw browser (met Tailscale actief op je laptop). Je ziet het Polymarket dashboard.

---

## 11 — Operationeel: dagelijks gebruik

### Logs bekijken
```bash
journalctl -u poly-baws-bot -f             # live bot logs
journalctl -u poly-baws-streamlit -f       # dashboard logs
journalctl -u poly-baws-bot --since today  # alles van vandaag
```

### Bot herstarten na code-update
```bash
cd /opt/poly-baws-bot
git pull origin claude/polymarket-trading-bot-GEypG
systemctl restart poly-baws-bot
systemctl restart poly-baws-streamlit
```

### Kill switch via CLI
```bash
su - polybot -c "cd /opt/poly-baws-bot/backend && \
    /opt/poly-baws-bot/venv/bin/python -m src.main --kill"
```

### Kill switch resetten via CLI
```bash
su - polybot -c "cd /opt/poly-baws-bot/backend && \
    /opt/poly-baws-bot/venv/bin/python -m src.main --reset-kill"
```

### Trades analyseren
```bash
su - polybot
cd /opt/poly-baws-bot/backend
/opt/poly-baws-bot/venv/bin/python scripts/analyze.py
```

### Exporteren naar CSV
```bash
/opt/poly-baws-bot/venv/bin/python scripts/export_csv.py \
    data/trades.db /tmp/trades_$(date +%Y%m%d).csv
```

### Dagelijkse SQLite backup (automatisch via cron)
```bash
crontab -e -u polybot
```
Voeg toe:
```cron
0 3 * * * cp /opt/poly-baws-bot/backend/data/trades.db \
    /opt/poly-baws-bot/backend/data/backups/trades.$(date +\%Y\%m\%d).db
```

---

## 12 — Paper mode draaien als eerste test

Voordat je live gaat, altijd eerst paper mode draaien:

1. Start beide services (zie stap 7)
2. Open dashboard via Tailscale
3. Controleer dat mode op `paper_hybrid` staat (de default)
4. Schakel de coins in die je wilt testen (BTC eerst)
5. Kijk of de scanner markten vindt (event log: `scanner_refreshed coin=BTC count=X`)
6. In hybrid mode: wacht op een "Trigger Entry" knop in het Hybrid Trigger Panel
7. Klik de knop — de bot doet een gesimuleerde entry op de live orderbook
8. Volg de trade in het Active Positions panel
9. Na 5-10 trades: run `analyze.py` en vergelijk met jouw handmatige resultaten

---

## 13 — Naar live gaan

Checklist voordat je live schakelt:

- [ ] Paper mode heeft minstens 20 trades gedraaid zonder crashes
- [ ] `analyze.py` toont resultaten vergelijkbaar met handmatige trades
- [ ] API credentials gecheckt (`check_credentials()` geeft True)
- [ ] Wallet heeft voldoende USDC (check via Polymarket interface)
- [ ] Daily loss limit in `config.yaml` is realistisch ingesteld (default €10)
- [ ] Kill switch getest vanuit dashboard en via CLI

Schakel dan om in het dashboard: **paper_hybrid → live_hybrid**

Begin met één coin (BTC), max 1 positie, en observeer de eerste echte trades.

---

## Referentie: alle service-commando's

| Actie | Commando |
|---|---|
| Bot status | `systemctl status poly-baws-bot` |
| Dashboard status | `systemctl status poly-baws-streamlit` |
| Bot herstarten | `systemctl restart poly-baws-bot` |
| Dashboard herstarten | `systemctl restart poly-baws-streamlit` |
| Bot stoppen | `systemctl stop poly-baws-bot` |
| Live bot logs | `journalctl -u poly-baws-bot -f` |
| Tailscale status | `tailscale status` |
| Tailscale serve status | `tailscale serve status` |
| Kill switch (dashboard) | Klik KILL in het dashboard |
| Kill switch (CLI) | `python -m src.main --kill` |
| Reset kill (CLI) | `python -m src.main --reset-kill` |
