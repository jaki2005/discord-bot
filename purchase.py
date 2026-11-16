"""
Buka Akses: purchase flow for NusaKlip products, fully inside NusaKlip AI.

  /akses panel produk:<blueprint|mentoring> [channel]
      Posts a product panel with two buttons (1 Bulan, 3 Bulan).

  Member clicks a plan -> short form (modal) -> bot creates a private room
  in PURCHASE_CATEGORY_ID with payment instructions and three buttons:
      Udah Bayar, Panggil Staff   (member, pings the staff role)
      Verifikasi & Buka Akses     (staff only: gives the product role, logs it)
      Tutup Room                  (member while pending, staff any time)

  Auto-close (checked every PURCHASE_CHECK_INTERVAL_MINUTES):
      pending  -> warning, then closed after PURCHASE_INACTIVITY_HOURS with no member/staff message
      verified -> closed PURCHASE_CLOSE_AFTER_VERIFY_HOURS after verification
      active   -> mentoring room, never auto-closed here
  Every room is archived (transcript zip) to ARCHIVE_CHANNEL_ID before deletion.

The bot never checks payments itself. A human presses Verifikasi.
All room state lives in the channel topic, so nothing is lost on a Railway redeploy.
"""

import io
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks

import archive

WIB = ZoneInfo("Asia/Jakarta")
COLOR = 0xF5A623


def _id(name: str) -> int | None:
    return int(os.getenv(name, "0") or 0) or None


PURCHASE_CATEGORY_ID = _id("PURCHASE_CATEGORY_ID")
MENTORING_CATEGORY_ID = _id("MENTORING_CATEGORY_ID")
STAFF_ROLE_ID = _id("STAFF_ROLE_ID")
PURCHASE_LOG_CHANNEL_ID = _id("PURCHASE_LOG_CHANNEL_ID")
INACTIVITY_HOURS = float(os.getenv("PURCHASE_INACTIVITY_HOURS", "24"))
WARNING_HOURS = float(os.getenv("PURCHASE_WARNING_HOURS_BEFORE_CLOSE", "4"))
CLOSE_AFTER_VERIFY_HOURS = float(os.getenv("PURCHASE_CLOSE_AFTER_VERIFY_HOURS", "24"))
CHECK_MINUTES = float(os.getenv("PURCHASE_CHECK_INTERVAL_MINUTES", "15"))
PING_COOLDOWN_SECONDS = 30 * 60

# Payment is QRIS only. The image ships as assets/qris.png; QRIS_IMAGE_URL is a fallback.
QRIS_IMAGE_URL = os.getenv("QRIS_IMAGE_URL", "").strip()
QRIS_FILE = Path(__file__).parent / "assets" / "qris.png"

WARNING_MARKER = "⏳ Pengingat:"

PRODUCTS = {
    "blueprint": {
        "name": "Full Clipping Blueprint",
        "short": "blueprint",
        "emoji": "🚀",
        "role_env": "BLUEPRINT_ROLE_ID",
        "plans": {
            "1b": {"label": "1 Bulan", "price": 229_000, "days": 30, "renewal": "Rp199.000 per bulan"},
            "3b": {"label": "3 Bulan", "price": 389_000, "days": 90, "renewal": None},
        },
        "panel": (
            "Belajar clipping dari nol sampai cuan lewat seri video lengkap, plus akses komunitas eksklusif.\n\n"
            "**Yang lu dapet:**\n"
            "• Breakdown clipping lengkap dan roadmap\n"
            "• Tutorial editing format clip viral\n"
            "• Setup dan warm-up akun, metode audiens Tier 1\n"
            "• Review clip harian dari coach\n"
            "• 2 webinar dan 2 Live Q&A tiap bulan\n"
            "• Akses prioritas ke campaign brand eksklusif\n"
            "• Paket aset (preset, SFX, overlay) dan giveaway eksklusif"
        ),
    },
    "mentoring": {
        "name": "Mentoring Eksklusif",
        "short": "mentoring",
        "emoji": "👑",
        "role_env": "MENTORING_ROLE_ID",
        "plans": {
            "1b": {"label": "1 Bulan", "price": 389_000, "days": 30, "renewal": "Rp389.000 per bulan"},
            "3b": {"label": "3 Bulan", "price": 829_000, "days": 90, "renewal": "Rp829.000 per 3 bulan"},
        },
        "panel": (
            "Semua isi Full Clipping Blueprint, ditambah dibimbing langsung sama mentor "
            "sampai hasil pertama lu kelihatan.\n\n"
            "**Tambahan dari Blueprint:**\n"
            "• Sesi mentoring personal, bukan cuma materi\n"
            "• Target dan evaluasi progress yang dipersonalisasi\n"
            "• Prioritas review clip dari mentor\n"
            "• Room mentoring pribadi di Discord\n\n"
            "Kuota mentoring dibatasi biar kualitas bimbingan tetap terjaga."
        ),
    },
}

