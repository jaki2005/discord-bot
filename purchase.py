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
Room state lives in the title link of the bot's first message in each room (not in the
channel topic, which stays human readable), so nothing is lost on a Railway redeploy.
"""

import io
import os
import re
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
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
STATE_BASE = "https://nusaklip.online/"
ROOM_PREFIXES = ("⏳", "✅", "👑")

# Sent after verification (replaces the welcome message staff used to type by hand).
WHOP_URL = os.getenv("WHOP_URL", "https://whop.com/nusaklip").strip()
VIP_GUIDE_CHANNEL_ID = os.getenv("VIP_GUIDE_CHANNEL_ID", "").strip()
BOOKING_CHANNEL_ID = os.getenv("BOOKING_CHANNEL_ID", "").strip()
HELP_CHANNEL_ID = os.getenv("HELP_CHANNEL_ID", "").strip()

DIV = "\n\u200b\n_________________________\n\n"

PRODUCTS = {
    "blueprint": {
        "name": "Full Clipping Blueprint",
        "short": "blueprint",
        "emoji": "📘",
        "role_env": "BLUEPRINT_ROLE_ID",
        "plans": {
            "1b": {"label": "1 Bulan", "price": 229_000, "days": 30, "renewal": "Rp199.000 per bulan"},
            "3b": {"label": "3 Bulan", "price": 389_000, "days": 90, "renewal": "Rp100.000 per 3 bulan"},
        },
        "panel_title": "📘 Full Clipping Blueprint (ELITE CLASS)",
        "panel": (
            "💎 **PILIH PAKET FULL CLIPPING BLUEPRINT**\n\n"
            "🥇 **FULL CLIPPING BLUEPRINT: 1 BULAN**\n"
            "💰 Harga promo: **Rp229.000**\n"
            "🔄 Perpanjangan Rp199.000/bulan.\n"
            "🎯 Cocok buat kamu yang ingin mencoba program ini terlebih dahulu, belajar, "
            "dan mulai membangun hasil pertamamu.\n\n"
            "🥇 **FULL CLIPPING BLUEPRINT: 3 BULAN** 🔥 *PALING POPULER*\n"
            "💰 Harga: **Rp389.000**\n"
            "🔄 Perpanjangan Rp100.000/3 bulan.\n"
            "🎯 Cocok buat kamu yang serius membangun skill clipping, konsisten berproses, "
            "dan punya waktu lebih panjang untuk mengejar hasil pertamamu."
            + DIV +
            "🤝 **KOMUNITAS & BIMBINGAN**\n\n"
            "✅ Akses komunitas eksklusif & support penuh 24/7\n"
            "✅ Ruang feedback dan tumbuh bareng coach clipper berpengalaman\n"
            "✅ Review clip setiap hari\n"
            "✅ Akses langsung ke para top earner\n"
            "✅ 2 webinar eksklusif + 2 Live Q&A tiap bulan\n"
            "✅ Akses semua rekaman webinar eksklusif sebelumnya"
            + DIV +
            "📚 **MATERI & TUTORIAL**\n\n"
            "✅ Breakdown clipping lengkap berdurasi 2 jam\n"
            "✅ Roadmap lengkap menuju Rp30 sampai 100 juta per bulan\n"
            "✅ Tutorial editing lengkap untuk membuat format clip viral\n"
            "✅ Setup & warm-up akun yang benar\n"
            "✅ Penjelasan virality, views, dan analytics secara tuntas\n"
            "✅ Metode menjangkau audiens Tier 1\n"
            "✅ Cara mengatasi 0 View Jail di YouTube Shorts\n"
            "✅ Metode teruji untuk berbagai platform\n"
            "✅ Cara membangun theme page yang sukses sebagai aset jangka panjang\n"
            "✅ Cara mengubah clipping menjadi sumber income berkelanjutan\n"
            "✅ Penjelasan lengkap berbagai platform clipping\n"
            "✅ Panduan mendalam untuk masalah umum & spesifik Nusa Elite Clippers\n"
            "✅ Mindset dan ketahanan jangka panjang di dunia clipping"
            + DIV +
            "💸 **PELUANG & INCOME**\n\n"
            "✅ Akses prioritas ke campaign brand eksklusif\n"
            "✅ Update peluang campaign secara rutin\n"
            "✅ Marketplace privat\n"
            "✅ Dealer CC Pro terpercaya"
            + DIV +
            "🎁 **TOOLS & BONUS**\n\n"
            "✅ Paket aset pilihan: preset, SFX, dan overlay\n"
            "🎉 Giveaway eksklusif: proxy, akun, dan bonus lainnya"
            + DIV +
            "💳 **INFORMASI PEMBAYARAN**\n\n"
            "• Klik tombol paket di bawah, lalu isi form singkat.\n"
            "• Bot bakal bikin room pembayaran pribadi buat kamu. Pembayaran via **QRIS**.\n"
            "• Upload bukti bayar di room itu, tim kami verifikasi maksimal 1x24 jam.\n\n"
            "🚀 Pilih paketmu dan mulai perjalanan clipping-mu bersama NusaKlip!\n\n"
            "*Catatan: Penghasilan yang disebutkan dalam roadmap merupakan target pembelajaran, "
            "bukan jaminan pendapatan.*"
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
        "panel_title": "👑 NusaKlip: MENTORING EKSKLUSIF",
        "panel": (
            "💎 **PILIHAN PAKET MENTORING**\n\n"
            "📅 **Mentoring Eksklusif: 1 Bulan**\n"
            "💰 Harga: **Rp389.000**\n"
            "🎯 Cocok buat kamu yang ingin mendapatkan bimbingan intensif dan mulai membangun "
            "progress dengan arahan mentor.\n\n"
            "👑 **Mentoring Eksklusif: 3 Bulan** 🔥 *PALING POPULER*\n"
            "💰 Harga: **Rp829.000**\n"
            "🎯 Cocok buat kamu yang ingin mendapatkan pendampingan lebih panjang, evaluasi "
            "progress, dan arahan berkelanjutan selama 3 bulan."
            + DIV +
            "🏆 **APA YANG KAMU DAPATKAN?**\n\n"
            "✅ Semua benefit Full Clipping Blueprint: komunitas, materi, peluang, dan tools.\n"
            "✅ Pendampingan langsung bersama mentor.\n"
            "✅ Sesi mentoring personal, bukan hanya materi.\n"
            "✅ Target dan evaluasi progress yang disesuaikan dengan perkembanganmu.\n"
            "✅ Prioritas review clip langsung dari mentor.\n"
            "✅ Arahan strategi untuk membantu kamu mengejar hasil pertamamu."
            + DIV +
            "📩 **INGIN BERGABUNG DENGAN MENTORING EKSKLUSIF?**\n\n"
            "Klik tombol di bawah, pilih durasi mentoring yang kamu inginkan, isi form singkat, "
            "lalu lanjut bayar via **QRIS** di room pembayaran pribadimu.\n\n"
            "🔥 Tingkatkan skill, evaluasi progress, dan bangun perjalanan clipping-mu bersama NusaKlip!"
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


def is_staff(member: discord.abc.User) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.manage_guild:
        return True
    return bool(STAFF_ROLE_ID and any(r.id == STAFF_ROLE_ID for r in member.roles))


BULAN = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
         "Agustus", "September", "Oktober", "November", "Desember"]


def fmt_date(dt: datetime) -> str:
    d = dt.astimezone(WIB)
    return f"{d.day} {BULAN[d.month - 1][:3]} {d.year}"


def fmt_date_long(dt: datetime) -> str:
    d = dt.astimezone(WIB)
    return f"{d.day} {BULAN[d.month - 1]} {d.year}"


def chan_ref(channel_id: str, name: str) -> str:
    return f"<#{channel_id}>" if channel_id else f"#{name}"


def staff_mention(guild: discord.Guild) -> str:
    role = guild.get_role(STAFF_ROLE_ID) if STAFF_ROLE_ID else None
    return role.mention if role else "tim NusaKlip"


# ---------------------------------------------------------------------------
# Room state: kept in the room message's title link, e.g.
#   https://nusaklip.online/?nk=1&u=<user id>&p=blueprint&pl=1b&s=pending
# s: pending | verified | active.  v: verified at (unix).  x: expires (unix).
# ---------------------------------------------------------------------------
STATE_KEYS = ("u", "p", "pl", "s", "v", "x")


def state_url(state: dict) -> str:
    q = {"nk": "1", **{k: str(state[k]) for k in STATE_KEYS if state.get(k) not in (None, "")}}
    return STATE_BASE + "?" + urlencode(q)


def parse_state_url(url: str | None) -> dict | None:
    if not url or "nk=1" not in url:
        return None
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    if q.get("k") or "u" not in q or q.get("p") not in PRODUCTS:
        return None  # "k" marks other kinds (konsultasi, perpanjang, member records)
    q.pop("nk", None)
    return q


def topic_text(state: dict) -> str:
    p = PRODUCTS[state["p"]]
    label = p["plans"].get(state.get("pl", "1b"), {}).get("label", "")
    paket = f"{p['name']} {label}".strip()
    status = state.get("s")
    if status == "active":
        until = fmt_date(datetime.fromtimestamp(int(state["x"]), timezone.utc)) if state.get("x") else "-"
        return f"👑 Info mentee (khusus pengumuman) • {paket} aktif sampai {until}"
    if status == "verified":
        until = fmt_date(datetime.fromtimestamp(int(state["x"]), timezone.utc)) if state.get("x") else "-"
        return f"✅ Akses aktif sampai {until} • {paket} • room ketutup otomatis"
    return f"⏳ Menunggu pembayaran • {paket}"


def room_channels(guild: discord.Guild, prefixes=ROOM_PREFIXES) -> list[discord.TextChannel]:
    cats = {c for c in (PURCHASE_CATEGORY_ID, MENTORING_CATEGORY_ID) if c}
    return [
        ch for ch in guild.text_channels
        if ch.category_id in cats and ch.topic and ch.topic.startswith(prefixes)
    ]


async def find_room(ch) -> tuple[discord.Message | None, dict | None]:
    if not isinstance(ch, discord.TextChannel):
        return None, None
    async for msg in ch.history(limit=10, oldest_first=True):
        if msg.author.id == ch.guild.me.id and msg.embeds:
            state = parse_state_url(msg.embeds[0].url)
            if state:
                return msg, state
    return None, None


def room_whop(msg: discord.Message) -> tuple[str, str]:
    for field in msg.embeds[0].fields:
        if "WHOP" in field.name:
            email = re.search(r"Email:\*\* `([^`]*)`", field.value)
            user = re.search(r"Username:\*\* `([^`]*)`", field.value)
            return (email.group(1) if email else "-", user.group(1) if user else "-")
    return "-", "-"


async def save_state(msg: discord.Message, state: dict) -> None:
    e = msg.embeds[0]
    e.url = state_url(state)
    if msg.attachments:  # keep the QRIS attachment showing inside the embed
        e.set_image(url=f"attachment://{msg.attachments[0].filename}")
    await msg.edit(embed=e)


# ---------------------------------------------------------------------------
# Panel (lives in the product channel)
# ---------------------------------------------------------------------------
def panel_embed(product: str) -> discord.Embed:
    p = PRODUCTS[product]
    e = discord.Embed(title=p["panel_title"], description=p["panel"], color=COLOR)
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
        uid = interaction.user.id
        existing = next(
            (
                ch for ch in room_channels(interaction.guild, ("⏳",))
                if any(getattr(t, "id", None) == uid for t in ch.overwrites)
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


class LoggedView(discord.ui.View):
    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        print(f"[purchase] error on {getattr(item, 'custom_id', item)}: {type(error).__name__}: {error}", flush=True)
        traceback.print_exception(type(error), error, error.__traceback__)
        try:
            msg = "Ada error di bot. Coba lagi sebentar, atau tag tim NusaKlip ya."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


class PanelView(LoggedView):
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

        state = {"u": member.id, "p": self.product, "pl": self.plan, "s": "pending"}
        whop_user = self.whop_username.value.strip()
        name = f"{PRODUCTS[self.product]['short']}-{self.plan.replace('b', 'bln')}-{slug(member.name)}"
        try:
            room = await category.create_text_channel(
                name=name,
                overwrites=overwrites,
                topic=topic_text(state),
                reason=f"Buka Akses {self.product} {self.plan} oleh {member}",
            )
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"Gagal bikin room ({type(e).__name__}). Tag tim NusaKlip ya.", ephemeral=True
            )
            return

        embed, file = room_embed(self.product, self.plan, member, state, email, whop_user)
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
def room_embed(product, plan, member, state, email, whop_user):
    p = PRODUCTS[product]
    info = p["plans"][plan]
    price = rupiah(info["price"])
    e = discord.Embed(
        title=f"{p['emoji']} {p['name']}: Paket {info['label']}",
        url=state_url(state),
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
    e.add_field(
        name="💳 Langkah 1: Bayar",
        value=(
            "Scan QRIS di bawah pakai GoPay, OVO, DANA, ShopeePay, atau m-banking apa aja.\n"
            f"Masukin nominal pas **{price}**, terus screenshot bukti bayarnya.\n"
            "Pastiin nama merchant-nya **NUSAKLIP, KOMUNITAS CLIPP**."
        ),
        inline=False,
    )
    e.add_field(
        name="🧾 Langkah 2: Kirim bukti",
        value="Upload screenshot bukti bayar langsung di room ini, terus klik **Udah Bayar, Panggil Staff**.",
        inline=False,
    )
    e.add_field(
        name="📧 Data WHOP lu",
        value=f"• **Email:** `{email.replace('`', '')}`\n"
        f"• **Username:** `{(whop_user or 'belum ada').replace('`', '')}`\n"
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


def welcome_text(member, product: str, plan: str, start: datetime, expires: datetime,
                 in_dm: bool = False) -> str:
    """The thank-you message staff used to send by hand after a purchase."""
    p = PRODUCTS[product]
    period = f"dari {fmt_date_long(start)} sampai {fmt_date_long(expires)}"
    parts = [
        f"Halo kak {member.mention}, makasih udah beli NusaKlip {p['name']}! 🎉",
        "Berikut akses kamu:",
        "🎬 **Full Clipping Blueprint**\n"
        + ("" if product == "mentoring" else f"Akses kamu aktif {period}.\n\n")
        + "Cara aksesnya:\n"
        f"1. Buka Whop NusaKlip: {WHOP_URL}\n"
        "2. Masuk ke bagian Course\n"
        "3. Klik akses ke Full Clipping Blueprint\n"
        "4. Selesai, langsung mulai belajar",
    ]
    if product == "mentoring":
        parts.append(
            "🧑‍🏫 **Mentoring Eksklusif**\n"
            f"Akses mentoring kamu aktif {period}.\n"
            f"Bimbingan langsung ada di channel {chan_ref(BOOKING_CHANNEL_ID, 'booking-1on1')}."
        )
    parts += [
        f"Bisa langsung eksplor channel VIP dari sini dulu ya: {chan_ref(VIP_GUIDE_CHANNEL_ID, 'nusaklip-guide')}",
        f"Kalau ada kendala pas akses, langsung bikin ticket bantuan disini: {chan_ref(HELP_CHANNEL_ID, 'bantuan')}",
        "Selamat belajar, semoga cepet cuan! 🚀",
    ]
    if in_dm:
        parts.append("-# Pesan ini disimpan di DM biar kamu bisa buka lagi kapan aja.")
    else:
        parts.append(
            f"-# Room pembayaran ini otomatis ketutup {CLOSE_AFTER_VERIFY_HOURS:g} jam lagi. "
            "Salinan pesan ini juga dikirim ke DM kamu."
        )
    return "\n\n".join(parts)


async def _room_or_reply(interaction: discord.Interaction):
    msg, state = await find_room(interaction.channel)
    if state is None:
        await interaction.response.send_message(
            "Data room ini nggak kebaca. Tag tim NusaKlip ya.", ephemeral=True
        )
    return msg, state


class RoomView(LoggedView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Udah Bayar, Panggil Staff", emoji="📸",
                       style=discord.ButtonStyle.primary, custom_id="nk:pay:ping")
    async def ping(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg, state = await _room_or_reply(interaction)
        if state is None:
            return
        if str(interaction.user.id) != state["u"] and not is_staff(interaction.user):
            await interaction.response.send_message("Tombol ini buat pemilik room.", ephemeral=True)
            return
        if state.get("s") != "pending":
            await interaction.response.send_message("Akses di room ini udah diverifikasi.", ephemeral=True)
            return
        has_proof = False
        async for m in interaction.channel.history(limit=50):
            if str(m.author.id) == state["u"] and m.attachments:
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
        msg, state = await _room_or_reply(interaction)
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
        state.update({"s": "verified", "v": int(now.timestamp()), "x": int(expires.timestamp())})
        try:
            await save_state(msg, state)
        except discord.HTTPException as e:
            print(f"save_state failed in #{interaction.channel.name}: {e}")

        try:
            await interaction.channel.edit(topic=topic_text(state), reason="Pembayaran diverifikasi")
        except discord.HTTPException:
            pass

        await interaction.followup.send(
            content=welcome_text(member, product, plan, now, expires),
            allowed_mentions=discord.AllowedMentions(users=True),
        )

        # A copy in DM, so the access steps outlive the room (closed after 24h).
        dm_ok = True
        try:
            await member.send(welcome_text(member, product, plan, now, expires, in_dm=True))
        except discord.HTTPException:
            dm_ok = False
            await interaction.channel.send(
                f"⚠️ {member.mention}, DM kamu tertutup jadi salinan pesan di atas nggak bisa dikirim. "
                "Screenshot atau simpan pesan di atas sebelum room ini ketutup ya.",
                allowed_mentions=discord.AllowedMentions(users=True),
            )

        email, whop_user = room_whop(msg)
        import renewal  # lazy: renewal imports this module
        await renewal.on_new_access(guild, member, product, plan, expires, interaction.user, role,
                                    email, whop_user, dm_ok)

    @discord.ui.button(label="Tutup Room", emoji="❌",
                       style=discord.ButtonStyle.secondary, custom_id="nk:pay:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg, state = await _room_or_reply(interaction)
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


async def close_room(ch: discord.TextChannel, reason: str) -> bool:
    """Archive transcript, then delete. Returns False (and keeps the room) if archiving fails."""
    target = ch.guild.get_channel(archive.ARCHIVE_CHANNEL_ID) if archive.ARCHIVE_CHANNEL_ID else None
    if not isinstance(target, discord.TextChannel):
        return False
    try:
        _, state = await find_room(ch)
        data, info = await archive.build_archive(ch, ch.guild.filesize_limit)
        who = f"<@{state['u']}>" if state else "-"
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
            for ch in room_channels(guild, ("⏳", "✅")):
                try:
                    _, state = await find_room(ch)
                    if state is None:
                        continue
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
    for label, cid, name in (("Channel panduan VIP", VIP_GUIDE_CHANNEL_ID, "VIP_GUIDE_CHANNEL_ID"),
                             ("Channel booking mentoring", BOOKING_CHANNEL_ID, "BOOKING_CHANNEL_ID"),
                             ("Channel bantuan", HELP_CHANNEL_ID, "HELP_CHANNEL_ID")):
        ch = g.get_channel(int(cid)) if cid.isdigit() else None
        lines.append(line(label, ch is not None, ch.mention if ch else f"{name} belum diset (pesan sambutan pakai teks biasa)"))
    if MENTORING_CATEGORY_ID:
        mc = g.get_channel(MENTORING_CATEGORY_ID)
        lines.append(line("Kategori room mentoring", isinstance(mc, discord.CategoryChannel),
                          mc.name if mc else "ID salah"))
    pending = len(room_channels(g, ("⏳",)))
    lines.append(f"\nRoom pembayaran yang masih nunggu: **{pending}**")
    await interaction.response.send_message("\n".join(lines), ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------
def setup(bot) -> None:
    bot.tree.add_command(akses)

    # Persistent views must be created inside the running event loop. A View built
    # before bot.run() starts is born "stopped" and silently ignores every click.
    original_hook = bot.setup_hook

    async def setup_hook():
        await original_hook()
        for product in PRODUCTS:
            bot.add_view(PanelView(product))
        bot.add_view(RoomView())

    bot.setup_hook = setup_hook
    loop = make_loop(bot)

    async def _start_loop():
        print(f"[purchase] ready, persistent views: {len(bot.persistent_views)}", flush=True)
        if not loop.is_running():
            loop.start()

    async def _log_interaction(interaction: discord.Interaction):
        data = interaction.data or {}
        cid = data.get("custom_id") or data.get("name")
        if cid and (str(cid).startswith("nk:") or cid == "akses"):
            print(f"[purchase] interaction {interaction.type.name} {cid} from {interaction.user} "
                  f"in #{getattr(interaction.channel, 'name', '?')}", flush=True)

    bot.add_listener(_start_loop, "on_ready")
    bot.add_listener(_log_interaction, "on_interaction")
