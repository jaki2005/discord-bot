"""
Masa aktif + Perpanjang (Phase 4).

Membership records live in PURCHASE_LOG_CHANNEL_ID: every verified purchase or renewal is
one embed there, and its title link holds the machine-readable state:
    https://nusaklip.online/?nk=1&k=member&u=<user>&p=<product>&pl=<plan>&x=<expires unix>&s=<status>
    status: active | reminded | grace | expired | renewed | churned
    g: grace-until unix (status grace)   rm: renewal room channel id

Timeline for one record (x = last active day, matching the old "username-nov-17" rooms):
    x - RENEW_REMIND_DAYS     open a private renewal room in RENEW_CATEGORY_ID, DM the member
    day after x, RENEW_REMOVE_HOUR WIB
                              product role removed, ALUMNI_ROLE_ID added, member DM'd
Renewal room (also opened from the panel in RENEW_CHANNEL_ID):
    member: Udah Bayar, Panggil Staff
    staff:  Perpanjang +30 hari / +90 hari, Tambah Waktu 24 Jam, Nggak Lanjut, Tutup Room
Members from the old manual system have no record here, so the bot never touches them.
"""

import io
import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlparse

import discord
from discord import app_commands
from discord.ext import tasks

import purchase as P

ALUMNI_ROLE_ID = P._id("ALUMNI_ROLE_ID")
RENEW_CATEGORY_ID = P._id("RENEW_CATEGORY_ID")
RENEW_CHANNEL_ID = P._id("RENEW_CHANNEL_ID")
REMIND_DAYS = float(os.getenv("RENEW_REMIND_DAYS", "3"))
REMOVE_HOUR = int(os.getenv("RENEW_REMOVE_HOUR", "9"))
GRACE_HOURS = float(os.getenv("RENEW_GRACE_HOURS", "24"))
ROOM_IDLE_HOURS = float(os.getenv("RENEW_ROOM_IDLE_HOURS", "96"))
DONE_CLOSE_HOURS = float(os.getenv("RENEW_DONE_CLOSE_HOURS", "24"))
CHECK_MINUTES = float(os.getenv("RENEW_CHECK_INTERVAL_MINUTES", "30"))

GREEN, GREY = 0x2ECC71, 0x95A5A6
STATUS_TEXT = {
    "active": "🟢 Aktif",
    "reminded": "🟡 Aktif, room perpanjangan udah dibuka",
    "grace": "🟠 Waktu tambahan",
    "expired": "🔴 Berakhir, role dicabut (jadi Alumni)",
    "renewed": "🔁 Diperpanjang (lihat catatan baru)",
    "churned": "⚫ Nggak lanjut",
}
LIVE = ("active", "reminded", "grace")


# ---------------------------------------------------------------------------
# Membership records in #log-pembelian
# ---------------------------------------------------------------------------
def record_url(state: dict) -> str:
    q = {"nk": "1", "k": "member", **{k: str(v) for k, v in state.items() if v not in (None, "")}}
    return P.STATE_BASE + "?" + urlencode(q)


def parse_record(url: str | None) -> dict | None:
    if not url or "k=member" not in url:
        return None
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    if q.get("k") != "member" or "u" not in q or "x" not in q or q.get("p") not in P.PRODUCTS:
        return None
    q.pop("nk", None)
    q.pop("k", None)
    return q


def removal_time(x: int) -> datetime:
    """Role goes at RENEW_REMOVE_HOUR WIB on the day after the last active day."""
    last_day = datetime.fromtimestamp(int(x), timezone.utc).astimezone(P.WIB).date()
    d = last_day + timedelta(days=1)
    return datetime(d.year, d.month, d.day, REMOVE_HOUR, tzinfo=P.WIB)


def end_time(state: dict) -> datetime:
    if state.get("s") == "grace" and state.get("g"):
        return datetime.fromtimestamp(int(state["g"]), timezone.utc)
    return removal_time(int(state["x"]))


def status_line(state: dict) -> str:
    s = state.get("s", "active")
    text = STATUS_TEXT.get(s, s)
    if s == "grace" and state.get("g"):
        text += f" sampai <t:{state['g']}:f>"
    return text


def log_channel(guild: discord.Guild):
    ch = guild.get_channel(P.PURCHASE_LOG_CHANNEL_ID) if P.PURCHASE_LOG_CHANNEL_ID else None
    return ch if isinstance(ch, discord.TextChannel) else None


async def iter_records(guild: discord.Guild):
    ch = log_channel(guild)
    if ch is None:
        return
    async for msg in ch.history(limit=None):
        if msg.author.id == guild.me.id and msg.embeds:
            state = parse_record(msg.embeds[0].url)
            if state:
                yield msg, state


async def latest_record(guild, user_id: int, product: str):
    """Newest record (any status) for this member and product."""
    async for msg, state in iter_records(guild):  # history is newest first
        if state["u"] == str(user_id) and state["p"] == product:
            return msg, state
    return None, None


