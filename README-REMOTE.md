# Running jarvis from other devices

`server.py` puts a network front door on the exact same jarvis brain the
REPL uses — same fast-path routing, same tools, same memory. It's already
running right now on this Mac at:

- `http://172.20.14.163:8765/` (LAN IP — works from any device on the same Wi-Fi)
- `http://Adityas-MacBook-Air.local:8765/` (same thing, by hostname)

Open that in a browser on your phone/iPad/Windows machine (same Wi-Fi) and
you'll get a chat page. It'll ask for a token the first time — that's
`JARVIS_TOKEN` from `.env`.

## 1. Make it start automatically (so you don't have to run `python server.py` by hand)

```bash
# stop the one I started manually first, so they don't fight over the port
pkill -f "python server.py"

cp com.jarvis.server.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.jarvis.server.plist
```

It'll now start on login and restart itself if it crashes. Logs go to
`server.log`. To stop it permanently:

```bash
launchctl unload ~/Library/LaunchAgents/com.jarvis.server.plist
```

One caveat: the first time it runs `osascript` (Reminders/Spotify/volume)
under launchd, macOS may pop a permission dialog (Automation / Reminders
access) — you'll need to be logged into the GUI session once to click
"Allow". After that it's remembered.

## 2. Windows + iPad, from anywhere (not just home Wi-Fi) — Tailscale

The LAN IP only works when you're on the same network. To reach the Mac
from anywhere, install **Tailscale** (free, private VPN mesh, no port
forwarding):

1. Install Tailscale on the Mac, sign in, and on it too on Windows and the
   iPad (App Store has it) — same account on all of them.
2. Each device gets a stable name like `adityas-macbook-air` reachable from
   any other device on your tailnet.
3. On Windows/iPad, open `http://adityas-macbook-air:8765/` (Tailscale's
   MagicDNS) instead of the LAN IP — works over cellular, coffee shop
   wifi, anywhere.
4. On iPad, Safari → Share → "Add to Home Screen" turns the chat page into
   an app-like icon.

No code changes needed for this step — it's purely network setup.

## 3. WhatsApp

This is the one piece I can't do for you — it needs your own Meta
developer account and a verified phone number:

1. Create a Meta developer app at https://developers.facebook.com →
   add the "WhatsApp" product. Meta gives you a free test number to start.
2. Get a permanent access token + your phone number ID from that app's
   WhatsApp → API Setup page.
3. Expose this Mac's server publicly so Meta's webhook can reach it —
   `cloudflared tunnel --url http://localhost:8765` (Cloudflare Tunnel,
   free, no account needed for a quick tunnel) gives you a public HTTPS
   URL pointed at the Mac.
4. In the Meta app, set the webhook URL to `<that public URL>/whatsapp`
   and subscribe to `messages`.
5. Tell me your WhatsApp phone number ID + access token (put them in
   `.env`, don't paste them in chat) and I'll add a `/whatsapp` route to
   `server.py` that receives the webhook, calls the same `handle_message()`
   already in there, and posts the reply back via the Graph API — plus an
   allow-list so it only ever answers your own number.

## Security notes

- `server.py` refuses to start without `JARVIS_TOKEN` in `.env` — every
  request must send it as `Authorization: Bearer <token>`. Don't share
  that token or commit `.env`.
- The `shell` tool means anyone who reaches this server with the right
  token can run commands on this Mac. Tailscale (private) is the right
  amount of exposure for the web chat; the public tunnel in step 3 is
  needed only for the WhatsApp webhook and should point at `/whatsapp`
  specifically once that route exists and enforces the sender allow-list —
  don't leave a bare public tunnel pointed at `/message`.
