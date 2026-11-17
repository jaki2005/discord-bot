"""
Honeypot channel: anyone who posts in HONEYPOT_CHANNEL_ID is removed automatically.

Compromised accounts usually blast phishing links or scam files into every channel. A
channel that real members are told never to use catches them on the first message.

  HONEYPOT_ACTION
    softban (default)  ban + immediate unban: kicks the account AND deletes its messages
                       from the last HONEYPOT_DELETE_HOURS in every channel. The real
                       owner can rejoin after securing the account.
    ban                permanent ban, messages deleted the same way
    kick               kick only, messages stay

  Staff (Manage Server or STAFF_ROLE_ID) and bots are never punished; their message is
  just deleted. Every action is logged to HONEYPOT_LOG_CHANNEL_ID (or ARCHIVE_CHANNEL_ID).

  /honeypot pasang   posts the warning embed in the honeypot channel.
  /honeypot cek      shows the setup and whether the bot has the permissions it needs.
"""

import os
import time

import discord
from discord import app_commands


def _id(name: str) -> int | None:
    return int(os.getenv(name, "0") or 0) or None


CHANNEL_ID = _id("HONEYPOT_CHANNEL_ID")
LOG_CHANNEL_ID = _id("HONEYPOT_LOG_CHANNEL_ID") or _id("ARCHIVE_CHANNEL_ID")
STAFF_ROLE_ID = _id("STAFF_ROLE_ID")
ACTION = os.getenv("HONEYPOT_ACTION", "softban").strip().lower()
DELETE_SECONDS = int(min(float(os.getenv("HONEYPOT_DELETE_HOURS", "24")), 168) * 3600)

_recent: dict[int, float] = {}  # user id -> time handled, so a burst of messages acts once


def _is_staff(member: discord.Member) -> bool:
    if member.guild_permissions.manage_guild or member.guild_permissions.administrator:
        return True
    return bool(STAFF_ROLE_ID and any(r.id == STAFF_ROLE_ID for r in member.roles))


def warning_embed() -> discord.Embed:
    e = discord.Embed(
        title="⚠️ Room Jebakan: Jangan Chat di Sini",
        description=(
            "**Kirim pesan apa pun di room ini = otomatis dikeluarkan dari server.**\n\n"
            "Room ini bagian dari sistem keamanan NusaKlip, buat nangkep akun yang dibajak "
            "lalu nyebar link phishing atau file scam ke semua room.\n\n"
            "Tindakannya langsung dijalankan bot, tanpa dicek staff dulu. Semua pesan dari akun "
            "itu di room lain juga ikut dihapus.\n\n"
            "Kalau akun kamu sempat dibajak dan kekeluar: amankan dulu akunnya (ganti password, "
            "nyalain 2FA, hapus aplikasi yang nggak dikenal di Settings → Authorized Apps), "
            "baru join lagi.\n\n"
            "**Jangan chat di sini dengan alasan apa pun.**"
        ),
        color=0xE74C3C,
    )
    e.set_footer(text="NusaKlip • Sistem keamanan otomatis")
    return e


async def _log(guild: discord.Guild, text: str) -> None:
    ch = guild.get_channel(LOG_CHANNEL_ID) if LOG_CHANNEL_ID else None
    if isinstance(ch, discord.TextChannel):
        try:
            await ch.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass


async def on_message(message: discord.Message) -> None:
    if not CHANNEL_ID or message.channel.id != CHANNEL_ID or message.guild is None:
        return
    author = message.author
    try:
        await message.delete()
    except discord.HTTPException:
        pass
    if author.bot or not isinstance(author, discord.Member) or _is_staff(author):
        return

    now = time.time()
    if now - _recent.get(author.id, 0) < 60:
        return
    _recent[author.id] = now

    guild = message.guild
    preview = (message.content or "").replace("`", "'")[:300] or "(tanpa teks)"
    if message.attachments:
        preview += f" + {len(message.attachments)} file"

    try:
        await author.send(
            f"Akun kamu dikeluarkan otomatis dari server **{guild.name}** karena ngirim pesan di "
            "room keamanan yang nggak boleh dipakai chat.\n\n"
            "Biasanya ini terjadi karena akunnya dibajak dan dipakai nyebar spam. Amankan dulu akun "
            "kamu: ganti password, nyalain 2FA, dan hapus aplikasi yang nggak kamu kenal di "
            "User Settings → Authorized Apps. Setelah itu kamu boleh join lagi."
            if ACTION != "ban" else
            f"Akun kamu dibanned otomatis dari server **{guild.name}** karena ngirim pesan di room "
            "keamanan. Kalau akun kamu sempat dibajak, amankan dulu lalu hubungi tim NusaKlip."
        )
    except discord.HTTPException:
        pass

    reason = "Honeypot: kirim pesan di room keamanan (kemungkinan akun dibajak)"
    try:
        if ACTION == "kick":
            await author.kick(reason=reason)
        else:
            await guild.ban(author, reason=reason, delete_message_seconds=DELETE_SECONDS)
            if ACTION != "ban":
                await guild.unban(author, reason="Honeypot softban: cuma kick + hapus pesan")
        done = {"kick": "dikick", "ban": "dibanned permanen"}.get(ACTION, "dikick + pesannya dihapus")
        await _log(guild, f"🛡️ **Honeypot** • {author.mention} (`{author}` / `{author.id}`) {done}.\n"
                          f"Isi pesan: `{preview}`")
        print(f"[honeypot] {ACTION} {author} ({author.id})", flush=True)
    except discord.Forbidden:
        await _log(guild, f"⚠️ **Honeypot** • {author.mention} (`{author.id}`) chat di room jebakan tapi bot "
                          "**nggak bisa** ngeluarin dia. Cek permission Ban Members / Kick Members dan pastiin "
                          "role NusaKlip AI ada di atas role dia.\n"
                          f"Isi pesan: `{preview}`")
    except discord.HTTPException as e:
        print(f"[honeypot] failed on {author.id}: {e}", flush=True)


honeypot = app_commands.Group(
    name="honeypot", description="Room jebakan untuk akun yang dibajak",
    guild_only=True, default_permissions=discord.Permissions(manage_guild=True),
)


@honeypot.command(name="pasang", description="Kirim pesan peringatan di room jebakan")
async def honeypot_pasang(interaction: discord.Interaction):
    ch = interaction.guild.get_channel(CHANNEL_ID) if CHANNEL_ID else None
    if not isinstance(ch, discord.TextChannel):
        await interaction.response.send_message("HONEYPOT_CHANNEL_ID belum diset di Railway.", ephemeral=True)
        return
    await ch.send(embed=warning_embed())
    await interaction.response.send_message(f"Peringatan udah dipasang di {ch.mention}.", ephemeral=True)


@honeypot.command(name="cek", description="Cek setup room jebakan")
async def honeypot_cek(interaction: discord.Interaction):
    g = interaction.guild
    me = g.me
    ch = g.get_channel(CHANNEL_ID) if CHANNEL_ID else None
    log = g.get_channel(LOG_CHANNEL_ID) if LOG_CHANNEL_ID else None
    need_ban = ACTION != "kick"
    perm_ok = me.guild_permissions.ban_members if need_ban else me.guild_permissions.kick_members
    everyone_can_post = bool(ch) and ch.permissions_for(g.default_role).send_messages
    lines = [
        f"{'✅' if ch else '❌'} Room jebakan: {ch.mention if ch else 'HONEYPOT_CHANNEL_ID belum diset'}",
        f"{'✅' if everyone_can_post else '❌'} @everyone bisa chat di room itu "
        "(harus bisa, biar akun bajakan ketangkep)",
        f"{'✅' if perm_ok else '❌'} Permission bot: {'Ban Members' if need_ban else 'Kick Members'}",
        f"{'✅' if ch and ch.permissions_for(me).manage_messages else '❌'} Bot bisa hapus pesan di room itu",
        f"{'✅' if log else '❌'} Log: {log.mention if log else 'nggak ada'}",
        f"Mode: **{ACTION}**" + (f", hapus pesan {DELETE_SECONDS // 3600} jam terakhir" if need_ban else ""),
        "-# Role NusaKlip AI juga harus di atas role member biasa (Elite, VIP, Alumni, dll).",
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())


def setup(bot) -> None:
    bot.tree.add_command(honeypot)
    bot.add_listener(on_message, "on_message")
