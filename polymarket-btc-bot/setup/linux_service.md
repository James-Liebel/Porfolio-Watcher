# Running the Bot as a Linux systemd Service

This sets up the bot to start automatically on boot and restart if it crashes.

---

## Step 1 — Prepare the Environment

```bash
cd /home/$USER/polymarket-btc-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy and fill in your `.env`:
```bash
cp .env.example .env
nano .env
```

Create the data and logs directories:
```bash
mkdir -p data logs
```

Test the bot starts manually:
```bash
python -m src
# Ctrl+C to stop
```

---

## Step 2 — Create the systemd Unit File

Create `/etc/systemd/system/polymarket-bot.service`:

```bash
sudo nano /etc/systemd/system/polymarket-bot.service
```

Paste the following (replace `YOUR_USER` and paths with your actual Linux user and clone location):

```ini
[Unit]
Description=Polymarket structural-arbitrage bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/polymarket-btc-bot
ExecStart=/home/YOUR_USER/polymarket-btc-bot/.venv/bin/python -m src
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=polymarket-bot

# Keep environment clean
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

---

## Step 3 — Enable and Start the Service

```bash
sudo systemctl daemon-reload
sudo systemctl enable polymarket-bot
sudo systemctl start polymarket-bot
```

Check status:
```bash
sudo systemctl status polymarket-bot
```

---

## Step 4 — View Live Logs

```bash
journalctl -u polymarket-bot -f
```

Show last 100 lines:
```bash
journalctl -u polymarket-bot -n 100
```

Show logs since today:
```bash
journalctl -u polymarket-bot --since today
```

---

## Step 5 — Control the Service

```bash
sudo systemctl stop polymarket-bot
sudo systemctl start polymarket-bot
sudo systemctl restart polymarket-bot
```

---

## Step 6 — Prevent Laptop From Sleeping (CRITICAL)

### Ignore lid close events

Edit `/etc/systemd/logind.conf`:
```bash
sudo nano /etc/systemd/logind.conf
```

Find and change (or add) these lines:
```ini
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
```

Apply without reboot:
```bash
sudo systemctl restart systemd-logind
```

### Disable sleep/suspend targets

```bash
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

To reverse this later:
```bash
sudo systemctl unmask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

---

## Step 7 — Keep Screen Off (Power Saving Without Sleep)

```bash
# Turn off screen blanking for the console
sudo setterm -blank 0 -powerdown 0 -powersave off
```

For desktop environments (GNOME):
```bash
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-battery-type 'nothing'
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Service fails to start | `journalctl -u polymarket-bot -n 50` to see errors |
| `.env` not found | Verify `WorkingDirectory` path in unit file |
| Python not found | Use full path to venv python in `ExecStart` |
| Port 8765 in use | Change `CONTROL_API_PORT` in `.env`, restart service |
| Network not ready at boot | Ensure `After=network-online.target` is set |