async def write_record(guild, member, product, plan, expires, staff, role, *, kind="baru",
                       email="-", whop_user="-", dm_ok=True) -> discord.Message | None:
    ch = log_channel(guild)
    if ch is None:
        return None
    p = P.PRODUCTS[product]
    info = p["plans"][plan]
    state = {"u": member.id, "p": product, "pl": plan, "x": int(expires.timestamp()), "s": "active"}
    title = "🧾 Akses dibuka" if kind == "baru" else "🔁 Perpanjangan"
    e = discord.Embed(title=f"{title}: {p['name']} ({info['label']})", url=record_url(state), color=GREEN)
    e.add_field(name="Member", value=f"{member.mention}\n`{member.id}`")
    e.add_field(name="Harga", value=P.rupiah(info["price"]) if kind == "baru" else (info["renewal"] or "-"))
    e.add_field(name="Aktif sampai", value=P.fmt_date(expires))
    e.add_field(name="Email WHOP", value=f"`{email}`")
    e.add_field(name="Username WHOP", value=f"`{whop_user}`")
    e.add_field(name="Role", value=role.mention if role else "-")
    e.add_field(name="DM", value="✅ terkirim" if dm_ok else "⚠️ gagal (DM member tertutup)")
    e.add_field(name="Status", value=status_line(state), inline=False)
    e.set_footer(text=f"Diverifikasi oleh {staff.display_name}")
    try:
        return await ch.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        return None


async def update_record(msg: discord.Message, **changes) -> dict:
    state = parse_record(msg.embeds[0].url) or {}
    for k, v in changes.items():
        if v is None:
            state.pop(k, None)
        else:
            state[k] = str(v)
    e = msg.embeds[0]
    e.url = record_url(state)
    if state.get("x"):
        for i, f in enumerate(e.fields):
            if f.name == "Aktif sampai":
                e.set_field_at(i, name=f.name, value=P.fmt_date(datetime.fromtimestamp(int(state["x"]), timezone.utc)),
                               inline=f.inline)
    for i, f in enumerate(e.fields):
        if f.name == "Status":
            e.set_field_at(i, name="Status", value=status_line(state), inline=False)
            break
    else:
        e.add_field(name="Status", value=status_line(state), inline=False)
    e.colour = GREEN if state.get("s") in LIVE else GREY
    await msg.edit(embed=e)
    return state


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------
def alumni_role(guild):
    return guild.get_role(ALUMNI_ROLE_ID) if ALUMNI_ROLE_ID else None


async def make_active(member: discord.Member, product: str, reason: str) -> discord.Role | None:
    role = member.guild.get_role(P.role_id_for(product) or 0)
    if role and role not in member.roles:
        await member.add_roles(role, reason=reason)
    alum = alumni_role(member.guild)
    if alum and alum in member.roles:
        await member.remove_roles(alum, reason=reason)
    return role


async def make_alumni(member: discord.Member, product: str, reason: str) -> None:
    role = member.guild.get_role(P.role_id_for(product) or 0)
    if role and role in member.roles:
        await member.remove_roles(role, reason=reason)
    alum = alumni_role(member.guild)
    if alum and alum not in member.roles:
        await member.add_roles(alum, reason=reason)


async def on_new_access(guild, member, product, plan, expires, staff, role, email, whop_user, dm_ok):
    """Called by purchase.py after a Buka Akses verification."""
    alum = alumni_role(guild)
    if alum and alum in member.roles:
        try:
            await member.remove_roles(alum, reason="Beli akses baru")
        except discord.HTTPException:
            pass
    await write_record(guild, member, product, plan, expires, staff, role,
                       kind="baru", email=email, whop_user=whop_user, dm_ok=dm_ok)


async def dm(member, text: str) -> bool:
    try:
        await member.send(text)
        return True
    except discord.HTTPException:
        return False


def renew_ref() -> str:
    return P.chan_ref(str(RENEW_CHANNEL_ID or ""), "perpanjang-akses")


# ---------------------------------------------------------------------------
# Renewal rooms
# ---------------------------------------------------------------------------
def room_url(state: dict) -> str:
    q = {"nk": "1", "k": "renew", **{k: str(v) for k, v in state.items() if v not in (None, "")}}
    return P.STATE_BASE + "?" + urlencode(q)


def parse_room(url: str | None) -> dict | None:
    if not url or "k=renew" not in url:
        return None
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    if q.get("k") != "renew" or "u" not in q or q.get("p") not in P.PRODUCTS:
        return None
    q.pop("nk", None)
    q.pop("k", None)
    return q


def renew_rooms(guild) -> list[discord.TextChannel]:
    return [
        ch for ch in guild.text_channels
        if ch.category_id == RENEW_CATEGORY_ID and ch.topic and ch.topic.startswith(("🔄", "✅", "⚫"))
    ]


