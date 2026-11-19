"""
AI sales assistant inside Konsultasi Akses tickets (Claude via the Anthropic API).

  Needs ANTHROPIC_API_KEY. Without it the module stays off and tickets work as before.

  - Replies to the ticket owner a few seconds after they stop typing (messages are batched).
  - Keeps running when staff write in the ticket (it reads their messages for context).
    Set AI_PAUSE_ON_STAFF=true to pause it instead as soon as staff write.
    /konsultasi ai status:nyala|mati inside a ticket turns it on or off (only staff see the reply).
  - When the model ends a reply with [[ADMIN]], the marker is removed and the staff role is
    tagged (once per 30 minutes per ticket).
  - Cost caps: AI_MAX_REPLIES_PER_TICKET and AI_DAILY_LIMIT.

Knowledge and sales rules live in ai_sales_prompt.md (channel links filled in at runtime).
"""

import asyncio
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

import consult as C

API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
MODEL = os.getenv("AI_MODEL", "claude-haiku-5-5").strip()
ENABLED = bool(API_KEY) and os.getenv("AI_ENABLED", "true").strip().lower() != "false"
MAX_PER_TICKET = int(os.getenv("AI_MAX_REPLIES_PER_TICKET", "25"))
DAILY_LIMIT = int(os.getenv("AI_DAILY_LIMIT", "300"))
DEBOUNCE_SECONDS = float(os.getenv("AI_DEBOUNCE_SECONDS", "6"))
PAUSE_ON_STAFF = os.getenv("AI_PAUSE_ON_STAFF", "false").strip().lower() == "true"
HISTORY_LIMIT = 40
ADMIN_TOKEN = "[[ADMIN]]"
PING_COOLDOWN = 30 * 60

AI_FOOTER = "-# 🤖 Dijawab asisten AI NusaKlip. Mau ngobrol sama admin? Bilang aja."
MARK_ON = "-# 🤖 Asisten AI diaktifkan lagi di ticket ini."
MARK_OFF = "-# 🤖 Asisten AI dimatikan di ticket ini."
SYSTEM_PREFIXES = ("⏳", "🔔", "Oke, ticket ditutup", "Sip, ticket ditutup", "✅ Dijawab", "🙋 Dijawab", "-# 🤖 Asisten")

PROMPT_FILE = Path(__file__).parent / "ai_sales_prompt.md"

_client = None
_pending: dict[int, asyncio.Task] = {}
_last_admin_ping: dict[int, float] = {}
_day = {"date": None, "count": 0}


def _ref(env: str, name: str) -> str:
    v = os.getenv(env, "").strip()
    return f"<#{v}>" if v else f"#{name}"


def system_prompt() -> str:
    text = PROMPT_FILE.read_text(encoding="utf-8")
    refs = {
        "blueprint_ch": _ref("BLUEPRINT_CHANNEL_ID", "full-clipping-blueprint"),
        "mentoring_ch": _ref("MENTORING_CHANNEL_ID", "mentoring-ekslusif"),
        "renew_ch": _ref("RENEW_CHANNEL_ID", "perpanjang-akses"),
        "booking_ch": _ref("BOOKING_CHANNEL_ID", "booking-1on1"),
        "help_ch": _ref("HELP_CHANNEL_ID", "bantuan"),
        "guide_ch": _ref("GUIDE_CHANNEL_ID", "panduan-gratis-content-rewards"),
        "content_rewards_ch": _ref("CONTENT_REWARDS_CHANNEL_ID", "daftar-content-rewards"),
        "diskusi_ch": _ref("WELCOME_CHANNEL_ID", "diskusi-clipper"),
        "member_win": _ref("MEMBER_WIN_CHANNEL_ID", "member-win-nusaclippers"),
    }
    for k, v in refs.items():
        text = text.replace("{" + k + "}", v)
    today = datetime.now(ZoneInfo("Asia/Jakarta")).strftime("%d %B %Y")
    return text + f"\n\n# Konteks\nHari ini: {today}. Kamu sedang membalas di sebuah ticket Konsultasi Akses."


def client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.AsyncAnthropic(api_key=API_KEY)
    return _client


def is_consult_ticket(ch) -> bool:
    return isinstance(ch, discord.TextChannel) and bool(ch.topic) and ch.topic.startswith(C.TOPIC_PREFIX)


