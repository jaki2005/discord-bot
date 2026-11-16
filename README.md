# Custom Discord bot — scheduled welcome message + self-closing tickets

## What it does
- Posts a welcome/announcement message in a public channel on a repeating schedule.
- Lets anyone open a private ticket with `/ticket`.
- Auto-closes (warns, then deletes) a ticket channel after a set number of hours with no new messages.
- `/close` lets staff close a ticket immediately.

## 1. Create the bot application (and give it its custom name)
1. Go to https://discord.com/developers/applications → **New Application**. The application name you pick here *is* the bot's display name — change it any time under **General Information → Name**, and set an avatar there too.
2. In the left sidebar go to **Bot** → **Reset Token** → copy the token. This goes in your `.env` as `BOT_TOKEN`. Keep it secret — anyone with it can control the bot.
3. On the same **Bot** page, turn on **Message Content Intent** and **Server Members Intent** (the code needs both).
4. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot permissions: `Manage Channels`, `Send Messages`, `Read Message History`, `View Channels` (add more if you want moderation features later)
   - Copy the generated URL, open it, and invite the bot to your server.

## 2. Configure
1. Copy `.env.example` to `.env`.
2. Fill in `BOT_TOKEN`.
3. Turn on Developer Mode in Discord (User Settings → Advanced), then right-click your server/channel/category to copy IDs for `GUILD_ID`, `WELCOME_CHANNEL_ID`, and optionally `TICKET_CATEGORY_ID`.
4. The welcome message defaults to the NusaKlip "Hallo!! Welcome yang baru join..." text baked into `bot.py`, with `#rules`, `#panduan-gratis-content-rewards`, and `#capcut-pro` as plain text. Fill in `RULES_CHANNEL_ID`, `GUIDE_CHANNEL_ID`, `CAPCUT_CHANNEL_ID` to turn those into real clickable channel links, or set `WELCOME_MESSAGE` to replace the whole text yourself. Adjust `WELCOME_INTERVAL_HOURS`, `TICKET_INACTIVITY_HOURS`, etc. to taste.

## 3. Run it locally (to test)
```bash
pip install -r requirements.txt
python bot.py
```
If it logs in successfully you'll see `Logged in as <YourBotName>`. Try `/ticket` in your server.

## 4. Keep it running 24/7
Running it on your own laptop only works while the laptop is on and the script is running. For "always on," pick one:

- **Railway** (railway.app) — easiest for a first deploy. Push this folder to a GitHub repo, create a new Railway project from that repo, add the same environment variables from `.env` in Railway's dashboard, and it deploys and restarts automatically. Has a small free monthly credit, then usage-based billing (a bot this light costs very little).
- **Render** (render.com) — similar workflow, "Background Worker" service type; free tier exists but can spin down when idle, so a low-cost paid worker tier is more reliable for a bot that must always be connected.
- **A cheap VPS** (DigitalOcean, Hetzner, a $4-6/mo droplet) — most control, you SSH in, install Python, and run the bot with a process manager like `pm2` or a `systemd` service so it restarts if it crashes or the server reboots.
- **Oracle Cloud Free Tier** — has an always-free small VM if you want $0/month and don't mind a bit more setup.

I'd suggest Railway first since it needs the least server administration — say the word and I'll walk you through pushing this to GitHub and deploying it there, or set it up a different way if you'd rather.

## Notes on how it works
- Ticket tracking (`tickets_data.json`) persists ticket channel IDs and last-activity timestamps to disk, so inactivity tracking survives a bot restart.
- Every non-bot message inside a tracked ticket channel resets its inactivity timer.
- A background loop checks all open tickets on an interval (`TICKET_CHECK_INTERVAL_MINUTES`), posts one warning before closing, then deletes the channel once the inactivity threshold is hit.
- The welcome message loop posts on a fixed interval from whenever the bot starts; it does not currently post at a fixed clock time (e.g. "every day at 9am") — ask if you want that instead, it's a small change.
- No AI involved anywhere in this bot — it's pure automation: the same welcome text, posted on a timer, and tickets that open/close on fixed rules.

## Buka Akses (purchase flow)
`purchase.py` replaces the ticket bot for purchases.

1. Set the `PURCHASE_*`, `STAFF_ROLE_ID`, `BLUEPRINT_ROLE_ID`, `MENTORING_ROLE_ID` variables (QRIS image ships in `assets/qris.png`) (see `.env.example`).
2. Give the bot **Manage Channels** and **Manage Roles**, and drag the NusaKlip AI role **above** the Blueprint and Mentoring roles.
3. Run `/akses cek` to see what is still missing.
4. In each product channel run `/akses panel produk:<Blueprint|Mentoring>`.

Flow: member picks 1 Bulan or 3 Bulan, fills a short form (WHOP email, WHOP username, SETUJU), gets a private room with payment steps. They upload proof and press **Udah Bayar, Panggil Staff**. Staff press **Verifikasi & Buka Akses**, the bot adds the role, posts the NusaKlip welcome message (WHOP steps, active period, links to `VIP_GUIDE_CHANNEL_ID`, `BOOKING_CHANNEL_ID`, `HELP_CHANNEL_ID`) and logs it to `PURCHASE_LOG_CHANNEL_ID`. Both Blueprint and Mentoring payment rooms close 24h after verification; the welcome message is also sent to the member's DM so it outlives the room (a warning is posted if their DMs are closed). Coaching runs in #booking-1on1, so coaches never see payment rooms. Unpaid rooms get a warning, then close after 24h idle. Every closed room is archived to `ARCHIVE_CHANNEL_ID` first. Room state lives in the title link of the bot's first message in the room; the channel topic is a readable status line. Redeploys lose nothing.

## Welcome post
Posted every `WELCOME_INTERVAL_HOURS` (default 2) in `WELCOME_CHANNEL_ID`. The bot checks every 10 minutes and only posts when its last welcome post in that channel is older than the interval, so restarts and redeploys never cause an extra post. The previous welcome post is deleted when a new one goes out (`WELCOME_DELETE_PREVIOUS`).