async def find_room(ch):
    if not isinstance(ch, discord.TextChannel):
        return None, None
    async for msg in ch.history(limit=10, oldest_first=True):
        if msg.author.id == ch.guild.me.id and msg.embeds:
            state = parse_room(msg.embeds[0].url)
            if state:
                return msg, state
    return None, None


async def save_room(msg, state):
    e = msg.embeds[0]
    e.url = room_url(state)
    if msg.attachments:
        e.set_image(url=f"attachment://{msg.attachments[0].filename}")
    await msg.edit(embed=e)


def open_room_for(guild, user_id: int) -> discord.TextChannel | None:
    for ch in renew_rooms(guild):
        if ch.topic.startswith("🔄") and any(getattr(t, "id", None) == user_id for t in ch.overwrites):
            return ch
    return None


EMAIL_RE = r"[^@\s]+@[^@\s]+\.[^@\s]+"
WHOP_FIELD = "📧 Data WHOP kamu"


def whop_value(email: str | None, whop_user: str | None) -> str:
    if not email:
        return "Belum diisi. Klik **Isi Data WHOP** dulu ya, dipakai tim buat buka lagi akses materi kamu."
    return (f"• **Email:** `{email.replace('`', '')}`\n"
            f"• **Username:** `{(whop_user or 'belum ada').replace('`', '')}`")


class WhopForm(discord.ui.Modal, title="Data WHOP buat perpanjangan"):
    email = discord.ui.TextInput(label="Email WHOP / email terdaftar", max_length=100,
                                 placeholder="nama@email.com (dipakai buat buka lagi akses materi)")
    whop_username = discord.ui.TextInput(label="Username WHOP (kosongin kalau belum ada)", required=False,
                                         max_length=50, placeholder="Username WHOP kamu kalau ada")

    def __init__(self, product: str | None = None):
        super().__init__()
        self.product = product  # set when opened from the panel: create the room after submit

    async def on_submit(self, interaction: discord.Interaction):
        email = self.email.value.strip()
        if not re.fullmatch(EMAIL_RE, email):
            await interaction.response.send_message("Format email-nya kayaknya salah. Coba lagi ya.", ephemeral=True)
            return
        user = self.whop_username.value.strip()
        if self.product:
            await _open_from_panel(interaction, self.product, email, user)
            return
        msg, state = await find_room(interaction.channel)
        if state is None:
            await interaction.response.send_message("Data room ini nggak kebaca. Tag tim NusaKlip ya.", ephemeral=True)
            return
        e = msg.embeds[0]
        for i, f in enumerate(e.fields):
            if f.name == WHOP_FIELD:
                e.set_field_at(i, name=WHOP_FIELD, value=whop_value(email, user), inline=False)
        if msg.attachments:
            e.set_image(url=f"attachment://{msg.attachments[0].filename}")
        await msg.edit(embed=e)
        await interaction.response.send_message("✅ Data WHOP kamu udah disimpan di room ini.", ephemeral=True)


def room_has_whop(msg) -> bool:
    email, _ = P.room_whop(msg)
    return email not in ("", "-")


