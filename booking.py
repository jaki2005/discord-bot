"""
Coach tickets: Booking 1-on-1 and Bantuan (paid members). Replaces Ticket Tool.

  /booking panel [channel]   in #booking-1on1: Mentoring members book a 1-on-1 session
  /bantuan panel [channel]   in #bantuan: Blueprint or Mentoring members ask for help

  Click -> private ticket (member + COACH_ROLE_ID + STAFF_ROLE_ID + bot) in the kind's category.
  In the ticket: "Panggil Coach" (bot tags the coach role; the member must write something
  first; 30 min cooldown) and "Selesai, tutup ticket" (member, coach or staff).
  Payment rooms are never visible to coaches; these tickets are the only place they are added.

  Auto-close: after the kind's inactivity hours without a member/coach message the bot asks
  Udah / Belum (Belum tags the coach role); no answer for the force-close hours closes it.
  Transcripts go to ARCHIVE_CHANNEL_ID.
"""

import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import discord
from discord import app_commands
from discord.ext import tasks

import consult as C

COACH_ROLE_ID = C._id("COACH_ROLE_ID")
CHECK_MINUTES = float(os.getenv("BOOKING_CHECK_INTERVAL_MINUTES", "30"))
PING_COOLDOWN_SECONDS = 30 * 60
_last_ping: dict[int, float] = {}


@dataclass
class Kind:
    key: str            # url marker k=<key>
    cid: str            # custom_id prefix (stable, never change)
    topic: str          # channel topic prefix
    name_prefix: str
    category_env: str
    role_envs: tuple    # member roles allowed to open (staff always allowed)
    inactivity_env: str
    force_env: str
    color: int
    panel_title: str
    panel_text: str
    button_label: str
    button_emoji: str
    button_style: discord.ButtonStyle
    deny_text: str
    ticket_title: str
    ticket_text: str    # {member}
    ping_text: str      # {coach} {owner}
    reminder_q: str
    done_text: str

    @property
    def category_id(self):
        return C._id(self.category_env)

    @property
    def inactivity(self):
        return float(os.getenv(self.inactivity_env, "48"))

    @property
    def force_close(self):
        return float(os.getenv(self.force_env, "24"))


