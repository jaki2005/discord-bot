"""
Custom Discord bot:
  - Scheduled welcome/announcement message posted on a loop to a public channel
  - Ticket system (private channels) that auto-closes after a period of inactivity

Setup:
  1. Copy .env.example to .env and fill in the values.
  2. pip install -r requirements.txt
  3. python bot.py

See README.md for full setup (creating the bot application, renaming it,
inviting it to your server, and hosting it 24/7).
"""

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0")) or None

# --- Welcome / scheduled announcement settings ---
WELCOME_CHANNEL_ID = int(os.getenv("WELCOME_CHANNEL_ID", "0")) or None
RULES_CHANNEL_ID = os.getenv("RULES_CHANNEL_ID", "").strip()
GUIDE_CHANNEL_ID = os.getenv("GUIDE_CHANNEL_ID", "").strip()
CAPCUT_CHANNEL_ID = os.getenv("CAPCUT_CHANNEL_ID", "").strip()


def _channel_ref(channel_id: str, fallback_name: str) -> str:
    """Real clickable channel mention if an ID was given, else plain #name text."""
    return f"<#{channel_id}>" if channel_id else f"#{fallback_name}"


WELCOME_MESSAGE = os.getenv("WELCOME_MESSAGE", "").strip() or (
    "Hallo!! Welcome yang baru join\n"
    f"Silahkan dibaca dulu untuk {_channel_ref(RULES_CHANNEL_ID, 'rules')}\n\n"
    f"Kalau udah nanti lanjut {_channel_ref(GUIDE_CHANNEL_ID, 'panduan-gratis-content-rewards')}\n"
    "Nanti tinggal di download aja ya untuk FAQ nya gratis untuk di pelajari,\n\n"
    "Dan apabila kalian tertarik kami juga menyediakan Capcut Pro premium tentunya dengan "
    "garansi 100%, kalo minat silahkan cek disini ya qaqah🥰\n"
    f"{_channel_ref(CAPCUT_CHANNEL_ID, 'capcut-pro')}\n\n"
    "Semangat menjadi Dollar🤑🤑"
)
WELCOME_INTERVAL_HOURS = float(os.getenv("WELCOME_INTERVAL_HOURS", "24"))

# --- Ticket settings ---
TICKET_CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID", "0")) or None
TICKET_INACTIVITY_HOURS = float(os.getenv("TICKET_INACTIVITY_HOURS", "48"))
TICKET_CHECK_INTERVAL_MINUTES = float(os.getenv("TICKET_CHECK_INTERVAL_MINUTES", "15"))
TICKET_WARNING_HOURS_BEFORE_CLOSE = float(os.getenv("TICKET_WARNING_HOURS_BEFORE_CLOSE", "4"))

DATA_FILE = Path(__file__).parent / "tickets_data.json"

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ---------------------------------------------------------------------------
# Persistence: which channels are tickets, and when they last had activity
# ---------------------------------------------------------------------------
def load_data() -> dict:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text())
    return {"tickets": {}}


def save_data(data: dict) -> None:
    DATA_FILE.write_text(json.dumps(data, indent=2))


ticket_data = load_data()


def touch_ticket(channel_id: int) -> None:
    """Record that a ticket channel just had activity."""
    key = str(channel_id)
    if key in ticket_data["tickets"]:
        ticket_data["tickets"][key]["last_activity"] = datetime.now(timezone.utc).isoformat()
        ticket_data["tickets"][key]["warned"] = False
        save_data(ticket_data)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    try:
        if GUILD_ID:
            guild_obj = discord.Object(id=GUILD_ID)
            # Commands are registered globally (no guild= on the decorator),
            # so they must be copied into the guild before a guild sync will
            # pick them up — otherwise sync(guild=...) silently syncs 0.
            bot.tree.copy_global_to(guild=guild_obj)
            synced = await bot.tree.sync(guild=guild_obj)
        else:
            synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"Slash command sync failed: {e}")

    if not post_welcome_message.is_running():
        post_welcome_message.start()
    if not check_inactive_tickets.is_running():
        check_inactive_tickets.start()


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    # Any message inside a tracked ticket channel resets its inactivity clock
    if str(message.channel.id) in ticket_data["tickets"]:
        touch_ticket(message.channel.id)
    await bot.process_commands(message)


# ---------------------------------------------------------------------------
# Scheduled welcome message
# ---------------------------------------------------------------------------
@tasks.loop(hours=WELCOME_INTERVAL_HOURS)
async def post_welcome_message():
    if not WELCOME_CHANNEL_ID:
        print("WELCOME_CHANNEL_ID not set, skipping scheduled welcome message.")
        return
    channel = bot.get_channel(WELCOME_CHANNEL_ID)
    if channel is None:
        print(f"Could not find channel {WELCOME_CHANNEL_ID} for welcome message.")
        return
    try:
        await channel.send(WELCOME_MESSAGE)
    except discord.Forbidden:
        print("Missing permission to send the welcome message in that channel.")