async def open_renew_room(guild, member, product, record_msg, record_state,
                          email: str | None = None, whop_user: str | None = None) -> discord.TextChannel | None:
    category = guild.get_channel(RENEW_CATEGORY_ID) if RENEW_CATEGORY_ID else None
    if not isinstance(category, discord.CategoryChannel):
        return None
    p = P.PRODUCTS[product]
    staff_role = guild.get_role(P.STAFF_ROLE_ID) if P.STAFF_ROLE_ID else None
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True,
                                            read_message_history=True, embed_links=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True,
                                              embed_links=True, read_message_history=True,
                                              manage_channels=True, manage_messages=True),
    }
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                             attach_files=True, read_message_history=True)
    until = ""
    if record_state:
        until = P.fmt_date(datetime.fromtimestamp(int(record_state["x"]), timezone.utc))
    ch = await category.create_text_channel(
        name=f"perpanjang-{P.slug(member.name)}",
        overwrites=overwrites,
        topic=f"🔄 Perpanjang {p['name']}" + (f" • aktif sampai {until}" if until else ""),
        reason=f"Room perpanjangan {product} untuk {member}",
    )
    state = {"u": member.id, "p": product, "s": "open", "src": record_msg.id if record_msg else ""}
    e = discord.Embed(title=f"🔄 Perpanjang {p['name']}", url=room_url(state), color=P.COLOR)
    if record_state and record_state.get("s") in LIVE:
        e.description = (f"Hai {member.mention}, akses **{p['name']}** kamu aktif sampai **{until}**. "
                         "Biar nggak keputus, perpanjang lewat room ini ya.")
    elif record_state:
        e.description = (f"Hai {member.mention}, akses **{p['name']}** kamu udah berakhir ({until}). "
                         "Mau lanjut lagi? Perpanjang lewat room ini ya.")
    else:
        e.description = f"Hai {member.mention}, mau perpanjang **{p['name']}**? Lewat room ini ya."
    e.add_field(name="💰 Harga perpanjangan", value=renew_price_lines(product), inline=False)
    e.add_field(
        name="💳 Cara perpanjang",
        value=("1. Scan QRIS di bawah, masukin nominal sesuai paket yang kamu pilih\n"
               "2. Upload screenshot bukti bayar di room ini, tulis paketnya (1 Bulan / 3 Bulan)\n"
               "3. Pastiin data WHOP kamu udah diisi (tombol **Isi Data WHOP**)\n"
               "4. Klik **Udah Bayar, Panggil Staff**\n"
               "Masa aktif baru dihitung dari tanggal habis yang lama, jadi nggak ada hari yang hilang."),
        inline=False,
    )
    e.add_field(name=WHOP_FIELD, value=whop_value(email, whop_user), inline=False)
    e.add_field(
        name="⏳ Kalau nggak diperpanjang",
        value=(f"Setelah masa aktif habis, role {p['name']} dicabut dan kamu jadi **NusaKlip Alumni**. "
               f"Kamu tetap bisa perpanjang kapan aja di {renew_ref()}."),
        inline=False,
    )
    e.set_footer(text="NusaKlip • Pembayaran via QRIS • Verifikasi maksimal 1x24 jam")
    file = None
    if P.QRIS_FILE.exists():
        file = discord.File(P.QRIS_FILE, filename="qris.png")
        e.set_image(url="attachment://qris.png")
    elif P.QRIS_IMAGE_URL:
        e.set_image(url=P.QRIS_IMAGE_URL)
    kwargs = {"file": file} if file else {}
    await ch.send(content=member.mention, embed=e, view=RenewView(),
                  allowed_mentions=discord.AllowedMentions(users=True), **kwargs)
    return ch


async def _room_or_reply(interaction):
    msg, state = await find_room(interaction.channel)
    if state is None:
        await interaction.response.send_message("Data room ini nggak kebaca. Tag tim NusaKlip ya.", ephemeral=True)
    return msg, state


async def _staff_or_reply(interaction) -> bool:
    if P.is_staff(interaction.user):
        return True
    await interaction.response.send_message("Tombol ini cuma buat staff.", ephemeral=True)
    return False


async def _record_for_room(guild, state):
    log = log_channel(guild)
    if log and state.get("src"):
        try:
            msg = await log.fetch_message(int(state["src"]))
            return msg, parse_record(msg.embeds[0].url)
        except (discord.HTTPException, ValueError, IndexError):
            pass
    return await latest_record(guild, int(state["u"]), state["p"])


async def _finish_room(interaction, room_msg, room_state, topic):
    room_state.update({"s": "done", "v": int(time.time())})
    try:
        await save_room(room_msg, room_state)
        await interaction.channel.edit(topic=topic)
    except discord.HTTPException:
        pass


def renewed_text(member, product: str, expires: datetime, email: str) -> str:
    p = P.PRODUCTS[product]
    parts = [
        f"✅ Perpanjangan berhasil! Akses **{p['name']}** {member.mention} sekarang aktif sampai "
        f"**{P.fmt_date_long(expires)}**.",
        "🎬 **Akses materi di WHOP**\n"
        "Kalau kamu sempat ke-kick dari WHOP karena masa aktif sebelumnya habis, join lagi ya:\n"
        f"1. Buka Whop NusaKlip: {P.WHOP_URL}\n"
        "2. Masuk ke bagian Course, terus klik Full Clipping Blueprint dan isi nama discord kamu",
    ]
    if product == "mentoring":
        parts.append(f"🧑‍🏫 Bimbingan mentoring tetap di {P.chan_ref(P.BOOKING_CHANNEL_ID, 'booking-1on1')}.")
    parts.append(f"Kalau ada kendala akses, langsung bikin ticket di {P.chan_ref(P.HELP_CHANNEL_ID, 'bantuan')}.")
    parts.append("Makasih udah lanjut bareng NusaKlip, semoga makin cuan! 🚀")
    return "\n\n".join(parts)