def _is_ai_text(msg: discord.Message) -> bool:
    return not msg.embeds and bool(msg.content) and not msg.content.startswith(SYSTEM_PREFIXES) \
        and C.IRRELEVANT_MARKER not in msg.content


def _toggle_from_url(url):
    """The ticket header embed URL may carry ai=on|off and at=<unix time> (set by /konsultasi ai)."""
    q = parse_qs(urlparse(url or "").query)
    try:
        return q["ai"][0], float(q["at"][0])
    except (KeyError, ValueError, IndexError):
        return None, 0.0


async def _header(ch: discord.TextChannel):
    async for msg in ch.history(limit=10, oldest_first=True):
        if msg.author.id == ch.guild.me.id and msg.embeds and C.ticket_owner_from_url(msg.embeds[0].url):
            return msg
    return None


async def _state(ch: discord.TextChannel, owner_id: int):
    """Return (paused, ai_reply_count, history) from the channel, oldest first."""
    msgs = [m async for m in ch.history(limit=HISTORY_LIMIT)]
    msgs.reverse()
    ai_count, history = 0, []
    events = []  # (unix time, paused?) ; the newest event decides
    me = ch.guild.me.id
    header = await _header(ch)
    if header:
        mode, at = _toggle_from_url(header.embeds[0].url)
        if mode in ("on", "off"):
            events.append((at, mode == "off"))
    for m in msgs:
        ts = m.created_at.timestamp()
        if m.author.id == me:
            if m.content.startswith(MARK_OFF):  # older tickets toggled with a visible message
                events.append((ts, True))
            elif m.content.startswith(MARK_ON):
                events.append((ts, False))
            elif _is_ai_text(m):
                ai_count += 1
                history.append(("assistant", m.content.replace(AI_FOOTER, "").strip(), []))
        elif m.author.id == owner_id:
            history.append(("user", m.content or "", [a for a in m.attachments
                                                      if (a.content_type or "").startswith("image/")]))
        elif not m.author.bot and isinstance(m.author, discord.Member) and C.is_staff(m.author):
            if PAUSE_ON_STAFF:
                events.append((ts, True))  # a human took over
            elif m.content:
                # AI keeps running; it sees what the admin said so it stays consistent with it
                history.append(("assistant", f"(Admin {m.author.display_name} menjawab:) {m.content}", []))
    paused = max(events)[1] if events else False
    return paused, ai_count, history


def _messages(history):
    """Merge into alternating user/assistant turns; images only from the last few user turns."""
    out = []
    image_budget = 3
    for role, text, images in reversed(history):
        blocks = []
        if role == "user" and images and image_budget > 0:
            for a in images[:image_budget]:
                blocks.append({"type": "image", "source": {"type": "url", "url": a.url}})
            image_budget -= len(blocks)
        if text.strip():
            blocks.append({"type": "text", "text": text})
        elif role == "user" and images:
            blocks.append({"type": "text", "text": "(member mengirim gambar)"})
        if not blocks:
            continue
        if out and out[0]["role"] == role:
            out[0]["content"] = blocks + out[0]["content"]
        else:
            out.insert(0, {"role": role, "content": blocks})
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def _strip_images(messages):
    clean = []
    for m in messages:
        blocks = [b for b in m["content"] if b["type"] == "text"] or [{"type": "text", "text": "(gambar)"}]
        clean.append({"role": m["role"], "content": blocks})
    return clean


async def _ask(messages) -> str:
    kwargs = dict(
        model=MODEL, max_tokens=600,
        system=[{"type": "text", "text": system_prompt(), "cache_control": {"type": "ephemeral"}}],
        messages=messages,
    )
    try:
        resp = await client().messages.create(**kwargs)
    except Exception as e:
        if any(b["type"] == "image" for m in messages for b in m["content"]):
            print(f"[consult-ai] retry without images: {type(e).__name__}: {e}", flush=True)
            kwargs["messages"] = _strip_images(messages)
            resp = await client().messages.create(**kwargs)
        else:
            raise
    u = resp.usage
    print(f"[consult-ai] {MODEL} in={u.input_tokens} cache_read={getattr(u, 'cache_read_input_tokens', 0)} "
          f"out={u.output_tokens}", flush=True)
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()


def _under_daily_limit() -> bool:
    today = datetime.now(timezone.utc).date()
    if _day["date"] != today:
        _day.update(date=today, count=0)
    return _day["count"] < DAILY_LIMIT