_last_ping: dict[int, float] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def rupiah(n: int) -> str:
    return "Rp" + f"{n:,}".replace(",", ".")


def role_id_for(product: str) -> int | None:
    return _id(PRODUCTS[product]["role_env"])


def slug(text: str, limit: int = 20) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:limit].strip("-")) or "member"


def clean(value: str) -> str:
    """Values go into the channel topic, which uses | and = as separators."""
    return value.replace("|", "/").replace("=", "-").replace("\n", " ").strip()[:100]


def encode_topic(state: dict) -> str:
    return "NK|" + "|".join(f"{k}={v}" for k, v in state.items() if v not in (None, ""))


def parse_topic(topic: str | None) -> dict | None:
    if not topic or not topic.startswith("NK|"):
        return None
    out = {}
    for part in topic[3:].split("|"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v
    if "u" not in out or out.get("p") not in PRODUCTS:
        return None
    return out


def is_staff(member: discord.abc.User) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.manage_guild:
        return True
    return bool(STAFF_ROLE_ID and any(r.id == STAFF_ROLE_ID for r in member.roles))


def fmt_date(dt: datetime) -> str:
    bulan = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]
    d = dt.astimezone(WIB)
    return f"{d.day} {bulan[d.month - 1]} {d.year}"


def purchase_rooms(guild: discord.Guild) -> list[tuple[discord.TextChannel, dict]]:
    rooms = []
    for ch in guild.text_channels:
        state = parse_topic(ch.topic)
        if state:
            rooms.append((ch, state))
    return rooms


def staff_mention(guild: discord.Guild) -> str:
    role = guild.get_role(STAFF_ROLE_ID) if STAFF_ROLE_ID else None
    return role.mention if role else "tim NusaKlip"


# ---------------------------------------------------------------------------
# Panel (lives in the product channel)
# ---------------------------------------------------------------------------
def panel_embed(product: str) -> discord.Embed:
    p = PRODUCTS[product]
    one, three = p["plans"]["1b"], p["plans"]["3b"]
    desc = (
        p["panel"]
        + "\n\n**Pilih paket:**\n"
        + f"💎 1 Bulan: **{rupiah(one['price'])}**\n"
        + f"🚀 3 Bulan: **{rupiah(three['price'])}** (paling populer)\n\n"
        + "Klik salah satu tombol di bawah, isi form singkat, nanti bot bikinin "
        + "room pembayaran pribadi buat lu."
    )
    e = discord.Embed(title=f"{p['emoji']} {p['name']}", description=desc, color=COLOR)
    e.set_footer(text="NusaKlip • Pembayaran via QRIS")
    return e


