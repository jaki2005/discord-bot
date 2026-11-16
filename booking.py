"""
Booking 1-on-1: coaching session tickets for Mentoring members (replaces Ticket Tool).

  /booking panel [channel]   posts the panel in #booking-1on1 with one "BOOKING 1-ON-1" button.

  Click -> private ticket in BOOKING_CATEGORY_ID (member + coach role + staff role + bot).
  The member writes when they are free and presses "Panggil Coach" (bot tags the coach role,
  30 min cooldown); the coach answers with a slot.
  Only members holding the Mentoring role (or staff) can open one. Payment rooms are never
  visible to coaches; booking tickets are the only place coaches are added.

  Auto-close: after BOOKING_INACTIVITY_HOURS without a member/coach message the bot asks
  "sesi kamu udah beres?" (Udah closes, Belum tags the coach role); no answer for
  BOOKING_FORCE_CLOSE_HOURS closes it. Transcripts go to ARCHIVE_CHANNEL_ID.
"""

import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import discord
from discord import app_commands
from discord.ext import tasks

import consult as C

BOOKING_CATEGORY_ID = C._id("BOOKING_CATEGORY_ID")
COACH_ROLE_ID = C._id("COACH_ROLE_ID")
MENTORING_ROLE_ID = C._id("MENTORING_ROLE_ID")
INACTIVITY_HOURS = float(os.getenv("BOOKING_INACTIVITY_HOURS", "48"))
FORCE_CLOSE_HOURS = float(os.getenv("BOOKING_FORCE_CLOSE_HOURS", "24"))
CHECK_MINUTES = float(os.getenv("BOOKING_CHECK_INTERVAL_MINUTES", "30"))

TOPIC_PREFIX = "🎫 Booking 1-on-1"
PING_COOLDOWN_SECONDS = 30 * 60
_last_ping: dict[int, float] = {}


def coach_role(guild):
    return guild.get_role(COACH_ROLE_ID) if COACH_ROLE_ID else None


def coach_mention(guild) -> str:
    role = coach_role(guild)
    return role.mention if role else "coach"


def is_coach_or_staff(member) -> bool:
    if C.is_staff(member):
        return True
    return bool(COACH_ROLE_ID and isinstance(member, discord.Member)
                and any(r.id == COACH_ROLE_ID for r in member.roles))


def booking_url(user_id: int) -> str:
    return C.STATE_BASE + "?" + urlencode({"nk": "1", "k": "booking", "u": str(user_id)})


def booking_channels(guild) -> list[discord.TextChannel]:
    return [ch for ch in guild.text_channels if ch.topic and ch.topic.startswith(TOPIC_PREFIX)]


async def _can_close(interaction) -> bool:
    if is_coach_or_staff(interaction.user):
        return True
    return await C.ticket_owner(interaction.channel) == interaction.user.id


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------
def panel_embed(guild=None) -> discord.Embed:
    return discord.Embed(
        title="🎫 BOOKING 1-ON-1 NUSAKLIP",
        description=(
            "Mau mendapatkan sesi 1-on-1 bersama tim NusaKlip?\n"
            "Klik tombol di bawah untuk membuat ticket dan masuk ke antrean sesi 1-on-1.\n\n"
            "**Setelah ticket kebuat, jangan lupa:**\n"
            "1. Tulis kamu bisanya hari apa dan jam berapa\n"
            "2. Klik tombol **Panggil Coach**, nanti NusaKlip AI yang ngetag coach\n"
            "Coach bakal kasih tahu waktu available-nya di ticket kamu.\n\n"
            "Mohon bersabar selama menunggu giliran. Kami akan menghubungi kamu melalui "
            "ticket ini ketika sesi kamu sudah siap."
        ),
        color=0x2ECC71,
    )