class RenewView(P.LoggedView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Udah Bayar, Panggil Staff", emoji="📸", style=discord.ButtonStyle.primary,
                       custom_id="nk:renew:ping", row=0)
    async def ping(self, interaction, button):
        msg, state = await _room_or_reply(interaction)
        if state is None:
            return
        if str(interaction.user.id) != state["u"] and not P.is_staff(interaction.user):
            await interaction.response.send_message("Tombol ini buat pemilik room.", ephemeral=True)
            return
        has_proof = False
        async for m in interaction.channel.history(limit=50):
            if str(m.author.id) == state["u"] and m.attachments:
                has_proof = True
                break
        if not has_proof:
            await interaction.response.send_message(
                "Upload dulu screenshot bukti bayar kamu di room ini, baru klik tombol ini lagi ya.", ephemeral=True)
            return
        if not room_has_whop(msg):
            await interaction.response.send_message(
                "Isi dulu data WHOP kamu lewat tombol **Isi Data WHOP**, baru panggil staff ya.", ephemeral=True)
            return
        now = time.time()
        if now - P._last_ping.get(interaction.channel.id, 0) < P.PING_COOLDOWN_SECONDS:
            await interaction.response.send_message("Staff udah dipanggil barusan, tunggu sebentar ya.", ephemeral=True)
            return
        P._last_ping[interaction.channel.id] = now
        await interaction.response.send_message(
            f"🔔 {P.staff_mention(interaction.guild)}, <@{state['u']}> udah kirim bukti bayar perpanjangan. Tolong dicek ya.",
            allowed_mentions=discord.AllowedMentions(roles=True, users=False),
        )

    @discord.ui.button(label="Isi Data WHOP", emoji="📧", style=discord.ButtonStyle.secondary,
                       custom_id="nk:renew:whop", row=0)
    async def whop(self, interaction, button):
        msg, state = await _room_or_reply(interaction)
        if state is None:
            return
        if str(interaction.user.id) != state["u"] and not P.is_staff(interaction.user):
            await interaction.response.send_message("Tombol ini buat pemilik room.", ephemeral=True)
            return
        await interaction.response.send_modal(WhopForm())

    async def _renew(self, interaction, days: int):
        msg, state = await _room_or_reply(interaction)
        if state is None or not await _staff_or_reply(interaction):
            return
        if state.get("s") == "done":
            await interaction.response.send_message("Room ini udah selesai diproses.", ephemeral=True)
            return
        guild = interaction.guild
        member = guild.get_member(int(state["u"]))
        if member is None:
            await interaction.response.send_message("Member ini udah nggak ada di server.", ephemeral=True)
            return
        await interaction.response.defer()
        product = state["p"]
        plan = "1b" if days == 30 else "3b"
        rec_msg, rec = await _record_for_room(guild, state)
        now = datetime.now(timezone.utc)
        base = now
        if rec and rec.get("s") in LIVE:
            base = max(now, datetime.fromtimestamp(int(rec["x"]), timezone.utc))
        expires = base + timedelta(days=days)
        try:
            role = await make_active(member, product, f"Perpanjang, diverifikasi {interaction.user}")
        except discord.Forbidden:
            await interaction.followup.send("Bot nggak bisa ubah role. Pastiin role NusaKlip AI ada di atas role "
                                            "Elite, VIP, dan Alumni, dan punya Manage Roles.")
            return
        if rec_msg and rec and rec.get("s") in LIVE + ("expired", "churned"):
            try:
                await update_record(rec_msg, s="renewed", g=None)
            except discord.HTTPException:
                pass
        email, whop_user = P.room_whop(msg)
        text = renewed_text(member, product, expires, email)
        await interaction.followup.send(text + f"\n\n-# Room ini otomatis ketutup {DONE_CLOSE_HOURS:g} jam lagi. "
                                        "Salinan pesan ini juga dikirim ke DM kamu.",
                                        allowed_mentions=discord.AllowedMentions(users=True))
        dm_ok = await dm(member, text)
        if not dm_ok:
            await interaction.channel.send(
                f"⚠️ {member.mention}, DM kamu tertutup jadi salinan pesan di atas nggak bisa dikirim. "
                "Screenshot atau simpan pesan di atas sebelum room ini ketutup ya.",
                allowed_mentions=discord.AllowedMentions(users=True))
        await write_record(guild, member, product, plan, expires, interaction.user, role, kind="perpanjang",
                           email=email, whop_user=whop_user, dm_ok=dm_ok)
        await _finish_room(interaction, msg, state,
                           f"✅ Diperpanjang sampai {P.fmt_date(expires)} • room ketutup otomatis")

    @discord.ui.button(label="Perpanjang +30 hari", emoji="🔁", style=discord.ButtonStyle.success,
                       custom_id="nk:renew:30", row=1)
    async def renew30(self, interaction, button):
        await self._renew(interaction, 30)

    @discord.ui.button(label="Perpanjang +90 hari", emoji="🔁", style=discord.ButtonStyle.success,
                       custom_id="nk:renew:90", row=1)
    async def renew90(self, interaction, button):
        await self._renew(interaction, 90)

    @discord.ui.button(label=f"Tambah Waktu {GRACE_HOURS:g} Jam", emoji="⏳", style=discord.ButtonStyle.secondary,
                       custom_id="nk:renew:grace", row=1)
    async def grace(self, interaction, button):
        msg, state = await _room_or_reply(interaction)
        if state is None or not await _staff_or_reply(interaction):
            return
        guild = interaction.guild
        rec_msg, rec = await _record_for_room(guild, state)
        member = guild.get_member(int(state["u"]))
        if rec_msg is None or member is None:
            await interaction.response.send_message("Catatan masa aktif member ini nggak ketemu di #log-pembelian.",
                                                    ephemeral=True)
            return
        await interaction.response.defer()
        until = datetime.now(timezone.utc) + timedelta(hours=GRACE_HOURS)
        try:
            await make_active(member, state["p"], f"Tambah waktu oleh {interaction.user}")
        except discord.Forbidden:
            await interaction.followup.send("Bot nggak bisa ubah role. Cek urutan role NusaKlip AI.")
            return
        await update_record(rec_msg, s="grace", g=int(until.timestamp()), rm=interaction.channel.id)
        await interaction.followup.send(
            f"⏳ {member.mention}, akses kamu diperpanjang sementara sampai <t:{int(until.timestamp())}:f>. "
            "Selesaikan pembayaran perpanjangan sebelum itu ya.",
            allowed_mentions=discord.AllowedMentions(users=True),
        )

    @discord.ui.button(label="Nggak Lanjut", emoji="⚫", style=discord.ButtonStyle.danger,
                       custom_id="nk:renew:churn", row=1)
    async def churn(self, interaction, button):
        msg, state = await _room_or_reply(interaction)
        if state is None or not await _staff_or_reply(interaction):
            return
        await interaction.response.send_message(
            "Yakin member ini nggak lanjut? Role-nya dicabut sekarang dan dia jadi NusaKlip Alumni.",
            view=ConfirmChurn(interaction.user.id), ephemeral=True,
        )

    @discord.ui.button(label="Tutup Room", emoji="❌", style=discord.ButtonStyle.secondary,
                       custom_id="nk:renew:close", row=2)
    async def close(self, interaction, button):
        msg, state = await _room_or_reply(interaction)
        if state is None:
            return
        if not (P.is_staff(interaction.user) or str(interaction.user.id) == state["u"]):
            await interaction.response.send_message("Kamu nggak bisa nutup room ini.", ephemeral=True)
            return
        await interaction.response.send_message("Oke, room ditutup. Transkripnya disimpan.")
        await P.close_room(interaction.channel, f"ditutup oleh {interaction.user}")