class PlanButton(discord.ui.Button):
    def __init__(self, product: str, plan: str):
        info = PRODUCTS[product]["plans"][plan]
        super().__init__(
            label=f"{info['label']} · {rupiah(info['price'])}",
            emoji="💎" if plan == "1b" else "🚀",
            style=discord.ButtonStyle.success if plan == "1b" else discord.ButtonStyle.primary,
            custom_id=f"nk:buy:{product}:{plan}",
        )
        self.product, self.plan = product, plan

    async def callback(self, interaction: discord.Interaction):
        existing = next(
            (
                ch
                for ch, st in purchase_rooms(interaction.guild)
                if st.get("u") == str(interaction.user.id) and st.get("s") == "pending"
            ),
            None,
        )
        if existing:
            await interaction.response.send_message(
                f"Lu masih punya room pembayaran yang aktif: {existing.mention}. "
                "Selesaiin atau tutup yang itu dulu ya.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(PurchaseForm(self.product, self.plan))


class PanelView(discord.ui.View):
    def __init__(self, product: str):
        super().__init__(timeout=None)
        self.add_item(PlanButton(product, "1b"))
        self.add_item(PlanButton(product, "3b"))


# ---------------------------------------------------------------------------
# Form
# ---------------------------------------------------------------------------
class PurchaseForm(discord.ui.Modal):
    email = discord.ui.TextInput(
        label="Email WHOP / email terdaftar",
        placeholder="nama@email.com (dipakai buat buka akses materi)",
        max_length=100,
    )
    whop_username = discord.ui.TextInput(
        label="Username WHOP (kosongin kalau belum ada)",
        placeholder="Username WHOP lu kalau udah punya",
        required=False,
        max_length=50,
    )
    agree = discord.ui.TextInput(
        label="Konfirmasi: ketik SETUJU",
        placeholder="SETUJU",
        max_length=10,
    )

    def __init__(self, product: str, plan: str):
        p = PRODUCTS[product]
        super().__init__(title=f"Daftar {p['name']} {p['plans'][plan]['label']}"[:45])
        self.product, self.plan = product, plan

    async def on_submit(self, interaction: discord.Interaction):
        email = self.email.value.strip()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            await interaction.response.send_message(
                "Format email-nya kayaknya salah. Klik tombol paketnya lagi dan isi ulang ya.",
                ephemeral=True,
            )
            return
        if self.agree.value.strip().lower() != "setuju":
            await interaction.response.send_message(
                "Kolom konfirmasi harus diisi **SETUJU**. Klik tombol paketnya lagi ya.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        category = guild.get_channel(PURCHASE_CATEGORY_ID) if PURCHASE_CATEGORY_ID else None
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message(
                "Sistem pembayaran belum siap (kategori belum diset). Tag tim NusaKlip ya.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        member = interaction.user
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

        state = {
            "u": member.id,
            "p": self.product,
            "pl": self.plan,
            "s": "pending",
            "e": clean(email),
            "wu": clean(self.whop_username.value) if self.whop_username.value.strip() else "",
        }
        name = f"{PRODUCTS[self.product]['short']}-{self.plan.replace('b', 'bln')}-{slug(member.name)}"
        try:
            room = await category.create_text_channel(
                name=name,
                overwrites=overwrites,
                topic=encode_topic(state),
                reason=f"Buka Akses {self.product} {self.plan} oleh {member}",
            )
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"Gagal bikin room ({type(e).__name__}). Tag tim NusaKlip ya.", ephemeral=True
            )
            return

        embed, file = room_embed(self.product, self.plan, member, state)
        kwargs = {"file": file} if file else {}
        await room.send(
            content=f"👋 {member.mention}, room pendaftaran lu udah siap!",
            embed=embed,
            view=RoomView(),
            allowed_mentions=discord.AllowedMentions(users=True),
            **kwargs,
        )
        await interaction.followup.send(
            f"✅ Room lu udah dibuat: {room.mention}. Lanjut bayar di sana ya.", ephemeral=True
        )


# ---------------------------------------------------------------------------
# Private room
# ---------------------------------------------------------------------------
def room_embed(product: str, plan: str, member: discord.abc.User, state: dict):
    p = PRODUCTS[product]
    info = p["plans"][plan]
    price = rupiah(info["price"])
    e = discord.Embed(
        title=f"{p['emoji']} {p['name']}: Paket {info['label']}",
        description=(
            f"Hai {member.mention}, ini room pendaftaran pribadi lu. "
            "Cuma lu dan tim NusaKlip yang bisa lihat room ini."
        ),
        color=COLOR,
    )
    paket = (
        f"• **Produk:** {p['name']}\n"
        f"• **Durasi:** {info['label']} ({info['days']} hari)\n"
        f"• **Harga:** {price}"
    )
    if info["renewal"]:
        paket += f"\n• **Perpanjangan:** {info['renewal']}"
    e.add_field(name="📦 Paket yang lu pilih", value=paket, inline=False)

    pay = (
        "Scan QRIS di bawah pakai GoPay, OVO, DANA, ShopeePay, atau m-banking apa aja.\n"
        f"Masukin nominal pas **{price}**, terus screenshot bukti bayarnya.\n"
        "Pastiin nama merchant-nya **NUSAKLIP, KOMUNITAS CLIPP**."
    )
    e.add_field(name="💳 Langkah 1: Bayar", value=pay, inline=False)
    e.add_field(
        name="🧾 Langkah 2: Kirim bukti",
        value="Upload screenshot bukti bayar langsung di room ini, terus klik **Udah Bayar, Panggil Staff**.",
        inline=False,
    )
    e.add_field(
        name="📧 Data WHOP lu",
        value=f"• **Email:** `{state.get('e', '-')}`\n• **Username:** `{state.get('wu') or 'belum ada'}`\n"
        "(Dipakai tim buat buka akses materi lu.)",
        inline=False,
    )
    e.add_field(
        name="⏳ Auto-close",
        value=f"Room ini otomatis ketutup kalau {INACTIVITY_HOURS:g} jam nggak ada aktivitas, "
        f"atau {CLOSE_AFTER_VERIFY_HOURS:g} jam setelah akses lu dibuka.",
        inline=False,
    )
    e.add_field(
        name="⚠️ Hati-hati penipuan",
        value="Pembayaran resmi cuma lewat QRIS di room ini, nggak ada transfer rekening. "
        "Tim NusaKlip nggak pernah minta password, OTP, atau PIN.",
        inline=False,
    )
    e.set_footer(text="NusaKlip • Verifikasi maksimal 1x24 jam")

    file = None
    if QRIS_FILE.exists():
        file = discord.File(QRIS_FILE, filename="qris.png")
        e.set_image(url="attachment://qris.png")
    elif QRIS_IMAGE_URL:
        e.set_image(url=QRIS_IMAGE_URL)
    return e, file


async def _room_state(interaction: discord.Interaction) -> dict | None:
    state = parse_topic(getattr(interaction.channel, "topic", None))
    if state is None:
        await interaction.response.send_message("Ini bukan room pembayaran.", ephemeral=True)
    return state


class RoomView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Udah Bayar, Panggil Staff", emoji="📸",
                       style=discord.ButtonStyle.primary, custom_id="nk:pay:ping")
    async def ping(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = await _room_state(interaction)
        if state is None:
            return
        if str(interaction.user.id) != state["u"] and not is_staff(interaction.user):
            await interaction.response.send_message("Tombol ini buat pemilik room.", ephemeral=True)
            return
        if state.get("s") != "pending":
            await interaction.response.send_message("Akses di room ini udah diverifikasi.", ephemeral=True)
            return
        has_proof = False
        async for msg in interaction.channel.history(limit=50):
            if str(msg.author.id) == state["u"] and msg.attachments:
                has_proof = True
                break
        if not has_proof:
            await interaction.response.send_message(
                "Upload dulu screenshot bukti bayar lu di room ini, baru klik tombol ini lagi ya.",
                ephemeral=True,
            )
            return
        now = time.time()
        if now - _last_ping.get(interaction.channel.id, 0) < PING_COOLDOWN_SECONDS:
            await interaction.response.send_message(
                "Staff udah dipanggil barusan, tunggu sebentar ya. Verifikasi maksimal 1x24 jam.",
                ephemeral=True,
            )
            return
        _last_ping[interaction.channel.id] = now
        await interaction.response.send_message(
            f"🔔 {staff_mention(interaction.guild)}, <@{state['u']}> udah kirim bukti bayar. Tolong dicek ya.",
            allowed_mentions=discord.AllowedMentions(roles=True, users=False),
        )

    @discord.ui.button(label="Verifikasi & Buka Akses", emoji="✅",
                       style=discord.ButtonStyle.success, custom_id="nk:pay:verify")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = await _room_state(interaction)
        if state is None:
            return
        if not is_staff(interaction.user):
            await interaction.response.send_message(
                "Tombol ini cuma buat staff. Tunggu tim NusaKlip ngecek bukti bayar lu ya.", ephemeral=True
            )
            return
        if state.get("s") != "pending":
            await interaction.response.send_message("Room ini udah diverifikasi sebelumnya.", ephemeral=True)
            return

        guild = interaction.guild
        product, plan = state["p"], state.get("pl", "1b")
        info = PRODUCTS[product]["plans"][plan]
        member = guild.get_member(int(state["u"]))
        if member is None:
            await interaction.response.send_message(
                "Member ini udah nggak ada di server, role nggak bisa dikasih.", ephemeral=True
            )
            return

        role = guild.get_role(role_id_for(product) or 0)
        if role is None:
            await interaction.response.send_message(
                f"Role buat {PRODUCTS[product]['name']} belum diset di Railway "
                f"({PRODUCTS[product]['role_env']}).",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        try:
            await member.add_roles(role, reason=f"Diverifikasi oleh {interaction.user}")
        except discord.Forbidden:
            await interaction.followup.send(
                f"Bot nggak bisa kasih role {role.mention}. Pastiin role NusaKlip AI ada **di atas** "
                "role itu di Server Settings → Roles, dan punya permission Manage Roles.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        now = datetime.now(timezone.utc)
        expires = now + timedelta(days=info["days"])
        state.update({"s": "verified", "v": int(now.timestamp()), "x": int(expires.timestamp()),
                      "by": interaction.user.id})

        edit = {}
        is_mentoring = product == "mentoring"
        if is_mentoring:
            state["s"] = "active"
            edit["name"] = f"mentee-{slug(member.name)}"
            mcat = guild.get_channel(MENTORING_CATEGORY_ID) if MENTORING_CATEGORY_ID else None
            if isinstance(mcat, discord.CategoryChannel):
                edit["category"] = mcat
        edit["topic"] = encode_topic(state)
        try:
            await interaction.channel.edit(**edit, reason="Pembayaran diverifikasi")
        except discord.HTTPException:
            pass

        done = discord.Embed(
            title="✅ Pembayaran terverifikasi",
            description=(
                f"{member.mention}, akses **{PRODUCTS[product]['name']} ({info['label']})** lu udah aktif "
                f"sampai **{fmt_date(expires)}**.\n\nRole {role.mention} udah ditambahin. "
                "Akses materi WHOP lu bakal dikirim tim ke email yang lu isi."
            ),
            color=0x2ECC71,
        )
        if is_mentoring:
            done.add_field(
                name="👑 Room mentoring",
                value="Room ini sekarang jadi room mentoring pribadi lu. Mentor bakal nyapa lu di sini.",
                inline=False,
            )
        else:
            done.add_field(
                name="⏳ Room ini",
                value=f"Room ini otomatis ketutup {CLOSE_AFTER_VERIFY_HOURS:g} jam lagi. "
                "Kalau ada yang mau ditanyain, sekarang waktunya.",
                inline=False,
            )
        done.set_footer(text=f"Diverifikasi oleh {interaction.user.display_name}")
        await interaction.followup.send(
            content=member.mention, embed=done,
            allowed_mentions=discord.AllowedMentions(users=True),
        )
        await _log_verification(guild, member, state, interaction.user, expires)

    @discord.ui.button(label="Tutup Room", emoji="❌",
                       style=discord.ButtonStyle.secondary, custom_id="nk:pay:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = await _room_state(interaction)
        if state is None:
            return
        owner = str(interaction.user.id) == state["u"]
        if not (is_staff(interaction.user) or (owner and state.get("s") == "pending")):
            await interaction.response.send_message("Lu nggak bisa nutup room ini.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Yakin mau nutup room ini? Transkripnya disimpan, lalu room dihapus.",
            view=ConfirmClose(interaction.user.id),
            ephemeral=True,
        )


class ConfirmClose(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=60)
        self.user_id = user_id

    @discord.ui.button(label="Ya, tutup", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return
        await interaction.response.edit_message(content="Menutup room...", view=None)
        ok = await close_room(interaction.channel, f"ditutup oleh {interaction.user}")
        if not ok:
            await interaction.followup.send(
                "Transkrip gagal disimpan, jadi room nggak dihapus. Cek ARCHIVE_CHANNEL_ID.", ephemeral=True
            )


async def _log_verification(guild, member, state, staff, expires):
    ch = guild.get_channel(PURCHASE_LOG_CHANNEL_ID) if PURCHASE_LOG_CHANNEL_ID else None
    if not isinstance(ch, discord.TextChannel):
        return
    p = PRODUCTS[state["p"]]
    info = p["plans"][state.get("pl", "1b")]
    e = discord.Embed(title=f"🧾 Akses dibuka: {p['name']} ({info['label']})", color=0x2ECC71)
    e.add_field(name="Member", value=f"{member.mention}\n`{member.id}`")
    e.add_field(name="Harga", value=rupiah(info["price"]))
    e.add_field(name="Aktif sampai", value=fmt_date(expires))
    e.add_field(name="Email WHOP", value=f"`{state.get('e', '-')}`")
    e.add_field(name="Username WHOP", value=f"`{state.get('wu') or '-'}`")
    e.add_field(name="Diverifikasi oleh", value=staff.mention)
    try:
        await ch.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        pass


async def close_room(ch: discord.TextChannel, reason: str) -> bool:
    """Archive transcript, then delete. Returns False (and keeps the room) if archiving fails."""
    target = ch.guild.get_channel(archive.ARCHIVE_CHANNEL_ID) if archive.ARCHIVE_CHANNEL_ID else None
    if not isinstance(target, discord.TextChannel):
        return False
    try:
        data, info = await archive.build_archive(ch, ch.guild.filesize_limit)
        state = parse_topic(ch.topic) or {}
        who = f"<@{state['u']}>" if state.get("u") else "-"
        await target.send(
            f"🧾 **#{ch.name}** | {info['count']} pesan | member {who} | {reason}",
            file=discord.File(io.BytesIO(data), filename=f"{ch.name}.zip"),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await ch.delete(reason=reason)
        return True
    except Exception as e:
        print(f"close_room failed for #{ch.name}: {type(e).__name__}: {e}")
        return False


# ---------------------------------------------------------------------------
# Auto-close loop
# ---------------------------------------------------------------------------
async def _last_activity(ch: discord.TextChannel) -> tuple[datetime, bool]:
    """(time of last non-bot message or room creation, whether a warning came after it)."""
    warned = False
    async for msg in ch.history(limit=50):
        if msg.author.bot:
            if msg.content.startswith(WARNING_MARKER):
                warned = True
            continue
        return msg.created_at, warned
    return ch.created_at, warned


def make_loop(bot: discord.Client):
    @tasks.loop(minutes=CHECK_MINUTES)
    async def purchase_autoclose():
        now = datetime.now(timezone.utc)
        for guild in bot.guilds:
            for ch, state in purchase_rooms(guild):
                try:
                    status = state.get("s")
                    if status == "verified":
                        verified = datetime.fromtimestamp(int(state.get("v", 0)), timezone.utc)
                        if now - verified >= timedelta(hours=CLOSE_AFTER_VERIFY_HOURS):
                            await close_room(ch, "otomatis, akses sudah dibuka")
                    elif status == "pending":
                        last, warned = await _last_activity(ch)
                        idle = now - last
                        if idle >= timedelta(hours=INACTIVITY_HOURS):
                            await close_room(ch, f"otomatis, {INACTIVITY_HOURS:g} jam tidak aktif")
                        elif not warned and idle >= timedelta(hours=INACTIVITY_HOURS - WARNING_HOURS):
                            await ch.send(
                                f"{WARNING_MARKER} <@{state['u']}>, room ini bakal ketutup otomatis "
                                f"sekitar {WARNING_HOURS:g} jam lagi kalau belum ada aktivitas. "
                                "Kalau udah bayar, upload buktinya dan klik **Udah Bayar, Panggil Staff**.",
                                allowed_mentions=discord.AllowedMentions(users=True),
                            )
                except Exception as e:
                    print(f"purchase_autoclose error on #{ch.name}: {type(e).__name__}: {e}")

    @purchase_autoclose.before_loop
    async def _wait():
        await bot.wait_until_ready()

    return purchase_autoclose


# ---------------------------------------------------------------------------
# Slash command
# ---------------------------------------------------------------------------
akses = app_commands.Group(
    name="akses",
    description="Panel Buka Akses (pembelian Blueprint dan Mentoring)",
    guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)


@akses.command(name="panel", description="Kirim panel pembelian produk ke sebuah channel")
@app_commands.describe(produk="Produk yang mau dipasang panelnya", channel="Kosongin buat channel ini")
@app_commands.choices(produk=[
    app_commands.Choice(name="Full Clipping Blueprint", value="blueprint"),
    app_commands.Choice(name="Mentoring Eksklusif", value="mentoring"),
])
async def akses_panel(
    interaction: discord.Interaction,
    produk: app_commands.Choice[str],
    channel: discord.TextChannel | None = None,
):
    target = channel or interaction.channel
    try:
        await target.send(embed=panel_embed(produk.value), view=PanelView(produk.value))
    except discord.Forbidden:
        await interaction.response.send_message(
            f"Bot nggak bisa kirim pesan di {target.mention}.", ephemeral=True
        )
        return
    await interaction.response.send_message(f"Panel {produk.name} udah dipasang di {target.mention}.", ephemeral=True)


@akses.command(name="cek", description="Cek setup Buka Akses (kategori, role, QRIS, dll)")
async def akses_cek(interaction: discord.Interaction):
    g = interaction.guild

    def line(label, ok, detail):
        return f"{'✅' if ok else '❌'} **{label}:** {detail}"

    cat = g.get_channel(PURCHASE_CATEGORY_ID) if PURCHASE_CATEGORY_ID else None
    staff = g.get_role(STAFF_ROLE_ID) if STAFF_ROLE_ID else None
    log = g.get_channel(PURCHASE_LOG_CHANNEL_ID) if PURCHASE_LOG_CHANNEL_ID else None
    arch = g.get_channel(archive.ARCHIVE_CHANNEL_ID) if archive.ARCHIVE_CHANNEL_ID else None
    me = g.me
    lines = [
        line("Kategori room pembayaran", isinstance(cat, discord.CategoryChannel),
             cat.name if cat else "PURCHASE_CATEGORY_ID belum diset"),
        line("Role staff", staff is not None, staff.mention if staff else "STAFF_ROLE_ID belum diset"),
    ]
    for key, p in PRODUCTS.items():
        r = g.get_role(role_id_for(key) or 0)
        detail = r.mention if r else f"{p['role_env']} belum diset"
        if r and r >= me.top_role:
            detail += " (role bot harus DI ATAS role ini)"
        lines.append(line(f"Role {p['name']}", bool(r) and r < me.top_role, detail))
    lines += [
        line("QRIS", QRIS_FILE.exists() or bool(QRIS_IMAGE_URL),
             "ada" if (QRIS_FILE.exists() or QRIS_IMAGE_URL) else "belum ada (QRIS_IMAGE_URL)"),
        line("Channel log verifikasi", isinstance(log, discord.TextChannel),
             log.mention if log else "PURCHASE_LOG_CHANNEL_ID belum diset (opsional)"),
        line("Channel arsip transkrip", isinstance(arch, discord.TextChannel),
             arch.mention if arch else "ARCHIVE_CHANNEL_ID belum diset"),
        line("Permission Manage Channels", me.guild_permissions.manage_channels, "wajib"),
        line("Permission Manage Roles", me.guild_permissions.manage_roles, "wajib buat kasih role"),
    ]
    if MENTORING_CATEGORY_ID:
        mc = g.get_channel(MENTORING_CATEGORY_ID)
        lines.append(line("Kategori room mentoring", isinstance(mc, discord.CategoryChannel),
                          mc.name if mc else "ID salah"))
    pending = sum(1 for _, s in purchase_rooms(g) if s.get("s") == "pending")
    lines.append(f"\nRoom pembayaran yang masih nunggu: **{pending}**")
    await interaction.response.send_message("\n".join(lines), ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------
def setup(bot) -> None:
    bot.tree.add_command(akses)
    for product in PRODUCTS:
        bot.add_view(PanelView(product))
    bot.add_view(RoomView())
    loop = make_loop(bot)

    async def _start_loop():
        if not loop.is_running():
            loop.start()

    bot.add_listener(_start_loop, "on_ready")
