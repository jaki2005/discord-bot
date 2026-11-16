"""
Konsultasi Akses: simple support tickets inside NusaKlip AI.

  /konsultasi panel [channel]   posts the panel with one "Konsultasi" button.

  Click -> private ticket channel (member + staff role + bot), staff role is pinged.
  Buttons in the ticket: "Selesai, tutup ticket" (member or staff).

  Auto-close (checked every CONSULT_CHECK_INTERVAL_MINUTES):
    - after CONSULT_INACTIVITY_HOURS with no member/staff message, the bot asks
      "Masalah kamu udah beres?" with Udah / Belum buttons
    - Udah  -> ticket closed
    - Belum -> staff role is tagged, the clock restarts
    - no answer for CONSULT_FORCE_CLOSE_HOURS -> ticket closed
  Every ticket is archived (transcript zip) to ARCHIVE_CHANNEL_ID before deletion.
"""

import asyncio
import io
import os
import re
import traceback
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlparse

import discord
from discord import app_commands
from discord.ext import tasks

import archive

COLOR = 0xF5A623


def _id(name: str) -> int | None:
    return int(os.getenv(name, "0") or 0) or None


CONSULT_CATEGORY_ID = _id("CONSULT_CATEGORY_ID")
STAFF_ROLE_ID = _id("STAFF_ROLE_ID")
INACTIVITY_HOURS = float(os.getenv("CONSULT_INACTIVITY_HOURS", "24"))
FORCE_CLOSE_HOURS = float(os.getenv("CONSULT_FORCE_CLOSE_HOURS", "12"))
CHECK_MINUTES = float(os.getenv("CONSULT_CHECK_INTERVAL_MINUTES", "15"))
# Where off-topic questions get redirected (defaults to the public chat that gets the welcome post)
REDIRECT_CHANNEL_ID = os.getenv("CONSULT_REDIRECT_CHANNEL_ID", "").strip() or os.getenv("WELCOME_CHANNEL_ID", "").strip()
IRRELEVANT_CLOSE_SECONDS = 300
IRRELEVANT_MARKER = "-# Ticket ini ditutup otomatis dalam"

TOPIC_PREFIX = "💬 Konsultasi"
REMINDER_MARKER = "⏳ Cek dulu:"
STATE_BASE = "https://nusaklip.online/"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def slug(text: str, limit: int = 20) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:limit].strip("-")) or "member"


def is_staff(member) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.manage_guild:
        return True
    return bool(STAFF_ROLE_ID and any(r.id == STAFF_ROLE_ID for r in member.roles))


def staff_mention(guild: discord.Guild) -> str:
    role = guild.get_role(STAFF_ROLE_ID) if STAFF_ROLE_ID else None
    return role.mention if role else "tim NusaKlip"


def ticket_url(user_id: int) -> str:
    return STATE_BASE + "?" + urlencode({"nk": "1", "k": "konsul", "u": str(user_id)})


def ticket_owner_from_url(url: str | None) -> int | None:
    if not url or "k=konsul" not in url:
        return None
    q = parse_qs(urlparse(url).query)
    try:
        return int(q["u"][0])
    except (KeyError, ValueError, IndexError):
        return None


def ticket_channels(guild: discord.Guild) -> list[discord.TextChannel]:
    return [ch for ch in guild.text_channels if ch.topic and ch.topic.startswith(TOPIC_PREFIX)]


async def ticket_owner(ch: discord.TextChannel) -> int | None:
    async for msg in ch.history(limit=10, oldest_first=True):
        if msg.author.id == ch.guild.me.id and msg.embeds:
            uid = ticket_owner_from_url(msg.embeds[0].url)
            if uid:
                return uid
    return None


