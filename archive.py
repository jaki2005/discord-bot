"""
Bulk archive + delete for old closed ticket channels.

  /arsip cek       -> dry run: lists which channels match and whether the bot can read them
  /arsip jalankan  -> after a confirm button: for each matching channel, builds a
                      transcript (.zip with transcript.txt + attachments), posts it to
                      ARCHIVE_CHANNEL_ID, and only then deletes the channel.

A channel is never deleted unless its transcript was uploaded successfully.
"""

import asyncio
import io
import os
import zipfile
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

WIB = ZoneInfo("Asia/Jakarta")

ARCHIVE_CHANNEL_ID = int(os.getenv("ARCHIVE_CHANNEL_ID", "0") or 0) or None
NAME_PREFIXES = [
    p.strip().lower()
    for p in os.getenv("ARCHIVE_NAME_PREFIXES", "closed-,selesai").split(",")
    if p.strip()
]
# Never touched, even if they ever match a prefix.
PROTECTED_NAMES = {"transcripts", "important-dont-delete", "command"}

# Leave headroom under Discord's upload limit for the zip container itself.
SIZE_MARGIN_BYTES = 512 * 1024
SLEEP_BETWEEN_CHANNELS = 1.5

_running = False


def find_candidates(guild: discord.Guild) -> list[discord.TextChannel]:
    out = []
    for ch in guild.text_channels:
        name = ch.name.lower()
        if name in PROTECTED_NAMES:
            continue
        if ARCHIVE_CHANNEL_ID and ch.id == ARCHIVE_CHANNEL_ID:
            continue
        if any(name.startswith(prefix) for prefix in NAME_PREFIXES):
            out.append(ch)
    return out


def can_process(ch: discord.TextChannel) -> bool:
    perms = ch.permissions_for(ch.guild.me)
    return perms.view_channel and perms.read_message_history and perms.manage_channels


def _fmt(dt: datetime) -> str:
    return dt.astimezone(WIB).strftime("%Y-%m-%d %H:%M")


def _embed_text(embed: discord.Embed) -> str:
    parts = []
    if embed.title:
        parts.append(f"[{embed.title}]")
    if embed.description:
        parts.append(embed.description)
    for field in embed.fields:
        parts.append(f"{field.name}: {field.value}")
    return " | ".join(parts)


async def build_archive(ch: discord.TextChannel, size_limit: int) -> tuple[bytes, dict]:
    """Return (zip_bytes, info). Raises on read failure."""
    budget = max(size_limit - SIZE_MARGIN_BYTES, 1024 * 1024)
    body_lines: list[str] = []
    files: list[tuple[str, bytes]] = []
    used = 0
    skipped_files = 0
    count = 0
    first_human = None
    first_ts = last_ts = None

    async for msg in ch.history(limit=None, oldest_first=True):
        count += 1
        first_ts = first_ts or msg.created_at
        last_ts = msg.created_at
        if first_human is None and not msg.author.bot:
            first_human = msg.author

        line = f"[{_fmt(msg.created_at)} WIB] {msg.author} ({msg.author.id})"
        if msg.author.bot:
            line += " [BOT]"
        line += ":"
        if msg.content:
            line += f" {msg.content}"
        for embed in msg.embeds:
            text = _embed_text(embed)
            if text:
                line += f"\n    (embed) {text}"
        for i, att in enumerate(msg.attachments):
            stored_name = f"lampiran/{count:04d}-{i}-{att.filename}"
            if used + att.size <= budget:
                try:
                    data = await att.read()
                    files.append((stored_name, data))
                    used += len(data)
                    line += f"\n    (lampiran) {stored_name}"
                    continue
                except discord.HTTPException:
                    pass
            skipped_files += 1
            line += f"\n    (lampiran tidak disimpan, hanya link, bisa kedaluwarsa) {att.url}"
        body_lines.append(line)

    header = [
        "Transcript tiket NusaKlip",
        f"Channel   : #{ch.name} (id {ch.id})",
        f"Kategori  : {ch.category.name if ch.category else '-'}",
        f"Pembuka   : {first_human} ({first_human.id})" if first_human else "Pembuka   : -",
        f"Periode   : {_fmt(first_ts) if first_ts else '-'} s/d {_fmt(last_ts) if last_ts else '-'} WIB",
        f"Pesan     : {count}",
        f"Diarsip   : {_fmt(datetime.now(timezone.utc))} WIB",
        "=" * 60,
        "",
    ]
    transcript = "\n".join(header + body_lines) + "\n"

    def make_zip(include_files: bool) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("transcript.txt", transcript)
            if include_files:
                for name, data in files:
                    zf.writestr(name, data)
        return buf.getvalue()

    data = make_zip(include_files=True)
    included_files = len(files)
    if len(data) > size_limit and files:
        # Rare: compressed size still too big. Fall back to text only.
        skipped_files += len(files)
        included_files = 0
        data = make_zip(include_files=False)
    if len(data) > size_limit:
        raise ValueError("transcript terlalu besar untuk di-upload")

    info = {
        "count": count,
        "opener": first_human,
        "first": first_ts,
        "last": last_ts,
        "files": included_files,
        "skipped_files": skipped_files,
    }
    return data, info