@post_welcome_message.before_loop
async def before_welcome_loop():
    await bot.wait_until_ready()


# ---------------------------------------------------------------------------
# One-off styled announcements (e.g. "Clip on Content Rewards" style posts)
# ---------------------------------------------------------------------------
NUSAKLIP_EMBED_COLOR = 0xF5A623  # matches the bot's orange "N" avatar


@bot.tree.command(name="announce", description="Post a styled announcement embed (admin use)")
@app_commands.describe(
    title="Embed title (emoji ok, e.g. 'Clip on Content Rewards 🎬')",
    body="Embed body text",
    link="Optional link to include under the body",
    channel="Channel to post in (defaults to the channel you run this in)",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def announce(
    interaction: discord.Interaction,
    title: str,
    body: str,
    link: str = None,
    channel: discord.TextChannel = None,
):
    target = channel or interaction.channel
    description = f"{body}\n\n{link}" if link else body
    embed = discord.Embed(title=title, description=description, color=NUSAKLIP_EMBED_COLOR)
    await target.send(embed=embed)
    await interaction.response.send_message(f"Posted in {target.mention}.", ephemeral=True)


@announce.error
async def announce_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "You need the Manage Server permission to use this.", ephemeral=True
        )
    else:
        await interaction.response.send_message(f"Something went wrong: {error}", ephemeral=True)


# ---------------------------------------------------------------------------
# Ticket system
# ---------------------------------------------------------------------------
@bot.tree.command(name="ticket", description="Open a private support ticket")
async def ticket(interaction: discord.Interaction):
    guild = interaction.guild
    category = guild.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
    }

    channel_name = f"ticket-{interaction.user.name}".lower().replace(" ", "-")
    channel = await guild.create_text_channel(
        channel_name,
        category=category,
        overwrites=overwrites,
        topic=f"Support ticket for {interaction.user} (id: {interaction.user.id})",
    )

    ticket_data["tickets"][str(channel.id)] = {
        "opened_by": interaction.user.id,
        "last_activity": datetime.now(timezone.utc).isoformat(),
        "warned": False,
    }
    save_data(ticket_data)

    await channel.send(
        f"🎫 Halo {interaction.user.mention}, makasih udah buka tiket! Tim bakal bantu lu di sini.\n\n"
        f"_Tiket ini otomatis ditutup kalau sudah {TICKET_INACTIVITY_HOURS:g} jam nggak ada aktivitas._"
    )
    await interaction.response.send_message(f"Ticket created: {channel.mention}", ephemeral=True)


@bot.tree.command(name="close", description="Close this support ticket now")
async def close(interaction: discord.Interaction):
    key = str(interaction.channel.id)
    if key not in ticket_data["tickets"]:
        await interaction.response.send_message("This isn't a ticket channel.", ephemeral=True)
        return
    await interaction.response.send_message("Closing this ticket...")
    await close_ticket_channel(interaction.channel, reason="closed manually")


async def close_ticket_channel(channel: discord.TextChannel, reason: str) -> None:
    key = str(channel.id)
    ticket_data["tickets"].pop(key, None)
    save_data(ticket_data)
    try:
        await channel.send(f"🔒 This ticket is now closing ({reason}). Channel will be deleted shortly.")
        await asyncio.sleep(5)
        await channel.delete(reason=reason)
    except discord.NotFound:
        pass


@tasks.loop(minutes=TICKET_CHECK_INTERVAL_MINUTES)
async def check_inactive_tickets():
    now = datetime.now(timezone.utc)
    for channel_id, info in list(ticket_data["tickets"].items()):
        last_activity = datetime.fromisoformat(info["last_activity"])
        inactive_for = now - last_activity
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            # Channel no longer exists; drop it from tracking.
            ticket_data["tickets"].pop(channel_id, None)
            save_data(ticket_data)
            continue

        hours_until_close = TICKET_INACTIVITY_HOURS - (inactive_for.total_seconds() / 3600)

        if hours_until_close <= 0:
            await close_ticket_channel(channel, reason="inactive")
        elif hours_until_close <= TICKET_WARNING_HOURS_BEFORE_CLOSE and not info.get("warned"):
            try:
                await channel.send(
                    f"⏰ This ticket has been inactive and will auto-close in about "
                    f"{max(hours_until_close, 0):.1f} hour(s) unless there's a new reply."
                )
                info["warned"] = True
                save_data(ticket_data)
            except discord.Forbidden:
                pass


@check_inactive_tickets.before_loop
async def before_ticket_loop():
    await bot.wait_until_ready()


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")
    import archive  # imported after load_dotenv() so it sees ARCHIVE_* env vars
    import purchase  # Buka Akses: panels, forms, private payment rooms

    archive.setup(bot)
    purchase.setup(bot)
    bot.run(BOT_TOKEN)