async def close_ticket(ch: discord.TextChannel, reason: str) -> bool:
    """Archive transcript, then delete. Keeps the ticket if archiving fails."""
    target = ch.guild.get_channel(archive.ARCHIVE_CHANNEL_ID) if archive.ARCHIVE_CHANNEL_ID else None
    if not isinstance(target, discord.TextChannel):
        return False
    try:
        owner = await ticket_owner(ch)
        data, info = await archive.build_archive(ch, ch.guild.filesize_limit)
        who = f"<@{owner}>" if owner else "-"
        await target.send(
            f"💬 **#{ch.name}** | {info['count']} pesan | member {who} | {reason}",
            file=discord.File(io.BytesIO(data), filename=f"{ch.name}.zip"),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await ch.delete(reason=reason)
        return True
    except Exception as e:
        print(f"[konsultasi] close failed for #{ch.name}: {type(e).__name__}: {e}", flush=True)
        return False


def redirect_ref() -> str:
    return f"<#{REDIRECT_CHANNEL_ID}>" if REDIRECT_CHANNEL_ID else "#diskusi-clipper"


async def close_as_irrelevant(ch: discord.TextChannel, staff) -> None:
    owner = await ticket_owner(ch)
    who = f"<@{owner}>" if owner else "Halo"
    await ch.send(
        f"Halo {who}, makasih udah nanya! 🙏\n\n"
        "Ticket **Konsultasi Akses** khusus buat pertanyaan soal **Full Clipping Blueprint**, "
        "**Mentoring Eksklusif**, dan **kendala akses atau pembayaran**.\n\n"
        f"Buat pertanyaan seputar clipping atau campaign, tanya di {redirect_ref()} ya. "
        "Banyak member dan coach yang bisa bantu di sana.\n\n"
        f"{IRRELEVANT_MARKER} {IRRELEVANT_CLOSE_SECONDS // 60:g} menit.",
        allowed_mentions=discord.AllowedMentions(users=True),
    )
    await asyncio.sleep(IRRELEVANT_CLOSE_SECONDS)
    await close_ticket(ch, f"nggak relevan (ditandai {staff})")


class LoggedView(discord.ui.View):
    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        print(f"[konsultasi] error on {getattr(item, 'custom_id', item)}: {type(error).__name__}: {error}", flush=True)
        traceback.print_exception(type(error), error, error.__traceback__)
        try:
            msg = "Ada error di bot. Coba lagi sebentar, atau tag tim NusaKlip ya."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------
def panel_embed() -> discord.Embed:
    return discord.Embed(
        title="💬 Konsultasi Akses",
        description=(
            "Mau tanya-tanya dulu soal **Full Clipping Blueprint** atau **Mentoring Eksklusif** "
            "sebelum gabung? Atau lagi ada **kendala teknis**, misalnya akses WHOP belum kebuka, "
            "role belum masuk, atau pembayaran belum diverifikasi?\n\n"
            "Klik tombol **Konsultasi** di bawah. Bot bakal bukain ticket pribadi buat kamu, "
            "dan tim admin NusaKlip bakal bales secepatnya.\n\n"
            f"-# Pertanyaan seputar clipping atau campaign (clip ditolak, payout, dll) tanya di "
            f"{redirect_ref()} ya, banyak member dan coach yang bisa bantu di sana."
        ),
        color=COLOR,
    )


class PanelView(LoggedView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Konsultasi", emoji="💬", style=discord.ButtonStyle.primary,
                       custom_id="nk:kons:open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild, member = interaction.guild, interaction.user
        for ch in ticket_channels(guild):
            if any(getattr(t, "id", None) == member.id for t in ch.overwrites):
                await interaction.response.send_message(
                    f"Kamu masih punya ticket konsultasi yang kebuka: {ch.mention}", ephemeral=True
                )
                return

        category = guild.get_channel(CONSULT_CATEGORY_ID) if CONSULT_CATEGORY_ID else None
        if not isinstance(category, discord.CategoryChannel):
            category = interaction.channel.category  # fallback: same category as the panel

        await interaction.response.defer(ephemeral=True, thinking=True)
        staff_role = guild.get_role(STAFF_ROLE_ID) if STAFF_ROLE_ID else None
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, attach_files=True,
                read_message_history=True, embed_links=True,
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, attach_files=True, embed_links=True,
                read_message_history=True, manage_channels=True, manage_messages=True,
            ),
        }
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, attach_files=True, read_message_history=True,
            )
        try:
            ch = await guild.create_text_channel(
                name=f"konsultasi-{slug(member.name)}",
                category=category,
                overwrites=overwrites,
                topic=f"{TOPIC_PREFIX} • {member.display_name}",
                reason=f"Ticket konsultasi oleh {member}",
            )
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"Gagal bikin ticket ({type(e).__name__}). Tag tim NusaKlip ya.", ephemeral=True
            )
            return

        e = discord.Embed(
            title="💬 Konsultasi Akses",
            url=ticket_url(member.id),
            description=(
                f"Halo {member.mention}! Ceritain pertanyaan atau kendala kamu di sini ya, "
                "makin detail makin cepat dibantu.\n\n"
                "• Mau tanya soal **Blueprint** atau **Mentoring**? Tulis aja yang pengen kamu tahu.\n"
                "• Ada **kendala teknis**? Jelasin masalahnya dan sertakan screenshot.\n"
                "• Soal **pembayaran**? Sertakan screenshot bukti bayarnya.\n\n"
                "Kalau udah beres, klik **Selesai, tutup ticket**."
            ),
            color=COLOR,
        )
        e.set_footer(text=f"Ticket otomatis dicek kalau {INACTIVITY_HOURS:g} jam nggak ada aktivitas")
        await ch.send(
            content=f"{member.mention} {staff_mention(guild)}",
            embed=e,
            view=TicketView(),
            allowed_mentions=discord.AllowedMentions(users=True, roles=True),
        )
        await interaction.followup.send(f"✅ Ticket kamu udah dibuka: {ch.mention}", ephemeral=True)


