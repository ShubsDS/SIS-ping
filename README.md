# SIS-ping

Tiny async watcher that polls the UVA SIS class-details endpoint and emails
you the moment a seat opens in a specific class.

- **What it watches:** `enrollment_available` in `IScript_ClassDetails`
- **How:** one `aiohttp` session with auto-refreshing SIS session cookies,
  jittered polling, exponential backoff on failures.
- **Delivery:** Gmail SMTP (App Password, STARTTLS).
- **Deployment:** systemd user service with `Restart=always`.

---

## 0. What you need

- Python **3.11+** on the server
- A Google account with **2-Step Verification** on, so you can create an
  **App Password**: <https://myaccount.google.com/apppasswords>
- The **5-digit `class_nbr`** and **term code** of the section you want.
  For Fall 2026 the term is `1268`. Find the `class_nbr` on
  <https://louslist.org> or in SIS (it's labeled "Class Nbr").

---

## 1. Local setup (test it on your laptop first)

```bash
cd SIS-ping
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
$EDITOR .env          # fill in TERM, CLASS_NBR, SMTP_*, EMAIL_TO
```

Then do a one-shot check to confirm the config is good:

```bash
python watcher.py --check-only
```

Expected output looks like:

```
CS 1110-001 (16348)  'Introduction to Programming'
  status               = Open
  enrollment_available = 161
  enrollment_total     = 39 / 200
  wait_list            = 0 / 199
```

If that works, run the live watcher:

```bash
python watcher.py
```

You should get a startup email within ~10 seconds ("SIS-ping is now
watching..."). Leave it running — it will email you again when
`enrollment_available` goes above 0.

---

## 2. Deploy to a remote server (systemd, survives logout + reboots)

On any Linux box (Ubuntu/Debian shown) with your SSH access:

```bash
# 1. Copy the project up
scp -r SIS-ping user@server:~/

# 2. On the server:
ssh user@server
cd ~/SIS-ping
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env
nano .env            # fill in values; save

# 3. Confirm it can reach SIS and send mail
./venv/bin/python watcher.py --check-only

# 4. Install the user systemd unit
mkdir -p ~/.config/systemd/user
cp systemd/sis-ping.service ~/.config/systemd/user/

# 5. Allow your user services to keep running after logout
sudo loginctl enable-linger "$USER"

# 6. Start it
systemctl --user daemon-reload
systemctl --user enable --now sis-ping

# 7. Verify
systemctl --user status sis-ping
journalctl --user -u sis-ping -f        # live tail; Ctrl-C to detach
```

You can now `exit` the SSH session and the watcher keeps running. It
will auto-restart on crashes and on server reboot.

### Stopping / updating

```bash
systemctl --user restart sis-ping        # after editing .env
systemctl --user stop sis-ping
systemctl --user disable sis-ping
```

---

## 3. Alternative: `tmux` / `nohup` (no systemd needed)

If you don't have systemd (e.g. a shared host) or just want the simplest
thing possible:

```bash
# tmux — reattachable
tmux new -s sis-ping
./venv/bin/python watcher.py
# press Ctrl-b then d to detach; it keeps running.
# reattach later with: tmux attach -t sis-ping

# nohup — fire and forget
nohup ./venv/bin/python watcher.py > watcher.log 2>&1 &
disown
```

Neither auto-restarts on crashes or reboots, so use systemd for anything
you want up for months.

---

## 4. Configuration reference

All settings live in `.env` (see `.env.example` for the template).

| Key | Required | Default | Notes |
|---|---|---|---|
| `TERM` | yes | — | `1268` for Fall 2026 |
| `CLASS_NBR` | yes | — | 5-digit class number |
| `MNEMONIC` | no | — | Friendly label for email subject |
| `POLL_INTERVAL` | no | `60` | Seconds between checks |
| `SMTP_HOST` | no | `smtp.gmail.com` | |
| `SMTP_PORT` | no | `587` | STARTTLS |
| `SMTP_USER` | yes | — | Your Gmail address |
| `SMTP_APP_PASSWORD` | yes | — | Google App Password |
| `EMAIL_TO` | yes | — | Where alerts are sent |
| `EMAIL_FROM` | no | = `SMTP_USER` | For Gmail, keep as default |
| `LOG_FILE` | no | `watcher.log` | Rotating, 2 MB × 3 files |
| `SEND_STARTUP_EMAIL` | no | `true` | Confirms the watcher is alive |

---

## 5. How it handles real-world flakiness

- **Session cookies:** SIS returns a CSA5 "Access Interrupted" page for
  requests without a session. The client hits `psp/ihprd/?cmd=login`
  once to get cookies, and transparently re-hits it if a later request
  is interrupted.
- **Rate-limit friendly:** 60s default interval with ±5s jitter and an
  honest User-Agent.
- **Network errors:** exponential backoff up to 10 minutes.
- **Alert dedup:** the "open" alert fires once. If the seat fills again
  the watcher re-arms, so a second opening still pages you.
- **SIGTERM:** clean shutdown (systemd's `stop` exits the loop promptly,
  no orphaned connections).

## 6. Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Missing required config: ...` on startup | `.env` key missing or empty. |
| `SIS returned HTTP 403` repeatedly | UVA's WAF blocked the User-Agent/IP. Try from a different server. |
| `(535, '5.7.8 Username and Password not accepted')` | Using your Google login password instead of an **App Password**. |
| No startup email but logs look fine | Gmail rate-limited; check Gmail > Sent, and set `EMAIL_FROM=SMTP_USER`. |
| `CLASS_NBR` always reports "open" immediately | You set the wrong class number — it's not the one you think. Double-check on Hoos' List. |