async def _respond(ch: discord.TextChannel, owner_id: int):
    await asyncio.sleep(DEBOUNCE_SECONDS)
    paused, ai_count, history = await _state(ch, owner_id)
    if paused:
        return
    if ai_count >= MAX_PER_TICKET or not _under_daily_limit():
        if ai_count == MAX_PER_TICKET:
            await ch.send(f"🔔 {C.staff_mention(ch.guild)}, ticket ini udah panjang, tolong lanjutin sama admin ya.",
                          allowed_mentions=discord.AllowedMentions(roles=True))
        return
    messages = _messages(history)
    if not messages or messages[-1]["role"] != "user":
        return
    async with ch.typing():
        try:
            reply = await _ask(messages)
        except Exception as e:
            print(f"[consult-ai] API error in #{ch.name}: {type(e).__name__}: {e}", flush=True)
            await ch.send(f"🔔 {C.staff_mention(ch.guild)}, asisten AI lagi error. Tolong bantu jawab ya.",
                          allowed_mentions=discord.AllowedMentions(roles=True))
            return
    _day["count"] += 1
    needs_admin = ADMIN_TOKEN in reply
    reply = reply.replace(ADMIN_TOKEN, "").replace("\u2014", ", ").strip()
    if not reply:
        return
    if ai_count == 0:
        reply = f"{reply}\n\n{AI_FOOTER}"
    for i in range(0, len(reply), 1900):
        await ch.send(reply[i:i + 1900], allowed_mentions=discord.AllowedMentions.none())
    if needs_admin and time.time() - _last_admin_ping.get(ch.id, 0) > PING_COOLDOWN:
        _last_admin_ping[ch.id] = time.time()
        await ch.send(f"🔔 {C.staff_mention(ch.guild)}, <@{owner_id}> butuh bantuan admin di ticket ini.",
                      allowed_mentions=discord.AllowedMentions(roles=True, users=False))


async def on_message(message: discord.Message):
    if not ENABLED or message.author.bot or not is_consult_ticket(message.channel):
        return
    owner = await C.ticket_owner(message.channel)
    if owner != message.author.id:
        return
    old = _pending.get(message.channel.id)
    if old and not old.done():
        old.cancel()  # member still typing: wait for the last message

    async def run():
        try:
            await _respond(message.channel, owner)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[consult-ai] error in #{message.channel.name}: {type(e).__name__}: {e}", flush=True)

    _pending[message.channel.id] = asyncio.create_task(run())


@app_commands.command(name="ai", description="Nyalain atau matiin asisten AI di ticket konsultasi ini")
@app_commands.describe(status="nyala atau mati")
@app_commands.choices(status=[app_commands.Choice(name="nyala", value="on"),
                              app_commands.Choice(name="mati", value="off")])
async def ai_toggle(interaction: discord.Interaction, status: app_commands.Choice[str]):
    if not is_consult_ticket(interaction.channel):
        await interaction.response.send_message("Command ini cuma bisa dipakai di dalam ticket konsultasi.",
                                                ephemeral=True)
        return
    if not ENABLED:
        await interaction.response.send_message("Asisten AI belum aktif (ANTHROPIC_API_KEY belum diisi).",
                                                ephemeral=True)
        return
    header = await _header(interaction.channel)
    if not header:
        await interaction.response.send_message("Pesan pembuka ticket nggak ketemu, jadi status AI nggak bisa diubah.",
                                                ephemeral=True)
        return
    embed = header.embeds[0]
    u = urlparse(embed.url)
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    q.update(ai=status.value, at=str(int(time.time()) + 1))
    embed.url = urlunparse(u._replace(query=urlencode(q)))
    await header.edit(embed=embed)  # saved silently in the ticket header, nothing posted in the chat
    await interaction.response.send_message(
        "🤖 Asisten AI " + ("dinyalain lagi" if status.value == "on" else "dimatiin") + " di ticket ini. "
        "Cuma kamu yang lihat pesan ini.", ephemeral=True)


def setup(bot) -> None:
    C.konsultasi.add_command(ai_toggle)
    bot.add_listener(on_message, "on_message")
    print(f"[consult-ai] {'ON, model ' + MODEL if ENABLED else 'OFF (no ANTHROPIC_API_KEY)'}", flush=True)