class ConfirmView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.confirmed: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    @discord.ui.button(label="Ya, arsipkan & hapus", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        await interaction.response.edit_message(content="Oke, mulai mengarsipkan...", view=None)
        self.stop()

    @discord.ui.button(label="Batal", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        await interaction.response.edit_message(content="Dibatalkan, nggak ada yang dihapus.", view=None)
        self.stop()


arsip = app_commands.Group(
    name="arsip",
    description="Arsipkan dan hapus channel tiket lama",
    guild_only=True,
    default_permissions=discord.Permissions(administrator=True),
)


def _archive_channel(guild: discord.Guild) -> discord.TextChannel | None:
    if not ARCHIVE_CHANNEL_ID:
        return None
    ch = guild.get_channel(ARCHIVE_CHANNEL_ID)
    return ch if isinstance(ch, discord.TextChannel) else None


def _name_list(channels: list[discord.TextChannel], limit_chars: int = 1400) -> str:
    out, total = [], 0
    for ch in channels:
        item = f"#{ch.name}"
        if total + len(item) + 2 > limit_chars:
            out.append(f"...dan {len(channels) - len(out)} lainnya")
            break
        out.append(item)
        total += len(item) + 2
    return ", ".join(out) if out else "-"


@arsip.command(name="cek", description="Lihat channel mana yang bakal diarsip (tidak menghapus apa pun)")
async def arsip_cek(interaction: discord.Interaction):
    guild = interaction.guild
    candidates = find_candidates(guild)
    ok = [c for c in candidates if can_process(c)]
    blocked = [c for c in candidates if not can_process(c)]
    target = _archive_channel(guild)

    lines = [
        f"**Pola nama:** {', '.join(p + '*' for p in NAME_PREFIXES)}",
        f"**Cocok:** {len(candidates)} channel",
        f"**Bisa diproses:** {len(ok)}",
        f"**Bot nggak punya akses:** {len(blocked)}",
        f"**Channel arsip:** {target.mention if target else 'BELUM DISET (isi ARCHIVE_CHANNEL_ID)'}",
        "",
        f"Contoh yang bakal diarsip: {_name_list(ok)}",
    ]
    if blocked:
        lines.append(
            f"\nNggak bisa dibaca bot (perlu akses): {_name_list(blocked, 400)}\n"
            "Kasih role bot permission Administrator sementara, lalu cek lagi."
        )
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@arsip.command(name="jalankan", description="Arsipkan lalu hapus semua channel yang cocok (pakai konfirmasi)")
async def arsip_jalankan(interaction: discord.Interaction):
    global _running
    guild = interaction.guild
    target = _archive_channel(guild)
    if target is None:
        await interaction.response.send_message(
            "ARCHIVE_CHANNEL_ID belum diset atau channel-nya nggak ketemu.", ephemeral=True
        )
        return
    tperms = target.permissions_for(guild.me)
    if not (tperms.send_messages and tperms.attach_files):
        await interaction.response.send_message(
            f"Bot nggak bisa kirim file di {target.mention}. Cek permission Send Messages + Attach Files.",
            ephemeral=True,
        )
        return
    if _running:
        await interaction.response.send_message("Proses arsip lain masih jalan, tunggu dulu.", ephemeral=True)
        return

    ok = [c for c in find_candidates(guild) if can_process(c)]
    if not ok:
        await interaction.response.send_message("Nggak ada channel yang bisa diarsip.", ephemeral=True)
        return

    view = ConfirmView(interaction.user.id)
    await interaction.response.send_message(
        f"Bakal mengarsip **{len(ok)} channel** ke {target.mention}, lalu **menghapusnya**. "
        "Penghapusan nggak bisa dibatalkan. Channel yang gagal diarsip nggak akan dihapus.\n\n"
        f"{_name_list(ok)}",
        view=view,
        ephemeral=True,
    )
    await view.wait()
    if not view.confirmed:
        return

    _running = True
    ids = [c.id for c in ok]
    progress = await interaction.channel.send(f"🗂️ Mengarsip 0/{len(ids)} channel...")
    done, failed = 0, []
    try:
        for idx, ch_id in enumerate(ids, start=1):
            ch = guild.get_channel(ch_id)
            if ch is None:
                continue
            name = ch.name
            try:
                data, info = await build_archive(ch, guild.filesize_limit)
                opener = f"{info['opener'].mention}" if info["opener"] else "-"
                period = (
                    f"{_fmt(info['first'])} s/d {_fmt(info['last'])} WIB" if info["first"] else "kosong"
                )
                note = (
                    f" | {info['skipped_files']} lampiran cuma link" if info["skipped_files"] else ""
                )
                await target.send(
                    f"📁 **#{name}** | {info['count']} pesan | pembuka {opener} | {period}{note}",
                    file=discord.File(io.BytesIO(data), filename=f"{name}.zip"),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                await ch.delete(reason=f"Diarsip oleh {interaction.user} via /arsip jalankan")
                done += 1
            except Exception as e:  # keep going; this channel is NOT deleted
                failed.append(f"#{name} ({type(e).__name__})")
            if idx % 5 == 0 or idx == len(ids):
                try:
                    await progress.edit(content=f"🗂️ Mengarsip {idx}/{len(ids)} channel... (berhasil {done})")
                except discord.HTTPException:
                    pass
            await asyncio.sleep(SLEEP_BETWEEN_CHANNELS)
    finally:
        _running = False

    summary = f"✅ Selesai. {done} channel diarsip ke {target.mention} dan dihapus."
    if failed:
        summary += f"\n⚠️ Gagal (nggak dihapus): {', '.join(failed[:30])}"
        if len(failed) > 30:
            summary += f" ...dan {len(failed) - 30} lainnya"
    await progress.edit(content=summary)


def setup(bot: discord.Client) -> None:
    bot.tree.add_command(arsip)