KINDS = {
    "booking": Kind(
        key="booking", cid="book", topic="🎫 Booking 1-on-1", name_prefix="booking",
        category_env="BOOKING_CATEGORY_ID", role_envs=("MENTORING_ROLE_ID",),
        inactivity_env="BOOKING_INACTIVITY_HOURS", force_env="BOOKING_FORCE_CLOSE_HOURS",
        color=0x2ECC71,
        panel_title="🎫 BOOKING 1-ON-1 NUSAKLIP",
        panel_text=(
            "Mau mendapatkan sesi 1-on-1 bersama tim NusaKlip?\n"
            "Klik tombol di bawah untuk membuat ticket dan masuk ke antrean sesi 1-on-1.\n\n"
            "**Setelah ticket kebuat, jangan lupa:**\n"
            "1. Tulis kamu bisanya hari apa dan jam berapa\n"
            "2. Klik tombol **Panggil Coach**, nanti NusaKlip AI yang ngetag coach\n"
            "Coach bakal kasih tahu waktu available-nya di ticket kamu.\n\n"
            "Mohon bersabar selama menunggu giliran. Kami akan menghubungi kamu melalui "
            "ticket ini ketika sesi kamu sudah siap."
        ),
        button_label="BOOKING 1-ON-1", button_emoji="🎫", button_style=discord.ButtonStyle.primary,
        deny_text="Booking 1-on-1 khusus buat member **Mentoring Eksklusif** ya.",
        ticket_title="🎫 Booking 1-on-1",
        ticket_text=(
            "Halo {member}! Ticket booking 1-on-1 kamu udah kebuka.\n\n"
            "**Biar cepat dijadwalin:**\n"
            "1. Tulis kamu bisanya hari apa dan jam berapa "
            "(contoh: Rabu atau Kamis, jam 19.00 sampai 21.00 WIB)\n"
            "2. Kalau ada yang mau dibahas (clip, akun, strategi), tulis juga ya\n"
            "3. Klik **Panggil Coach**, nanti NusaKlip AI yang ngetag coach\n\n"
            "Nanti coach bakal kasih tahu waktu available-nya di sini. "
            "Kalau sesinya udah selesai, klik **Selesai, tutup ticket**."
        ),
        ping_text=("🔔 {coach}, {owner} mau jadwalin sesi 1-on-1. "
                   "Jadwal yang dia bisa ada di atas, tolong kabarin kapan kamu available ya."),
        reminder_q="sesi 1-on-1 kamu udah beres?",
        done_text="Sip, ticket ditutup. Makasih udah booking 1-on-1! 🙏",
    ),
    "bantuan": Kind(
        key="bantuan", cid="help", topic="🌟 Bantuan VIP", name_prefix="bantuan",
        category_env="BANTUAN_CATEGORY_ID", role_envs=("BLUEPRINT_ROLE_ID", "MENTORING_ROLE_ID"),
        inactivity_env="BANTUAN_INACTIVITY_HOURS", force_env="BANTUAN_FORCE_CLOSE_HOURS",
        color=0xE74C3C,
        panel_title="🌟 Bantuan VIP",
        panel_text=(
            "Halo Member VIP! Punya kendala atau pertanyaan seputar materi?\n\n"
            "Klik tombol di bawah untuk terhubung langsung dengan Tim Staf lewat jalur prioritas. "
            "Kami akan mendampingimu sampai tuntas!\n\n"
            "**Setelah ticket kebuat:**\n"
            "1. Tulis pertanyaan atau kendala kamu, sertakan screenshot kalau ada\n"
            "2. Klik tombol **Panggil Coach**, nanti NusaKlip AI yang ngetag coach"
        ),
        button_label="BUAT BANTUAN", button_emoji="🌟", button_style=discord.ButtonStyle.danger,
        deny_text="Bantuan VIP khusus buat member **Full Clipping Blueprint** atau **Mentoring Eksklusif** ya.",
        ticket_title="🌟 Bantuan VIP",
        ticket_text=(
            "Halo {member}! Ticket bantuan kamu udah kebuka.\n\n"
            "**Biar cepat dibantu:**\n"
            "1. Ceritain pertanyaan atau kendala kamu sedetail mungkin\n"
            "2. Sertakan screenshot kalau ada\n"
            "3. Klik **Panggil Coach**, nanti NusaKlip AI yang ngetag coach\n\n"
            "Kalau udah beres, klik **Selesai, tutup ticket**."
        ),
        ping_text="🔔 {coach}, {owner} butuh bantuan. Pertanyaannya ada di atas, tolong dibantu ya.",
        reminder_q="masalah kamu udah beres?",
        done_text="Sip, ticket ditutup. Semoga lancar terus clipping-nya! 🙏",
    ),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
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


def ticket_url(kind: Kind, user_id: int) -> str:
    return C.STATE_BASE + "?" + urlencode({"nk": "1", "k": kind.key, "u": str(user_id)})


def kind_channels(guild, kind: Kind) -> list[discord.TextChannel]:
    return [ch for ch in guild.text_channels if ch.topic and ch.topic.startswith(kind.topic)]


def booking_channels(guild):  # kept for older callers
    return kind_channels(guild, KINDS["booking"])


async def _can_close(interaction) -> bool:
    if is_coach_or_staff(interaction.user):
        return True
    return await C.ticket_owner(interaction.channel) == interaction.user.id


def panel_embed(kind: Kind) -> discord.Embed:
    return discord.Embed(title=kind.panel_title, description=kind.panel_text, color=kind.color)


# ---------------------------------------------------------------------------
# Views (built per kind; custom_ids nk:<cid>:<action> must stay stable)
# ---------------------------------------------------------------------------
def _button(label, emoji, style, custom_id, callback):
    b = discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=custom_id)
    b.callback = callback
    return b