class PanelView(C.LoggedView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="BOOKING 1-ON-1", emoji="🎫", style=discord.ButtonStyle.primary,
                       custom_id="nk:book:open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild, member = interaction.guild, interaction.user
        role_ids = {r.id for r in getattr(member, "roles", [])}
        if not (C.is_staff(member) or (MENTORING_ROLE_ID and MENTORING_ROLE_ID in role_ids)):
            await interaction.response.send_message(
                "Booking 1-on-1 khusus buat member **Mentoring Eksklusif** ya.", ephemeral=True)
            return
        for ch in booking_channels(guild):
            if any(getattr(t, "id", None) == member.id for t in ch.overwrites):
                await interaction.response.send_message(
                    f"Kamu masih punya ticket booking yang kebuka: {ch.mention}", ephemeral=True)
                return

        category = guild.get_channel(BOOKING_CATEGORY_ID) if BOOKING_CATEGORY_ID else None
        if not isinstance(category, discord.CategoryChannel):
            category = interaction.channel.category

        await interaction.response.defer(ephemeral=True, thinking=True)
        rw = dict(view_channel=True, send_messages=True, attach_files=True, read_message_history=True)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(embed_links=True, **rw),
            guild.me: discord.PermissionOverwrite(embed_links=True, manage_channels=True,
                                                  manage_messages=True, **rw),
        }
        for role in (coach_role(guild), guild.get_role(C.STAFF_ROLE_ID) if C.STAFF_ROLE_ID else None):
            if role:
                overwrites[role] = discord.PermissionOverwrite(**rw)
        try:
            ch = await guild.create_text_channel(
                name=f"booking-{C.slug(member.name)}",
                category=category,
                overwrites=overwrites,
                topic=f"{TOPIC_PREFIX} • {member.display_name}",
                reason=f"Booking 1-on-1 oleh {member}",
            )
        except discord.HTTPException as e:
            await interaction.followup.send(f"Gagal bikin ticket ({type(e).__name__}). Tag tim NusaKlip ya.",
                                            ephemeral=True)
            return

        e = discord.Embed(
            title="🎫 Booking 1-on-1",
            url=booking_url(member.id),
            description=(
                f"Halo {member.mention}! Ticket booking 1-on-1 kamu udah kebuka.\n\n"
                "**Biar cepat dijadwalin:**\n"
                "1. Tulis kamu bisanya hari apa dan jam berapa "
                "(contoh: Rabu atau Kamis, jam 19.00 sampai 21.00 WIB)\n"
                "2. Kalau ada yang mau dibahas (clip, akun, strategi), tulis juga ya\n"
                "3. Klik **Panggil Coach**, nanti NusaKlip AI yang ngetag coach\n\n"
                "Nanti coach bakal kasih tahu waktu available-nya di sini. "
                "Kalau sesinya udah selesai, klik **Selesai, tutup ticket**."
            ),
            color=0x2ECC71,
        )
        e.set_footer(text=f"Ticket otomatis dicek kalau {INACTIVITY_HOURS:g} jam nggak ada aktivitas")
        await ch.send(content=member.mention, embed=e, view=TicketView(),
                      allowed_mentions=discord.AllowedMentions(users=True))
        await interaction.followup.send(f"✅ Ticket booking kamu udah dibuka: {ch.mention}", ephemeral=True)