# ---------------------------------------------------------------------------
# Inside the ticket
# ---------------------------------------------------------------------------
async def _can_close(interaction: discord.Interaction) -> bool:
    if is_staff(interaction.user):
        return True
    owner = await ticket_owner(interaction.channel)
    return owner == interaction.user.id


class TicketView(LoggedView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Selesai, tutup ticket", emoji="✅", style=discord.ButtonStyle.secondary,
                       custom_id="nk:kons:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await _can_close(interaction):
            await interaction.response.send_message("Kamu nggak bisa nutup ticket ini.", ephemeral=True)
            return
        await interaction.response.send_message("Oke, ticket ditutup. Transkripnya disimpan. 🙏")
        if not await close_ticket(interaction.channel, f"ditutup oleh {interaction.user}"):
            await interaction.followup.send("Transkrip gagal disimpan, jadi ticket nggak dihapus.", ephemeral=True)


    @discord.ui.button(label="Nggak Relevan", emoji="🚫", style=discord.ButtonStyle.danger,
                       custom_id="nk:kons:irrelevant")
    async def irrelevant(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            await interaction.response.send_message("Tombol ini cuma buat staff.", ephemeral=True)
            return
        await interaction.response.send_message("Oke, ditandai nggak relevan.", ephemeral=True)
        await close_as_irrelevant(interaction.channel, interaction.user)


class ReminderView(LoggedView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Udah", emoji="✅", style=discord.ButtonStyle.success, custom_id="nk:kons:yes")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await _can_close(interaction):
            await interaction.response.send_message("Tombol ini buat pemilik ticket.", ephemeral=True)
            return
        await interaction.response.edit_message(content="✅ Dijawab: udah beres.", view=None)
        await interaction.channel.send("Sip, ticket ditutup. Makasih udah konsultasi! 🙏")
        await close_ticket(interaction.channel, f"udah beres (dijawab {interaction.user})")

    @discord.ui.button(label="Belum", emoji="🙋", style=discord.ButtonStyle.danger, custom_id="nk:kons:no")
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await _can_close(interaction):
            await interaction.response.send_message("Tombol ini buat pemilik ticket.", ephemeral=True)
            return
        # Editing the reminder removes its marker, and the 🔔 message below counts as fresh
        # activity, so the inactivity clock restarts from here.
        await interaction.response.edit_message(content="🙋 Dijawab: belum beres.", view=None)
        await interaction.channel.send(
            f"🔔 {staff_mention(interaction.guild)}, {interaction.user.mention} masih butuh bantuan di ticket ini.",
            allowed_mentions=discord.AllowedMentions(roles=True, users=False),
        )


# ---------------------------------------------------------------------------
# Auto-close loop
# ---------------------------------------------------------------------------
async def _activity(ch: discord.TextChannel):
    """(last non-bot message time, time of a reminder posted after it or None)."""
    reminder_at = None
    async for msg in ch.history(limit=50):
        if msg.author.bot:
            if msg.content.startswith("🔔"):  # "Belum" answer: counts as activity
                return msg.created_at, reminder_at
            if reminder_at is None and msg.content.startswith(REMINDER_MARKER):
                reminder_at = msg.created_at
            continue
        return msg.created_at, reminder_at
    return ch.created_at, reminder_at


def make_loop(bot):
    @tasks.loop(minutes=CHECK_MINUTES)
    async def consult_autoclose():
        now = datetime.now(timezone.utc)
        for guild in bot.guilds:
            for ch in ticket_channels(guild):
                try:
                    # Fallback for "Nggak Relevan" if the bot restarted during the wait.
                    flagged_at = None
                    async for m in ch.history(limit=10):
                        if m.author.bot and IRRELEVANT_MARKER in m.content:
                            flagged_at = m.created_at
                            break
                    if flagged_at:
                        if now - flagged_at >= timedelta(seconds=IRRELEVANT_CLOSE_SECONDS):
                            await close_ticket(ch, "nggak relevan")
                        continue
                    last, reminder_at = await _activity(ch)
                    if reminder_at:
                        if now - reminder_at >= timedelta(hours=FORCE_CLOSE_HOURS):
                            await close_ticket(ch, f"otomatis, nggak ada jawaban {FORCE_CLOSE_HOURS:g} jam")
                    elif now - last >= timedelta(hours=INACTIVITY_HOURS):
                        owner = await ticket_owner(ch)
                        who = f"<@{owner}>" if owner else "Halo"
                        await ch.send(
                            f"{REMINDER_MARKER} {who}, masalah kamu udah beres? Kalau nggak ada jawaban "
                            f"dalam {FORCE_CLOSE_HOURS:g} jam, ticket ini ditutup otomatis.",
                            view=ReminderView(),
                            allowed_mentions=discord.AllowedMentions(users=True),
                        )
                except Exception as e:
                    print(f"[konsultasi] autoclose error on #{ch.name}: {type(e).__name__}: {e}", flush=True)

    @consult_autoclose.before_loop
    async def _wait():
        await bot.wait_until_ready()

    return consult_autoclose


# ---------------------------------------------------------------------------
# Slash command + wiring
# ---------------------------------------------------------------------------
konsultasi = app_commands.Group(
    name="konsultasi",
    description="Panel ticket Konsultasi Akses",
    guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)


@konsultasi.command(name="panel", description="Kirim panel Konsultasi Akses ke sebuah channel")
@app_commands.describe(channel="Kosongin buat channel ini")
async def konsultasi_panel(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    target = channel or interaction.channel
    try:
        await target.send(embed=panel_embed(), view=PanelView())
    except discord.Forbidden:
        await interaction.response.send_message(f"Bot nggak bisa kirim pesan di {target.mention}.", ephemeral=True)
        return
    await interaction.response.send_message(f"Panel Konsultasi Akses udah dipasang di {target.mention}.", ephemeral=True)


@konsultasi.command(name="tidak-relevan", description="Bales sopan + arahkan ke diskusi, lalu tutup ticket ini")
async def konsultasi_irrelevant(interaction: discord.Interaction):
    ch = interaction.channel
    if not (isinstance(ch, discord.TextChannel) and ch.topic and ch.topic.startswith(TOPIC_PREFIX)):
        await interaction.response.send_message("Command ini cuma bisa dipakai di dalam ticket konsultasi.",
                                                ephemeral=True)
        return
    await interaction.response.send_message("Oke, ditandai nggak relevan.", ephemeral=True)
    await close_as_irrelevant(ch, interaction.user)


def setup(bot) -> None:
    bot.tree.add_command(konsultasi)

    # Persistent views must be built inside the running event loop (see purchase.py).
    original_hook = bot.setup_hook

    async def setup_hook():
        await original_hook()
        bot.add_view(PanelView())
        bot.add_view(TicketView())
        bot.add_view(ReminderView())

    bot.setup_hook = setup_hook
    loop = make_loop(bot)

    async def _start_loop():
        if not loop.is_running():
            loop.start()

    bot.add_listener(_start_loop, "on_ready")