class ConfirmChurn(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=60)
        self.user_id = user_id

    @discord.ui.button(label="Ya, nggak lanjut", style=discord.ButtonStyle.danger)
    async def yes(self, interaction, button):
        if interaction.user.id != self.user_id:
            return
        await interaction.response.edit_message(content="Memproses...", view=None)
        guild = interaction.guild
        msg, state = await find_room(interaction.channel)
        if state is None:
            return
        member = guild.get_member(int(state["u"]))
        product = state["p"]
        if member:
            try:
                await make_alumni(member, product, f"Nggak lanjut, dikonfirmasi {interaction.user}")
            except discord.Forbidden:
                await interaction.followup.send("Bot nggak bisa ubah role. Cek urutan role NusaKlip AI.", ephemeral=True)
                return
        rec_msg, rec = await _record_for_room(guild, state)
        if rec_msg:
            await update_record(rec_msg, s="churned", g=None)
        p = P.PRODUCTS[product]
        await interaction.channel.send(
            f"Oke, perpanjangan **{p['name']}** nggak dilanjut. Role-nya udah dicabut dan "
            f"<@{state['u']}> sekarang jadi NusaKlip Alumni. Kalau mau balik, perpanjang aja kapan pun di "
            f"{renew_ref()} ya. Makasih udah belajar bareng NusaKlip! 🙏",
            allowed_mentions=discord.AllowedMentions(users=True),
        )
        await _finish_room(interaction, msg, state, "⚫ Nggak lanjut • room ketutup otomatis")


# ---------------------------------------------------------------------------
# Panel in #perpanjang-akses
# ---------------------------------------------------------------------------
RETAINED = (
    "🌐 Komunitas eksklusif & support penuh\n"
    "🎬 Semua materi Full Clipping Blueprint\n"
    "📋 Review clip rutin oleh coach\n"
    "🤝 Akses langsung ke para top earner\n"
    "💎 2 webinar + 2 Live Q&A per bulan, plus semua rekamannya\n"
    "💼 Akses prioritas & update campaign eksklusif\n"
    "🎒 Asset packs & giveaway eksklusif"
)


def renew_price_lines(product: str) -> str:
    lines = []
    for info in P.PRODUCTS[product]["plans"].values():
        line = f"• {info['label']}: **{P.rupiah(info['renew_price'])}**"
        if info.get("renew_was"):
            line += f" ~~{P.rupiah(info['renew_was'])}~~"
        lines.append(line)
    return "\n".join(lines)