class PanelView(C.LoggedView):
    def __init__(self, kind: Kind):
        super().__init__(timeout=None)
        self.kind = kind
        self.add_item(_button(kind.button_label, kind.button_emoji, kind.button_style,
                              f"nk:{kind.cid}:open", self.open_ticket))

    async def open_ticket(self, interaction: discord.Interaction):
        kind, guild, member = self.kind, interaction.guild, interaction.user
        allowed = {C._id(env) for env in kind.role_envs} - {None}
        role_ids = {r.id for r in getattr(member, "roles", [])}
        if not (C.is_staff(member) or allowed & role_ids):
            await interaction.response.send_message(kind.deny_text, ephemeral=True)
            return
        for ch in kind_channels(guild, kind):
            if any(getattr(t, "id", None) == member.id for t in ch.overwrites):
                await interaction.response.send_message(f"Kamu masih punya ticket yang kebuka: {ch.mention}",
                                                        ephemeral=True)
                return
        category = guild.get_channel(kind.category_id) if kind.category_id else None
        if not isinstance(category, discord.CategoryChannel):
            category = interaction.channel.category

        await interaction.response.defer(ephemeral=True, thinking=True)
        rw = dict(view_channel=True, send_messages=True, attach_files=True, read_message_history=True)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(embed_links=True, **rw),
            guild.me: discord.PermissionOverwrite(embed_links=True, manage_channels=True, manage_messages=True, **rw),
        }
        for role in (coach_role(guild), guild.get_role(C.STAFF_ROLE_ID) if C.STAFF_ROLE_ID else None):
            if role:
                overwrites[role] = discord.PermissionOverwrite(**rw)
        try:
            ch = await guild.create_text_channel(
                name=f"{kind.name_prefix}-{C.slug(member.name)}", category=category, overwrites=overwrites,
                topic=f"{kind.topic} • {member.display_name}", reason=f"{kind.ticket_title} oleh {member}",
            )
        except discord.HTTPException as e:
            await interaction.followup.send(f"Gagal bikin ticket ({type(e).__name__}). Tag tim NusaKlip ya.",
                                            ephemeral=True)
            return
        e = discord.Embed(title=kind.ticket_title, url=ticket_url(kind, member.id),
                          description=kind.ticket_text.format(member=member.mention), color=kind.color)
        e.set_footer(text=f"Ticket otomatis dicek kalau {kind.inactivity:g} jam nggak ada aktivitas")
        await ch.send(content=member.mention, embed=e, view=TicketView(kind),
                      allowed_mentions=discord.AllowedMentions(users=True))
        await interaction.followup.send(f"✅ Ticket kamu udah dibuka: {ch.mention}", ephemeral=True)


class TicketView(C.LoggedView):
    def __init__(self, kind: Kind):
        super().__init__(timeout=None)
        self.kind = kind
        self.add_item(_button("Panggil Coach", "🔔", discord.ButtonStyle.primary, f"nk:{kind.cid}:ping", self.ping))
        self.add_item(_button("Selesai, tutup ticket", "✅", discord.ButtonStyle.secondary,
                              f"nk:{kind.cid}:close", self.close))

    async def ping(self, interaction: discord.Interaction):
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
                "Tulis dulu kebutuhan kamu di ticket ini, baru panggil coach ya.", ephemeral=True)
            return
        now = time.time()
        if now - _last_ping.get(interaction.channel.id, 0) < PING_COOLDOWN_SECONDS:
            await interaction.response.send_message("Coach udah dipanggil barusan, tunggu balasannya sebentar ya.",
                                                    ephemeral=True)
            return
        _last_ping[interaction.channel.id] = now
        await interaction.response.send_message(
            self.kind.ping_text.format(coach=coach_mention(interaction.guild), owner=f"<@{owner}>"),
            allowed_mentions=discord.AllowedMentions(roles=True, users=False),
        )

    async def close(self, interaction: discord.Interaction):
        if not await _can_close(interaction):
            await interaction.response.send_message("Kamu nggak bisa nutup ticket ini.", ephemeral=True)
            return
        await interaction.response.send_message(self.kind.done_text)
        if not await C.close_ticket(interaction.channel, f"ditutup oleh {interaction.user}"):
            await interaction.followup.send("Transkrip gagal disimpan, jadi ticket nggak dihapus.", ephemeral=True)


