import os
import re
import asyncio
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer

import discord
from discord.ext import commands

TOKEN = os.getenv("DISCORD_BOT_TOKEN")

if not TOKEN:
    raise ValueError("DISCORD_BOT_TOKEN is not set.")

WAIT_THRESHOLD_POINTS = 1950
WAIT_SECONDS = 15
MAX_MESSAGE_LENGTH = 1950

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

pending_lists = {}


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Bot is running")

    def log_message(self, format, *args):
        return


def run_health_server():
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"Health server listening on port {port}")
    server.serve_forever()


def clean_unit_name(name):
    name = name.strip()
    name = re.sub(r"^[A-Za-z]+\d+:\s*", "", name)
    name = re.sub(r"^\d+x\s+", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[\-–—:;,]+$", "", name).strip()
    return name


def split_long_message(text, max_len=MAX_MESSAGE_LENGTH):
    lines = text.splitlines()
    chunks = []
    current = ""

    for line in lines:
        candidate = line if not current else current + "\n" + line
        if len(candidate) <= max_len:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = line

    if current:
        chunks.append(current)

    return chunks


def parse_enhancement_line(line):
    stripped = line.strip()
    stripped = re.sub(r"^[•\-\*]\s*", "", stripped)

    m = re.match(
        r"^Enhancement:\s*(?P<name>.+?)\s*\(\+(?P<pts>\d+)\s*(?:pts?|points?|p)\)\s*$",
        stripped,
        re.IGNORECASE,
    )
    if m:
        enh_name = m.group("name").strip()
        enh_name = re.sub(r"\s*\(Aura\)\s*$", "", enh_name, flags=re.IGNORECASE)
        enh_pts = m.group("pts").strip()
        return f"{enh_name} +{enh_pts}p"

    m = re.match(r"^Enhancement:\s*(?P<name>.+?)\s*$", stripped, re.IGNORECASE)
    if m:
        enh_name = m.group("name").strip()
        enh_name = re.sub(r"\s*\(Aura\)\s*$", "", enh_name, flags=re.IGNORECASE)
        return enh_name

    return None


def shorten_warhammer_list(raw_text):
    lines = raw_text.splitlines()
    results = []
    current_unit = None
    total_points = 0

    unit_patterns = [
        r"^(?P<prefix>[A-Za-z]+\d+:\s*)?(?P<count>\d+x\s+)?(?P<name>.+?)\s*\((?P<pts>\d+)\s*(?:pts?|points?|p)\)\s*:",
        r"^(?P<prefix>[A-Za-z]+\d+:\s*)?(?P<count>\d+x\s+)?(?P<name>.+?)\s*\((?P<pts>\d+)\s*(?:pts?|points?|p)\)\s*$",
        r"^(?P<prefix>[A-Za-z]+\d+:\s*)?(?P<count>\d+x\s+)?(?P<name>.+?)\s*[-–—]\s*(?P<pts>\d+)\s*(?:pts?|points?|p)\s*$",
    ]

    ignored_headers = (
        "+ faction keyword",
        "+ detachment",
        "+ total army points",
        "+ warlord",
        "+ enhancement",
        "+ number of units",
        "+ secondary",
        "faction keyword",
        "detachment",
        "total army points",
        "number of units",
        "secondary",
        "characters",
        "battleline",
        "dedicated transports",
        "other datasheets",
        "mounted",
        "vehicles",
        "monsters",
        "infantry",
        "epic hero",
        "allied units",
        "warlord",
        "enhancement",
        "exported with",
        "army roster",
        "detachment choice",
    )

    for raw_line in lines:
        line = raw_line.strip()

        if not line:
            continue

        lower_line = line.lower()

        if set(line) == {"+"}:
            continue

        if lower_line.startswith(ignored_headers):
            continue

        if re.match(r"^[•\-\*]\s*\d+x?\s+", line):
            continue

        enhancement = parse_enhancement_line(line)
        if enhancement and current_unit is not None:
            current_unit["enhancements"].append(enhancement)
            continue

        matched = None
        for pattern in unit_patterns:
            m = re.match(pattern, line, re.IGNORECASE)
            if m:
                matched = m
                break

        if matched:
            unit_name = clean_unit_name(matched.group("name"))
            pts = int(matched.group("pts").strip())

            if not results and pts >= 1500 and ":" not in line:
                current_unit = None
                continue

            current_unit = {
                "name": unit_name,
                "pts": pts,
                "enhancements": []
            }
            results.append(current_unit)
            total_points += pts
            continue

    formatted = []
    for unit in results:
        if unit["enhancements"]:
            enh_text = ", ".join(unit["enhancements"])
            formatted.append(f"{unit['name']} [{enh_text}] ({unit['pts']}p)")
        else:
            formatted.append(f"{unit['name']} ({unit['pts']}p)")

    if not formatted:
        return "No valid units found.", 0, 0

    return "\n".join(formatted), total_points, len(results)


def looks_like_warhammer_list(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return False

    unit_like_count = 0

    for line in lines:
        if re.match(
            r"^(?:[A-Za-z]+\d+:\s*)?(?:\d+x\s+)?(.+?)\s*\((\d+)\s*(?:pts?|points?|p)\)\s*:?\s*$",
            line,
            re.IGNORECASE,
        ):
            unit_like_count += 1
        elif re.match(
            r"^(?:[A-Za-z]+\d+:\s*)?(?:\d+x\s+)?(.+?)\s*[-–—]\s*(\d+)\s*(?:pts?|points?|p)\s*$",
            line,
            re.IGNORECASE,
        ):
            unit_like_count += 1

    return unit_like_count >= 2


def contains_list_content(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False

    for line in lines:
        if re.match(
            r"^(?:[A-Za-z]+\d+:\s*)?(?:\d+x\s+)?(.+?)\s*\((\d+)\s*(?:pts?|points?|p)\)\s*:?\s*$",
            line,
            re.IGNORECASE,
        ):
            return True
        if re.match(
            r"^(?:[A-Za-z]+\d+:\s*)?(?:\d+x\s+)?(.+?)\s*[-–—]\s*(\d+)\s*(?:pts?|points?|p)\s*$",
            line,
            re.IGNORECASE,
        ):
            return True
        if re.match(r"^[•\-\*]?\s*Enhancement:\s*.+$", line, re.IGNORECASE):
            return True

    return False


async def send_compacted_list(channel, text):
    chunks = split_long_message(text, MAX_MESSAGE_LENGTH - 10)
    for chunk in chunks:
        await channel.send(f"```{chunk}```")


async def delete_original_messages(messages):
    failed = False

    for msg in messages:
        try:
            await msg.delete()
        except discord.Forbidden:
            print("Could not delete a message: missing Manage Messages permission.")
            failed = True
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            print(f"Could not delete a message: {e}")
            failed = True

    return failed


async def process_pending_list(channel_id, author_id):
    key = (channel_id, author_id)

    if key not in pending_lists:
        return

    entry = pending_lists.pop(key)
    channel = bot.get_channel(channel_id)
    if channel is None:
        return

    combined_text = "\n".join(entry["parts"])
    original_messages = entry["messages"]

    print(f"Processing pending list for user {author_id}")
    result, total_points, unit_count = shorten_warhammer_list(combined_text)
    print(f"Pending parse result: total_points={total_points}, unit_count={unit_count}")

    if result != "No valid units found.":
        await send_compacted_list(channel, result)
        failed = await delete_original_messages(original_messages)

        if failed:
            await channel.send("I compacted the list, but I could not delete the original messages. Please check that I have Manage Messages permission.")


async def delayed_process_list(channel_id, author_id):
    try:
        await asyncio.sleep(WAIT_SECONDS)
        await process_pending_list(channel_id, author_id)
    except asyncio.CancelledError:
        pass


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    print(f"Message seen from {message.author}: {message.content[:120]!r}")

    content = message.content.strip()
    if not content:
        return

    key = (message.channel.id, message.author.id)

    if content.startswith("!wl"):
        print("Manual command detected")
        text = content[3:].strip()
        result, total_points, unit_count = shorten_warhammer_list(text)
        print(f"Manual parse: total_points={total_points}, unit_count={unit_count}")

        if result != "No valid units found.":
            await send_compacted_list(message.channel, result)

            try:
                await message.delete()
            except discord.Forbidden:
                await message.channel.send("I compacted the list, but I could not delete the original command message. Please check that I have Manage Messages permission.")
            except discord.NotFound:
                pass
            except discord.HTTPException as e:
                print(f"Could not delete command message: {e}")
                await message.channel.send("I compacted the list, but I could not delete the original command message.")

        return

    if key in pending_lists:
        print("Pending list exists for this user/channel")
        if contains_list_content(content):
            print("Follow-up chunk accepted")
            pending_lists[key]["parts"].append(content)
            pending_lists[key]["messages"].append(message)

            combined_text = "\n".join(pending_lists[key]["parts"])
            result, total_points, unit_count = shorten_warhammer_list(combined_text)
            print(f"Combined parse: total_points={total_points}, unit_count={unit_count}")

            old_task = pending_lists[key]["task"]
            old_task.cancel()

            if total_points >= WAIT_THRESHOLD_POINTS:
                print("Threshold reached, processing immediately")
                await process_pending_list(message.channel.id, message.author.id)
            else:
                print("Still below threshold, restarting timer")
                new_task = asyncio.create_task(
                    delayed_process_list(message.channel.id, message.author.id)
                )
                pending_lists[key]["task"] = new_task
            return

    is_list = looks_like_warhammer_list(content)
    print(f"looks_like_warhammer_list = {is_list}")

    if is_list:
        result, total_points, unit_count = shorten_warhammer_list(content)
        print(f"Fresh parse: total_points={total_points}, unit_count={unit_count}")

        if result == "No valid units found.":
            print("List-like message but parser found no valid units")
            await bot.process_commands(message)
            return

        if total_points >= WAIT_THRESHOLD_POINTS:
            print("Threshold reached immediately, sending compacted list")
            await send_compacted_list(message.channel, result)

            try:
                await message.delete()
            except discord.Forbidden:
                await message.channel.send("I compacted the list, but I could not delete the original message. Please check that I have Manage Messages permission.")
            except discord.NotFound:
                pass
            except discord.HTTPException as e:
                print(f"Could not delete message: {e}")
                await message.channel.send("I compacted the list, but I could not delete the original message.")
        else:
            print("Below threshold, starting pending timer")
            task = asyncio.create_task(
                delayed_process_list(message.channel.id, message.author.id)
            )
            pending_lists[key] = {
                "parts": [content],
                "messages": [message],
                "task": task,
            }

    await bot.process_commands(message)


if __name__ == "__main__":
    Thread(target=run_health_server, daemon=True).start()
    bot.run(TOKEN)