# ---------------------------------------------------------------------------
# Inside the ticket
# ---------------------------------------------------------------------------
class TicketView(C.LoggedView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Panggil Coach", emoji="🔔", style=discord.ButtonStyle.primary,
                       custom_id="nk:book:ping")
    async def ping(self, interaction: discord.Interaction, button: discord.ui.Button):
        owner = await C.ticket_owner(interaction.channel)
        if interaction.user.id != owner and not is_coach_or_staff(interaction.user):
            await interaction.response.send_message("Tombol ini buat pemilik ticket.", ephemeral=True)
            return
        wrote = False
        async for m in interaction.channel.history(limit=50):
            if m.author.id == owner and (m.content.strip() or m.attachments):
                wrote = True
                break
        if not wrote:
            await interaction.response.send_message(
                "Tulis dulu kamu bisanya hari apa dan jam berapa di ticket ini, baru panggil coach ya.",
                ephemeral=True)
            return
        now = time.time()
        if now - _last_ping.get(interaction.channel.id, 0) < PING_COOLDOWN_SECONDS:
            await interaction.response.send_message(
                "Coach udah dipanggil barusan, tunggu balasannya sebentar ya.", ephemeral=True)
            return
        _last_ping[interaction.channel.id] = now
        await interaction.response.send_message(
            f"🔔 {coach_mention(interaction.guild)}, <@{owner}> mau jadwalin sesi 1-on-1. "
            "Jadwal yang dia bisa ada di atas, tolong kabarin kapan kamu available ya.",
            allowed_mentions=discord.AllowedMentions(roles=True, users=False),
        )

    @discord.ui.button(label="Selesai, tutup ticket", emoji="✅", style=discord.ButtonStyle.secondary,
                       custom_id="nk:book:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await _can_close(interaction):
            await interaction.response.send_message("Kamu nggak bisa nutup ticket ini.", ephemeral=True)
            return
        await interaction.response.send_message("Oke, ticket ditutup. Semoga sesinya bermanfaat! 🙏")
        if not await C.close_ticket(interaction.channel, f"ditutup oleh {interaction.user}"):
            await interaction.followup.send("Transkrip gagal disimpan, jadi ticket nggak dihapus.", ephemeral=True)


class ReminderView(C.LoggedView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Udah", emoji="✅", style=discord.ButtonStyle.success, custom_id="nk:book:yes")
    async def yes(self, interaction, button):
        if not await _can_close(interaction):
            await interaction.response.send_message("Tombol ini buat pemilik ticket.", ephemeral=True)
            return
        await interaction.response.edit_message(content="✅ Dijawab: sesi udah beres.", view=None)
        await interaction.channel.send("Sip, ticket ditutup. Makasih udah booking 1-on-1! 🙏")
        await C.close_ticket(interaction.channel, f"sesi beres (dijawab {interaction.user})")

    @discord.ui.button(label="Belum", emoji="🙋", style=discord.ButtonStyle.danger, custom_id="nk:book:no")
    async def no(self, interaction, button):
        if not await _can_close(interaction):
            await interaction.response.send_message("Tombol ini buat pemilik ticket.", ephemeral=True)
            return
        await interaction.response.edit_message(content="🙋 Dijawab: belum beres.", view=None)
        await interaction.channel.send(  # 🔔 counts as fresh activity (see consult._activity)
            f"🔔 {coach_mention(interaction.guild)}, {interaction.user.mention} masih nunggu jadwal sesi 1-on-1.",
            allowed_mentions=discord.AllowedMentions(roles=True, users=False),
        )


# ---------------------------------------------------------------------------
# Auto-close loop
# ---------------------------------------------------------------------------
def make_loop(bot):
    @tasks.loop(minutes=CHECK_MINUTES)
    async def booking_autoclose():
        now = datetime.now(timezone.utc)
        for guild in bot.guilds:
            for ch in booking_channels(guild):
                try:
                    last, reminder_at = await C._activity(ch)
                    if reminder_at:
                        if now - reminder_at >= timedelta(hours=FORCE_CLOSE_HOURS):
                            await C.close_ticket(ch, f"otomatis, nggak ada jawaban {FORCE_CLOSE_HOURS:g} jam")
                    elif now - last >= timedelta(hours=INACTIVITY_HOURS):
                        owner = await C.ticket_owner(ch)
                        who = f"<@{owner}>" if owner else "Halo"
                        await ch.send(
                            f"{C.REMINDER_MARKER} {who}, sesi 1-on-1 kamu udah beres? Kalau nggak ada jawaban "
                            f"dalam {FORCE_CLOSE_HOURS:g} jam, ticket ini ditutup otomatis.",
                            view=ReminderView(),
                            allowed_mentions=discord.AllowedMentions(users=True),
                        )
                except Exception as e:
                    print(f"[booking] autoclose error on #{ch.name}: {type(e).__name__}: {e}", flush=True)

    @booking_autoclose.before_loop
    async def _wait():
        await bot.wait_until_ready()

    return booking_autoclose


# ---------------------------------------------------------------------------
# Slash command + wiring
# ---------------------------------------------------------------------------
booking = app_commands.Group(
    name="booking", description="Panel ticket Booking 1-on-1",
    guild_only=True, default_permissions=discord.Permissions(manage_guild=True),
)


@booking.command(name="panel", description="Kirim panel Booking 1-on-1 ke sebuah channel")
@app_commands.describe(channel="Kosongin buat channel ini")
async def booking_panel(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    target = channel or interaction.channel
    try:
        await target.send(embed=panel_embed(interaction.guild), view=PanelView())
    except discord.Forbidden:
        await interaction.response.send_message(f"Bot nggak bisa kirim pesan di {target.mention}.", ephemeral=True)
        return
    await interaction.response.send_message(f"Panel Booking 1-on-1 udah dipasang di {target.mention}.", ephemeral=True)


def setup(bot) -> None:
    bot.tree.add_command(booking)
    original_hook = bot.setup_hook

    async def setup_hook():
        await original_hook()
        bot.add_view(PanelView())
        bot.add_view(TicketView())
        bot.add_view(ReminderView())

    bot.setup_hook = setup_hook
    loop = make_loop(bot)

    async def _start():
        if not loop.is_running():
            loop.start()

    bot.add_listener(_start, "on_ready")