class ReminderView(C.LoggedView):
    def __init__(self, kind: Kind):
        super().__init__(timeout=None)
        self.kind = kind
        self.add_item(_button("Udah", "✅", discord.ButtonStyle.success, f"nk:{kind.cid}:yes", self.yes))
        self.add_item(_button("Belum", "🙋", discord.ButtonStyle.danger, f"nk:{kind.cid}:no", self.no))

    async def yes(self, interaction):
        if not await _can_close(interaction):
            await interaction.response.send_message("Tombol ini buat pemilik ticket.", ephemeral=True)
            return
        await interaction.response.edit_message(content="✅ Dijawab: udah beres.", view=None)
        await interaction.channel.send(self.kind.done_text)
        await C.close_ticket(interaction.channel, f"udah beres (dijawab {interaction.user})")

    async def no(self, interaction):
        if not await _can_close(interaction):
            await interaction.response.send_message("Tombol ini buat pemilik ticket.", ephemeral=True)
            return
        await interaction.response.edit_message(content="🙋 Dijawab: belum beres.", view=None)
        await interaction.channel.send(  # 🔔 counts as fresh activity (see consult._activity)
            f"🔔 {coach_mention(interaction.guild)}, {interaction.user.mention} masih butuh bantuan di ticket ini.",
            allowed_mentions=discord.AllowedMentions(roles=True, users=False),
        )


# ---------------------------------------------------------------------------
# Auto-close loop
# ---------------------------------------------------------------------------
def make_loop(bot):
    @tasks.loop(minutes=CHECK_MINUTES)
    async def coach_ticket_autoclose():
        now = datetime.now(timezone.utc)
        for guild in bot.guilds:
            for kind in KINDS.values():
                for ch in kind_channels(guild, kind):
                    try:
                        last, reminder_at = await C._activity(ch)
                        if reminder_at:
                            if now - reminder_at >= timedelta(hours=kind.force_close):
                                await C.close_ticket(ch, f"otomatis, nggak ada jawaban {kind.force_close:g} jam")
                        elif now - last >= timedelta(hours=kind.inactivity):
                            owner = await C.ticket_owner(ch)
                            who = f"<@{owner}>" if owner else "Halo"
                            await ch.send(
                                f"{C.REMINDER_MARKER} {who}, {kind.reminder_q} Kalau nggak ada jawaban dalam "
                                f"{kind.force_close:g} jam, ticket ini ditutup otomatis.",
                                view=ReminderView(kind), allowed_mentions=discord.AllowedMentions(users=True),
                            )
                    except Exception as e:
                        print(f"[{kind.key}] autoclose error on #{ch.name}: {type(e).__name__}: {e}", flush=True)

    @coach_ticket_autoclose.before_loop
    async def _wait():
        await bot.wait_until_ready()

    return coach_ticket_autoclose


# ---------------------------------------------------------------------------
# Slash commands + wiring
# ---------------------------------------------------------------------------
def _panel_group(kind: Kind, name: str, label: str) -> app_commands.Group:
    group = app_commands.Group(name=name, description=f"Panel ticket {label}", guild_only=True,
                               default_permissions=discord.Permissions(manage_guild=True))

    @group.command(name="panel", description=f"Kirim panel {label} ke sebuah channel")
    @app_commands.describe(channel="Kosongin buat channel ini")
    async def panel(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        target = channel or interaction.channel
        try:
            await target.send(embed=panel_embed(kind), view=PanelView(kind))
        except discord.Forbidden:
            await interaction.response.send_message(f"Bot nggak bisa kirim pesan di {target.mention}.", ephemeral=True)
            return
        await interaction.response.send_message(f"Panel {label} udah dipasang di {target.mention}.", ephemeral=True)

    return group


booking = _panel_group(KINDS["booking"], "booking", "Booking 1-on-1")
bantuan = _panel_group(KINDS["bantuan"], "bantuan", "Bantuan VIP")


def setup(bot) -> None:
    bot.tree.add_command(booking)
    bot.tree.add_command(bantuan)
    original_hook = bot.setup_hook

    async def setup_hook():
        await original_hook()
        for kind in KINDS.values():
            bot.add_view(PanelView(kind))
            bot.add_view(TicketView(kind))
            bot.add_view(ReminderView(kind))

    bot.setup_hook = setup_hook
    loop = make_loop(bot)

    async def _start():
        if not loop.is_running():
            loop.start()

    bot.add_listener(_start, "on_ready")
