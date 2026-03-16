# Running the Bot as a Windows Service with NSSM

NSSM (Non-Sucking Service Manager) lets you register any executable as a proper Windows Service with automatic restart and logging.

---

## Step 1 — Download and Install NSSM

1. Go to [https://nssm.cc/download](https://nssm.cc/download)
2. Download the latest release ZIP
3. Extract it — copy `nssm.exe` from the `win64/` folder to `C:\tools\nssm\nssm.exe`
4. Add `C:\tools\nssm` to your system PATH:
   - Search → "Edit the system environment variables" → Environment Variables
   - Under "System variables", find `Path` → Edit → New → `C:\tools\nssm`

Verify:
```cmd
nssm version
```

---

## Step 2 — Locate Your Python and Bot Paths

Find your Python executable:
```cmd
where python
```
Example output: `C:\Users\james\AppData\Local\Programs\Python\Python311\python.exe`

Find your bot directory — it should be something like:
`C:\Users\james\Porfolio-Watcher\polymarket-btc-bot`

---

## Step 3 — Register the Service

Open **Command Prompt as Administrator** and run:

```cmd
nssm install polymarket-bot "C:\Users\james\AppData\Local\Programs\Python\Python311\python.exe" "-m src.main"
```

Then configure it:

```cmd
nssm set polymarket-bot AppDirectory "C:\Users\james\Porfolio-Watcher\polymarket-btc-bot"
nssm set polymarket-bot AppStdout "C:\Users\james\Porfolio-Watcher\polymarket-btc-bot\logs\bot.log"
nssm set polymarket-bot AppStderr "C:\Users\james\Porfolio-Watcher\polymarket-btc-bot\logs\bot_error.log"
nssm set polymarket-bot AppStdoutCreationDisposition 4
nssm set polymarket-bot AppStderrCreationDisposition 4
nssm set polymarket-bot AppRotateFiles 1
nssm set polymarket-bot AppRotateSeconds 86400
nssm set polymarket-bot AppRotateBytes 10485760
nssm set polymarket-bot Start SERVICE_AUTO_START
nssm set polymarket-bot ObjectName LocalSystem
```

Create the logs folder first:
```cmd
mkdir "C:\Users\james\Porfolio-Watcher\polymarket-btc-bot\logs"
```

---

## Step 4 — Start the Service

```cmd
nssm start polymarket-bot
```

---

## Step 5 — Verify It Is Running

```cmd
nssm status polymarket-bot
```

Expected output: `SERVICE_RUNNING`

Also check via Services panel:
- Press `Win + R` → `services.msc`
- Find "polymarket-bot" in the list

---

## Step 6 — View Live Logs

In PowerShell:
```powershell
Get-Content -Path "C:\Users\james\Porfolio-Watcher\polymarket-btc-bot\logs\bot.log" -Wait -Tail 50
```

Or just open the log file in any text editor — it updates in real time.

---

## Step 7 — Control the Service

```cmd
nssm stop polymarket-bot
nssm start polymarket-bot
nssm restart polymarket-bot
```

To remove the service:
```cmd
nssm remove polymarket-bot confirm
```

---

## Step 8 — Prevent Windows From Sleeping (CRITICAL)

Your laptop must never sleep while the bot is running.

### Power Settings
1. Search → "Power & sleep settings"
2. Set **Screen** to "Never"
3. Set **Sleep** to "Never"
4. Click "Additional power settings" → Select "High performance" plan

### Lid Close Behavior
1. Search → "Choose what closing the lid does"
2. Set "When I close the lid:" → **Do nothing** (for both battery and plugged in)

### Command-line alternative (run as Administrator):
```cmd
powercfg /change standby-timeout-ac 0
powercfg /change standby-timeout-dc 0
powercfg /change hibernate-timeout-ac 0
powercfg /change hibernate-timeout-dc 0
powercfg /change monitor-timeout-ac 0
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Service won't start | Check `logs\bot_error.log` for Python errors |
| `.env` not found | Make sure AppDirectory is set to the bot folder |
| Port 8765 in use | Change `CONTROL_API_PORT` in `.env` |
| Service starts but crashes | Run `python -m src.main` manually first to debug |
