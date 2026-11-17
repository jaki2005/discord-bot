"""
/kirim: staff write a message that is posted as NusaKlip AI.

  /kirim [channel] [balas_ke] [gambar]
    Opens a form with a multi-line text box. On submit the bot posts the text (plus the
    optional image) in the channel, optionally as a reply to a message link. The command
    reply is ephemeral, so the channel only shows NusaKlip AI, not "Haikal used /kirim".

  /edit-pesan link:<message link>
    Opens the same form prefilled with one of the bot's own messages and edits it in place.

Mentions: type them in Discord's raw form (<@user id>, <@&role id>) or use balas_ke to reply.
@everyone and @here are never pinged.
"""

import re

import discord
from discord import app_commands

LINK_RE = re.compile(r"discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)")
MENTIONS = discord.AllowedMentions(everyone=False, users=True, roles=True, replied_user=True)


async def _fetch_by_link(interaction: discord.Interaction, link: str) -> discord.Message | None:
    m = LINK_RE.search(link or "")
    if not m or int(m.group(1)) != interaction.guild_id:
        return None
    ch = interaction.guild.get_channel_or_thread(int(m.group(2)))
    if ch is None:
        return None
    try:
        return await ch.fetch_message(int(m.group(3)))
    except discord.HTTPException:
        return None


class SayForm(discord.ui.Modal):
    text = discord.ui.TextInput(label="Pesan", style=discord.TextStyle.paragraph, max_length=2000,
                                placeholder="Tulis pesan yang mau dikirim sebagai NusaKlip AI")

    def __init__(self, target, reply_to=None, image=None, edit_msg=None):
        super().__init__(title="Edit pesan NusaKlip AI" if edit_msg else "Kirim sebagai NusaKlip AI")
        self.target, self.reply_to, self.image, self.edit_msg = target, reply_to, image, edit_msg
        if edit_msg:
            self.text.default = edit_msg.content[:2000]

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        content = self.text.value
        try:
            if self.edit_msg:
                await self.edit_msg.edit(content=content, allowed_mentions=MENTIONS)
                await interaction.followup.send(f"✅ Pesan diedit: {self.edit_msg.jump_url}", ephemeral=True)
                return
            kwargs = {"content": content, "allowed_mentions": MENTIONS}
            if self.image:
                kwargs["file"] = await self.image.to_file()
            if self.reply_to:
                kwargs["reference"] = self.reply_to.to_reference(fail_if_not_exists=False)
            sent = await self.target.send(**kwargs)
        except discord.Forbidden:
            await interaction.followup.send(f"Bot nggak punya izin kirim pesan di {self.target.mention}.",
                                            ephemeral=True)
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f"Gagal kirim pesan ({e.status}).", ephemeral=True)
            return
        await interaction.followup.send(f"✅ Terkirim: {sent.jump_url}", ephemeral=True)


@app_commands.command(name="kirim", description="Kirim pesan sebagai NusaKlip AI")
@app_commands.describe(
    channel="Channel tujuan (kosongin buat channel ini)",
    balas_ke="Link pesan yang mau dibales (opsional)",
    gambar="Gambar atau file yang ikut dikirim (opsional)",
)
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def kirim(interaction: discord.Interaction, channel: discord.TextChannel | None = None,
                balas_ke: str | None = None, gambar: discord.Attachment | None = None):
    target = channel or interaction.channel
    reply_to = None
    if balas_ke:
        reply_to = await _fetch_by_link(interaction, balas_ke)
        if reply_to is None:
            await interaction.response.send_message(
                "Link pesan yang mau dibales nggak ketemu. Pakai **Copy Message Link** dari pesan di server ini.",
                ephemeral=True)
            return
        target = reply_to.channel
    await interaction.response.send_modal(SayForm(target, reply_to, gambar))


@app_commands.command(name="edit-pesan", description="Edit pesan yang pernah dikirim NusaKlip AI")
@app_commands.describe(link="Link pesan NusaKlip AI yang mau diedit")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def edit_pesan(interaction: discord.Interaction, link: str):
    msg = await _fetch_by_link(interaction, link)
    if msg is None or msg.author.id != interaction.client.user.id:
        await interaction.response.send_message("Itu bukan link pesan dari NusaKlip AI.", ephemeral=True)
        return
    if msg.embeds and not msg.content:
        await interaction.response.send_message(
            "Pesan itu isinya embed (panel atau pengumuman), jadi nggak bisa diedit lewat sini.", ephemeral=True)
        return
    await interaction.response.send_modal(SayForm(msg.channel, edit_msg=msg))


def setup(bot) -> None:
    bot.tree.add_command(kirim)
    bot.tree.add_command(edit_pesan)
