import os
import re
import asyncio
import discord
from discord.ext import commands
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise ValueError("DISCORD_BOT_TOKEN is not set.")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

WAIT_THRESHOLD_POINTS = 1950
WAIT_SECONDS = 15
pending_lists = {}

# ---------------- HEALTH SERVER ----------------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")

def run_health_server():
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

# ---------------- HELPERS ----------------
def pretty(text):
    return " ".join(word.capitalize() for word in text.split())

def add_weapon(counter, name, count):
    name = pretty(name.strip())
    counter[name] = counter.get(name, 0) + count

# ---------------- METADATA ----------------
def parse_metadata(text):
    faction = "Unknown"
    detachment = "Unknown"
    points = None

    for line in text.splitlines():
        line = line.strip()

        m = re.match(r"\+ FACTION KEYWORD:\s*(.+)", line, re.I)
        if m:
            faction = m.group(1)

        m = re.match(r"\+ DETACHMENT:\s*(.+)", line, re.I)
        if m:
            detachment = re.sub(r"\(.*\)", "", m.group(1)).strip()

        m = re.match(r"\+ TOTAL ARMY POINTS:\s*(\d+)", line, re.I)
        if m:
            points = int(m.group(1))

    return faction, detachment, points

# ---------------- PARSER ----------------
def parse_units(text):
    lines = text.splitlines()
    units = []
    current = None

    for i, line in enumerate(lines):
        stripped = line.strip()

        # New unit
        m = re.match(r"(.+?)\s*\((\d+)\s*pts?\)", stripped, re.I)
        if m and not stripped.startswith("•"):
            current = {
                "name": pretty(m.group(1)),
                "pts": int(m.group(2)),
                "weapons": {},
                "enh": []
            }
            units.append(current)
            continue

        if not current:
            continue

        # Enhancement
        m = re.search(r"Enhancement:\s*(.+)", stripped, re.I)
        if m:
            current["enh"].append(pretty(m.group(1)))
            continue

        m = re.search(r"(.+?)\(\+(\d+)", stripped)
        if m:
            current["enh"].append(f"{pretty(m.group(1))} (+{m.group(2)}p)")
            continue

        # Weapon line
        m = re.match(r"[•◦-]?\s*(\d+)x\s+(.+)", stripped)
        if m:
            add_weapon(current["weapons"], m.group(2), int(m.group(1)))
            continue

        # Nested weapon line (important fix)
        if re.match(r"\d+x\s+.+", stripped):
            count = int(stripped.split("x")[0])
            name = stripped.split("x", 1)[1]
            add_weapon(current["weapons"], name, count)

    return units

# ---------------- RENDER ----------------
def render(units, faction, detachment, points):
    out = []
    out.append(f"Faction: {faction}")
    out.append(f"Detachment: {detachment}")
    out.append(f"Points: {points if points else 'Unknown'}")

    for u in units:
        line = f"\n**{u['name']}** [{u['pts']}p]"

        weapons = ", ".join(f"{v}x {k}" for k, v in u["weapons"].items())

        if u["enh"]:
            enh = "; ".join(f"Enhancement: {e}" for e in u["enh"])
            weapons += f" [{enh}]"

        line += f"\n- {weapons}"
        out.append(line)

    return "\n".join(out)

# ---------------- CORE ----------------
def shorten(text):
    faction, detachment, points = parse_metadata(text)
    units = parse_units(text)

    if not units:
        return None, 0

    total = sum(u["pts"] for u in units)
    return render(units, faction, detachment, points), total

# ---------------- DISCORD ----------------
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    content = message.content

    result, total = shorten(content)

    if result:
        if total >= WAIT_THRESHOLD_POINTS:
            await message.channel.send(result)
            try:
                await message.delete()
            except:
                pass
        else:
            await asyncio.sleep(WAIT_SECONDS)
            await message.channel.send(result)

    await bot.process_commands(message)

# ---------------- START ----------------
if __name__ == "__main__":
    Thread(target=run_health_server, daemon=True).start()
    bot.run(TOKEN)