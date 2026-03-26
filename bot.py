import os
import re
import asyncio
import discord
from discord.ext import commands

# =========================
# 🔐 TOKEN (SAFE HANDLING)
# =========================
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

if not TOKEN:
    raise ValueError("❌ DISCORD_BOT_TOKEN is NOT set in environment variables.")

if "PASTE" in TOKEN or len(TOKEN) < 50:
    raise ValueError("❌ Invalid token detected. Make sure you pasted the REAL Discord bot token.")

print(f"🔍 Token detected (starts with): {TOKEN[:6]}...")

# =========================
# ⚙️ SETTINGS
# =========================
WAIT_THRESHOLD_POINTS = 1950
WAIT_SECONDS = 15
MAX_MESSAGE_LENGTH = 1950

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

pending_lists = {}

# =========================
# 🧠 HELPERS
# =========================

def clean_unit_name(name):
    name = name.strip()
    name = re.sub(r"^[A-Za-z]+\d+:\s*", "", name)
    name = re.sub(r"^\d+x\s+", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[\-–—:;,]+$", "", name).strip()
    return name


def parse_enhancement_line(line):
    stripped = line.strip()
    stripped = re.sub(r"^[•\-\*]\s*", "", stripped)

    m = re.match(
        r"^Enhancement:\s*(?P<name>.+?)\s*\(\+(?P<pts>\d+)\s*(?:pts?|points?|p)\)\s*$",
        stripped,
        re.IGNORECASE,
    )
    if m:
        return f"{m.group('name')} +{m.group('pts')}p"

    m = re.match(r"^Enhancement:\s*(?P<name>.+?)\s*$", stripped, re.IGNORECASE)
    if m:
        return m.group("name")

    return None


def shorten_warhammer_list(raw_text):
    lines = raw_text.splitlines()
    results = []
    current_unit = None
    total_points = 0

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Enhancement
        enh = parse_enhancement_line(line)
        if enh and current_unit:
            current_unit["enhancements"].append(enh)
            continue

        # Unit match
        m = re.match(
            r"(?:\d+x\s+)?(.+?)\s*\((\d+)\s*(?:pts?|points?|p)\)",
            line,
            re.IGNORECASE,
        )

        if m:
            name = clean_unit_name(m.group(1))
            pts = int(m.group(2))

            current_unit = {
                "name": name,
                "pts": pts,
                "enhancements": []
            }

            results.append(current_unit)
            total_points += pts

    formatted = []
    for unit in results:
        if unit["enhancements"]:
            formatted.append(
                f"{unit['name']} [{', '.join(unit['enhancements'])}] ({unit['pts']}p)"
            )
        else:
            formatted.append(f"{unit['name']} ({unit['pts']}p)")

    if not formatted:
        return None, 0

    return "\n".join(formatted), total_points


def looks_like_list(text):
    return bool(re.search(r"\(\d+\s*(pts?|points?)\)", text, re.IGNORECASE))


# =========================
# 🚀 DISCORD EVENTS
# =========================

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    content = message.content.strip()

    if not looks_like_list(content):
        return

    result, total = shorten_warhammer_list(content)

    if not result:
        return

    # If full list → send immediately
    if total >= WAIT_THRESHOLD_POINTS:
        await message.channel.send(f"```{result}```")

        try:
            await message.delete()
        except discord.Forbidden:
            await message.channel.send("⚠️ I lack permission to delete messages.")

    else:
        # Wait 15 seconds (partial list)
        await asyncio.sleep(WAIT_SECONDS)

        await message.channel.send(f"```{result}```")

        try:
            await message.delete()
        except discord.Forbidden:
            await message.channel.send("⚠️ I lack permission to delete messages.")


bot.run(TOKEN)