def panel_embed() -> discord.Embed:
    e = discord.Embed(
        title="🔄 Perpanjang Akses",
        description=(
            "Masa aktif kamu hampir berakhir atau udah habis? Jangan sampai kehilangan momentum, "
            "perpanjang sekarang."
        ),
        color=P.COLOR,
    )
    e.add_field(name="⭐ Yang kamu pertahankan", value="\n".join("• " + x for x in RETAINED.split("\n")),
                inline=False)
    e.add_field(
        name="💰 Harga perpanjangan",
        value=(f"🎬 **Full Clipping Blueprint**\n{renew_price_lines('blueprint')}\n\n"
               f"👑 **Mentoring Eksklusif**\n{renew_price_lines('mentoring')}"),
        inline=False,
    )
    e.add_field(
        name="📝 Cara perpanjang",
        value=("Klik tombol produk kamu di bawah. Bot bakal bukain room perpanjangan pribadi, "
               "lengkap sama QRIS-nya. Masa aktif baru dihitung dari tanggal habis yang lama, "
               "jadi nggak ada hari yang hilang."),
        inline=False,
    )
    e.set_footer(text="NusaKlip • Pembayaran via QRIS")
    return e


async def _open_from_panel(interaction, product, email, whop_user):
    guild, member = interaction.guild, interaction.user
    await interaction.response.defer(ephemeral=True, thinking=True)
    rec_msg, rec = await latest_record(guild, member.id, product)
    try:
        ch = await open_renew_room(guild, member, product, rec_msg, rec, email, whop_user)
    except discord.HTTPException as e:
        ch = None
        print(f"[renewal] could not open room: {e}", flush=True)
    if ch is None:
        await interaction.followup.send("Gagal bikin room perpanjangan. Tag tim NusaKlip ya.", ephemeral=True)
        return
    if rec_msg and rec and rec.get("s") in ("active", "grace"):
        await update_record(rec_msg, rm=ch.id)
    await interaction.followup.send(f"✅ Room perpanjangan kamu: {ch.mention}", ephemeral=True)


class RenewPanel(P.LoggedView):
    def __init__(self):
        super().__init__(timeout=None)

    async def _open(self, interaction, product):
        existing = open_room_for(interaction.guild, interaction.user.id)
        if existing:
            await interaction.response.send_message(f"Kamu masih punya room perpanjangan: {existing.mention}",
                                                    ephemeral=True)
            return
        await interaction.response.send_modal(WhopForm(product))

    @discord.ui.button(label="Perpanjang Blueprint", emoji="📘", style=discord.ButtonStyle.primary,
                       custom_id="nk:renew:open:blueprint")
    async def bp(self, interaction, button):
        await self._open(interaction, "blueprint")

    @discord.ui.button(label="Perpanjang Mentoring", emoji="👑", style=discord.ButtonStyle.primary,
                       custom_id="nk:renew:open:mentoring")
    async def mt(self, interaction, button):
        await self._open(interaction, "mentoring")


# ---------------------------------------------------------------------------
# Background loop: reminders, expiry, room cleanup
# ---------------------------------------------------------------------------
async def process_records(guild, now: datetime):
    latest: dict[tuple[str, str], int] = {}
    records = []
    async for msg, state in iter_records(guild):
        records.append((msg, state))
        key = (state["u"], state["p"])
        latest[key] = max(latest.get(key, 0), int(state["x"]))

    for msg, state in records:
        s = state.get("s", "active")
        if s not in LIVE:
            continue
        key = (state["u"], state["p"])
        if int(state["x"]) < latest[key] and s != "grace":
            await update_record(msg, s="renewed")  # a newer purchase covers this one
            continue
        member = guild.get_member(int(state["u"]))
        x = datetime.fromtimestamp(int(state["x"]), timezone.utc)
        product = state["p"]
        p = P.PRODUCTS[product]

        if now >= end_time(state):
            if member:
                try:
                    await make_alumni(member, product, "Masa aktif habis")
                except discord.HTTPException as e:
                    print(f"[renewal] could not change roles for {member}: {e}", flush=True)
                    continue
                text = (f"Halo kak {member.mention}, akses **{p['name']}** kamu udah berakhir "
                        f"(aktif sampai {P.fmt_date_long(x)}). Sekarang kamu jadi **NusaKlip Alumni**.\n\n"
                        f"Kalau mau lanjut belajar bareng lagi, perpanjang aja di {renew_ref()} ya. Makasih banyak! 🙏")
                await dm(member, text)
                room = guild.get_channel(int(state["rm"])) if state.get("rm") else None
                if isinstance(room, discord.TextChannel):
                    try:
                        await room.send(text, allowed_mentions=discord.AllowedMentions(users=True))
                    except discord.HTTPException:
                        pass
            await update_record(msg, s="expired", g=None)
            print(f"[renewal] expired {state['u']} {product}", flush=True)

        elif s == "active" and now >= x - timedelta(days=REMIND_DAYS) and member:
            room = open_room_for(guild, member.id)
            if room is None:
                try:
                    room = await open_renew_room(guild, member, product, msg, state)
                except discord.HTTPException as e:
                    print(f"[renewal] could not open room for {member}: {e}", flush=True)
            await update_record(msg, s="reminded", rm=room.id if room else None)
            where = room.mention if room else renew_ref()
            await dm(member, f"Halo kak! Akses **{p['name']}** kamu aktif sampai **{P.fmt_date_long(x)}**. "
                             f"Biar nggak keputus, perpanjang lewat sini ya: {where}")


