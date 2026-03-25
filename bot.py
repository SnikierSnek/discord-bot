import re
import asyncio
import discord
from discord.ext import commands

TOKEN = "DISCORD_BOT_TOKEN"

WAIT_THRESHOLD_POINTS = 1950
WAIT_SECONDS = 15
MAX_MESSAGE_LENGTH = 1950

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# key: (channel_id, author_id)
# value: {
#   "parts": [message_text, ...],
#   "messages": [discord.Message, ...],
#   "task": asyncio.Task
# }
pending_lists = {}


def clean_unit_name(name):
    name = name.strip()
    name = re.sub(r"^[A-Za-z]+\d+:\s*", "", name)  # Removes Char1:
    name = re.sub(r"^\d+x\s+", "", name, flags=re.IGNORECASE)  # Removes 1x
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

    # Format: Enhancement: Font of Spores (Aura) (+20 pts)
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

    # Format: Enhancement: Starfall Shells
    m = re.match(r"^Enhancement:\s*(?P<name>.+?)\s*$", stripped, re.IGNORECASE)
    if m:
        enh_name = m.group("name").strip()
        enh_name = re.sub(r"\s*\(Aura\)\s*$", "", enh_name, flags=re.IGNORECASE)
        return enh_name

    return None


def shorten_warhammer_list(raw_text):
    """
    Returns:
        formatted_output (str)
        total_points (int)
        unit_count (int)
    """
    lines = raw_text.splitlines()
    results = []
    current_unit = None
    total_points = 0

    unit_patterns = [
        # Char3: 1x Great Unclean One (270 pts): ...
        r"^(?P<prefix>[A-Za-z]+\d+:\s*)?(?P<count>\d+x\s+)?(?P<name>.+?)\s*\((?P<pts>\d+)\s*(?:pts?|points?|p)\)\s*:",
        # Jackal Alphus (65 points)
        r"^(?P<prefix>[A-Za-z]+\d+:\s*)?(?P<count>\d+x\s+)?(?P<name>.+?)\s*\((?P<pts>\d+)\s*(?:pts?|points?|p)\)\s*$",
        # Redemptor Dreadnought - 210 pts
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

        # Ignore +++++ separators
        if set(line) == {"+"}:
            continue

        # Ignore headings / metadata
        if lower_line.startswith(ignored_headers):
            continue

        # Ignore bullet weapon/detail lines, but keep bullet enhancement lines
        if re.match(r"^[•\-\*]\s*\d+x?\s+", line):
            continue

        # Enhancement belongs to previous unit
        enhancement = parse_enhancement_line(line)
        if enhancement and current_unit is not None:
            current_unit["enhancements"].append(enhancement)
            continue

        # Try matching a unit
        matched = None
        for pattern in unit_patterns:
            m = re.match(pattern, line, re.IGNORECASE)
            if m:
                matched = m
                break

        if matched:
            unit_name = clean_unit_name(matched.group("name"))
            pts = int(matched.group("pts").strip())

            # Skip likely army title lines like "bikerboyz (2000 points)"
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
    """
    More permissive than looks_like_warhammer_list().
    Used while the bot is already waiting for follow-up parts.
    """
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

    result, total_points, unit_count = shorten_warhammer_list(combined_text)

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

    content = message.content.strip()
    if not content:
        return

    key = (message.channel.id, message.author.id)

    # Manual command
    if content.startswith("!wl"):
        text = content[3:].strip()
        result, total_points, unit_count = shorten_warhammer_list(text)

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

    # If already waiting on this user's list in this channel,
    # accept follow-up chunks even if they are not full lists by themselves.
    if key in pending_lists:
        if contains_list_content(content):
            pending_lists[key]["parts"].append(content)
            pending_lists[key]["messages"].append(message)

            combined_text = "\n".join(pending_lists[key]["parts"])
            result, total_points, unit_count = shorten_warhammer_list(combined_text)

            old_task = pending_lists[key]["task"]
            old_task.cancel()

            # If combined parsed unit total is high enough, compact immediately
            if total_points >= WAIT_THRESHOLD_POINTS:
                await process_pending_list(message.channel.id, message.author.id)
            else:
                new_task = asyncio.create_task(
                    delayed_process_list(message.channel.id, message.author.id)
                )
                pending_lists[key]["task"] = new_task
            return

    # Fresh auto-detected list
    if looks_like_warhammer_list(content):
        result, total_points, unit_count = shorten_warhammer_list(content)

        if result == "No valid units found.":
            await bot.process_commands(message)
            return

        # Send immediately if parsed unit total is at least 1950
        if total_points >= WAIT_THRESHOLD_POINTS:
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
            # Otherwise wait for more parts
            task = asyncio.create_task(
                delayed_process_list(message.channel.id, message.author.id)
            )
            pending_lists[key] = {
                "parts": [content],
                "messages": [message],
                "task": task,
            }

    await bot.process_commands(message)


bot.run(TOKEN)