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
from anthropic import AsyncAnthropic
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0")) or None

# --- AI ticket auto-reply settings ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
AI_REPLIES_ENABLED = bool(ANTHROPIC_API_KEY)
# Role IDs (comma-separated) for staff. Once a staff member posts in a ticket,
# the AI stops auto-replying there so it never talks over a human.
STAFF_ROLE_IDS = {
    int(r) for r in os.getenv("STAFF_ROLE_IDS", "").split(",") if r.strip().isdigit()
}
MAX_HISTORY_MESSAGES = 16

KNOWLEDGE_BASE_PATH = Path(__file__).parent / "knowledge_base.md"
KNOWLEDGE_BASE = KNOWLEDGE_BASE_PATH.read_text() if KNOWLEDGE_BASE_PATH.exists() else ""

SYSTEM_PROMPT = f"""Lu adalah NusaKlip AI, asisten dukungan NusaKlip yang menjawab member di dalam\nchannel tiket privat Discord. Ikuti SEMUA aturan dan gaya bahasa di dokumen\nberikut dengan ketat. Dokumen ini adalah satu-satunya sumber kebenaran lu.\n\n{KNOWLEDGE_BASE}\n\nIngat: lu hanya menjawab di dalam channel tiket. Kalau pertanyaan di luar\ncakupan dokumen ini, atau butuh verifikasi pembayaran, refund, atau tindakan\nmanusia, bilang jujur dan arahkan ke tim (jangan mengarang jawaban)."""

client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY) if AI_REPLIES_ENABLED else None

# --- Welcome / scheduled announcement settings ---
WELCOME_CHANNEL_ID = int(os.getenv("WELCOME_CHANNEL_ID", "0")) or None
WELCOME_MESSAGE = os.getenv(
    "WELCOME_MESSAGE",
    "👋 Welcome to the community! Introduce yourself and check the pinned posts to get started.",
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


def append_history(channel_id: int, role: str, content: str) -> None:
    key = str(channel_id)
    if key not in ticket_data["tickets"]:
        return
    history = ticket_data["tickets"][key].setdefault("history", [])
    history.append({"role": role, "content": content})
    del history[:-MAX_HISTORY_MESSAGES]
    save_data(ticket_data)


def is_staff_member(member: discord.Member) -> bool:
    if not STAFF_ROLE_IDS:
        return False
    return any(role.id in STAFF_ROLE_IDS for role in getattr(member, "roles", []))


async def maybe_ai_reply(message: discord.Message) -> None:
    """If AI replies are enabled and no staff member has taken over, answer
    the member's message in their ticket using the NusaKlip knowledge base."""
    key = str(message.channel.id)
    info = ticket_data["tickets"].get(key)
    if info is None or not AI_REPLIES_ENABLED:
        return

    if isinstance(message.author, discord.Member) and is_staff_member(message.author):
        info["staff_engaged"] = True
        save_data(ticket_data)
        return

    if info.get("staff_engaged"):
        # A human took over this ticket; stay quiet.
        return

    append_history(message.channel.id, "user", message.content)

    async with message.channel.typing():
        try:
            response = await client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=600,
                system=SYSTEM_PROMPT,
                messages=info.get("history", []),
            )
            reply_text = "".join(
                block.text for block in response.content if block.type == "text"
            ).strip()
        except Exception as e:
            print(f"Anthropic API error: {e}")
            return

    if not reply_text:
        return

    append_history(message.channel.id, "assistant", reply_text)
    for chunk_start in range(0, len(reply_text), 1900):
        await message.channel.send(reply_text[chunk_start : chunk_start + 1900])


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    try:
        if GUILD_ID:
            synced = await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
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
        await maybe_ai_reply(message)
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
        "history": [],
        "staff_engaged": False,
    }
    save_data(ticket_data)

    intro = (
        f"🎫 Halo {interaction.user.mention}, makasih udah buka tiket!\n\n"
        f"Gue NusaKlip AI, bakal bantu jawab pertanyaan lu duluan. Kalau butuh "
        f"tim manusia (pembayaran, refund, dll), gue teruskan ya.\n\n"
        f"_Tiket ini otomatis ditutup kalau sudah {TICKET_INACTIVITY_HOURS:g} jam nggak ada aktivitas._"
        if AI_REPLIES_ENABLED
        else (
            f"🎫 Hi {interaction.user.mention}, thanks for opening a ticket! "
            f"A team member will be with you shortly.\n\n"
            f"_This ticket auto-closes after {TICKET_INACTIVITY_HOURS:g} hours of inactivity._"
        )
    )
    await channel.send(intro)
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
    bot.run(BOT_TOKEN)