async def _last_human(ch) -> datetime:
    async for msg in ch.history(limit=50):
        if not msg.author.bot:
            return msg.created_at
    return ch.created_at


async def cleanup_rooms(guild, now):
    for ch in renew_rooms(guild):
        msg, state = await find_room(ch)
        if state is None:
            continue
        if state.get("s") == "done":
            if state.get("v") and now - datetime.fromtimestamp(int(state["v"]), timezone.utc) >= timedelta(hours=DONE_CLOSE_HOURS):
                await P.close_room(ch, "otomatis, perpanjangan selesai diproses")
        elif now - await _last_human(ch) >= timedelta(hours=ROOM_IDLE_HOURS):
            await P.close_room(ch, f"otomatis, {ROOM_IDLE_HOURS:g} jam tidak aktif")


def make_loop(bot):
    @tasks.loop(minutes=CHECK_MINUTES)
    async def renewal_loop():
        now = datetime.now(timezone.utc)
        for guild in bot.guilds:
            for step in (process_records, cleanup_rooms):
                try:
                    await step(guild, now)
                except Exception as e:
                    print(f"[renewal] {step.__name__} error: {type(e).__name__}: {e}", flush=True)

    @renewal_loop.before_loop
    async def _wait():
        await bot.wait_until_ready()

    return renewal_loop


# ---------------------------------------------------------------------------
# Commands + wiring
# ---------------------------------------------------------------------------
perpanjang = app_commands.Group(
    name="perpanjang", description="Masa aktif dan perpanjangan member",
    guild_only=True, default_permissions=discord.Permissions(manage_guild=True),
)


@perpanjang.command(name="panel", description="Kirim panel Perpanjang Akses ke sebuah channel")
@app_commands.describe(channel="Kosongin buat channel ini")
async def perpanjang_panel(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    target = channel or interaction.channel
    try:
        await target.send(embed=panel_embed(), view=RenewPanel())
    except discord.Forbidden:
        await interaction.response.send_message(f"Bot nggak bisa kirim pesan di {target.mention}.", ephemeral=True)
        return
    await interaction.response.send_message(f"Panel Perpanjang Akses udah dipasang di {target.mention}.", ephemeral=True)


@perpanjang.command(name="daftar", description="Lihat member aktif dan kapan aksesnya habis")
async def perpanjang_daftar(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    g = interaction.guild
    rows = []
    async for msg, st in iter_records(g):
        if st.get("s") in LIVE:
            rows.append((int(st["x"]), st))
    rows.sort(key=lambda r: r[0])
    alum = alumni_role(g)
    cat = g.get_channel(RENEW_CATEGORY_ID) if RENEW_CATEGORY_ID else None
    head = [
        f"{'✅' if alum else '❌'} Role Alumni: {alum.mention if alum else 'ALUMNI_ROLE_ID belum diset'}",
        f"{'✅' if isinstance(cat, discord.CategoryChannel) else '❌'} Kategori perpanjang: "
        f"{cat.name if cat else 'RENEW_CATEGORY_ID belum diset'}",
        f"Role dicabut jam {REMOVE_HOUR:02d}.00 WIB sehari setelah tanggal terakhir. Reminder H-{REMIND_DAYS:g}.",
        "",
        f"**Member aktif ({len(rows)}):**",
    ]
    lines = []
    for x, st in rows[:40]:
        p = P.PRODUCTS[st["p"]]
        lines.append(f"<@{st['u']}> • {p['name']} • sampai {P.fmt_date(datetime.fromtimestamp(x, timezone.utc))} • "
                     f"{STATUS_TEXT.get(st.get('s'), st.get('s'))}")
    if len(rows) > 40:
        lines.append(f"...dan {len(rows) - 40} lainnya")
    await interaction.followup.send("\n".join(head + (lines or ["-"]))[:1990], ephemeral=True,
                                    allowed_mentions=discord.AllowedMentions.none())


def setup(bot) -> None:
    bot.tree.add_command(perpanjang)
    original_hook = bot.setup_hook

    async def setup_hook():
        await original_hook()
        bot.add_view(RenewView())
        bot.add_view(RenewPanel())

    bot.setup_hook = setup_hook
    loop = make_loop(bot)

    async def _start():
        if not loop.is_running():
            loop.start()

    bot.add_listener(_start, "on_ready")
