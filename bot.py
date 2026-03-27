import os
import re
import csv
import asyncio
from io import StringIO
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer

import discord
from discord.ext import commands

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise ValueError("DISCORD_BOT_TOKEN is not set.")

WAIT_THRESHOLD_POINTS = 1950
WAIT_SECONDS = 15
MAX_MESSAGE_LENGTH = 1900
MAX_ATTACHMENT_BYTES = 1_000_000

BULLET_RE = r"[•◦\-\*]"

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
pending_lists = {}


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")

    def log_message(self, format, *args):
        return


def run_health_server():
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


def clean_unit_name(name: str) -> str:
    name = name.strip()
    name = re.sub(r"^[A-Za-z]+\d+:\s*", "", name)
    name = re.sub(r"^\d+x\s+", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[\-–—:;,]+$", "", name).strip()
    return name


def pretty_name(text: str) -> str:
    keep_lower = {"of", "and", "with", "the", "in"}
    words = []
    for i, word in enumerate(text.split()):
        if word.isupper():
            words.append(word)
            continue

        if "-" in word:
            parts = word.split("-")
            fixed = []
            for j, p in enumerate(parts):
                if not p:
                    fixed.append(p)
                elif p.lower() in keep_lower and (i != 0 or j != 0):
                    fixed.append(p.lower())
                else:
                    fixed.append(p.capitalize())
            words.append("-".join(fixed))
        else:
            if word.lower() in keep_lower and i != 0:
                words.append(word.lower())
            else:
                words.append(word.capitalize())
    return " ".join(words)


def get_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def split_top_level_commas(text: str):
    parts = []
    current = []
    depth = 0

    for ch in text:
        if ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            current.append(ch)
        elif ch == "," and depth == 0:
            part = "".join(current).strip()
            if part:
                parts.append(part)
            current = []
        else:
            current.append(ch)

    final = "".join(current).strip()
    if final:
        parts.append(final)

    return parts


def normalize_weapon_name(name: str) -> str:
    name = name.strip()
    name = re.sub(r"^\d+x\s+", "", name, flags=re.IGNORECASE)
    name = re.sub(r"^\d+\s+", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return pretty_name(name)


def add_count(counter: dict, key: str, amount: int):
    key = key.strip()
    if not key:
        return
    counter[key] = counter.get(key, 0) + amount


def is_ignored_weapon_item(item: str) -> bool:
    lowered = item.strip().lower()
    ignored = {
        "warlord",
        "configuration",
        "battleline",
        "characters",
        "character",
        "other datasheets",
        "allied units",
        "vehicle",
        "vehicles",
        "monster",
        "monsters",
        "mounted",
        "dedicated transport",
        "dedicated transports",
        "infantry",
        "beast",
        "beasts",
        "epic hero",
        "",
    }
    return lowered in ignored


def parse_metadata(raw_text: str):
    lines = raw_text.splitlines()

    faction = "Unknown"
    detachment = "Unknown"
    points = None

    for line in lines:
        stripped = line.strip()

        m = re.match(r"^\+\s*FACTION KEYWORD:\s*(.+)$", stripped, re.IGNORECASE)
        if m:
            faction = m.group(1).strip()

        m = re.match(r"^\+\s*DETACHMENT:\s*(.+)$", stripped, re.IGNORECASE)
        if m:
            detachment = re.sub(r"\s*\([^)]*\)\s*$", "", m.group(1).strip())

        m = re.match(r"^\+\s*TOTAL ARMY POINTS:\s*(\d+)\s*pts?$", stripped, re.IGNORECASE)
        if m:
            points = int(m.group(1))

    if points is None:
        for line in lines:
            stripped = line.strip()
            m = re.search(r"\((\d+)\s*Points?\)", stripped, re.IGNORECASE)
            if m:
                val = int(m.group(1))
                if val >= 1000:
                    points = val
                    break
            m = re.search(r"\[(\d+)\s*pts?\]", stripped, re.IGNORECASE)
            if m:
                val = int(m.group(1))
                if val >= 1000:
                    points = val
                    break

    if faction == "Unknown" or detachment == "Unknown":
        nonempty = [x.strip() for x in lines if x.strip()]
        for idx, line in enumerate(nonempty):
            if re.match(r"^Strike Force\s*\(\d+\s*Points?\)$", line, re.IGNORECASE):
                if idx >= 2 and faction == "Unknown":
                    faction = nonempty[idx - 2]
                if idx + 1 < len(nonempty) and detachment == "Unknown":
                    detachment = nonempty[idx + 1]
                break

    return {
        "faction": faction,
        "detachment": detachment,
        "points": points
    }


def parse_standalone_enhancement_line(line: str):
    stripped = re.sub(rf"^{BULLET_RE}\s*", "", line.strip())

    m = re.match(
        r"^Enhancement[s]?:\s*(?P<name>.+?)\s*\(\+(?P<pts>\d+)\s*(?:pts?|points?|p)\)\s*$",
        stripped,
        re.IGNORECASE,
    )
    if m:
        return f"{pretty_name(m.group('name').strip())} (+{m.group('pts')}p)"

    m = re.match(r"^Enhancement[s]?:\s*(?P<name>.+?)\s*$", stripped, re.IGNORECASE)
    if m:
        return pretty_name(m.group("name").strip())

    return None


def parse_bullet_enhancement_line(line: str):
    stripped = re.sub(rf"^{BULLET_RE}\s*", "", line.strip())

    m = re.match(
        r"^(?P<name>.+?)\s*\(\+(?P<pts>\d+)\s*(?:pts?|points?|p)\)\s*$",
        stripped,
        re.IGNORECASE,
    )
    if m:
        return f"{pretty_name(m.group('name').strip())} (+{m.group('pts')}p)"

    m = re.match(r"^(?:Enhancement|Enhancements):\s*(?P<name>.+)$", stripped, re.IGNORECASE)
    if m:
        return pretty_name(m.group("name").strip())

    return None


def extract_inline_enhancements_and_strip(text: str):
    enhancements = []

    pattern = re.compile(
        r"(?P<full>(?P<name>[A-Za-z][A-Za-z0-9'’\-\s]+?)(?:\s*\(Aura\))?\s*\[(?P<pts>\d+)\s*(?:pts?|points?|p)\])",
        re.IGNORECASE,
    )

    def replacer(match):
        enhancements.append(f"{pretty_name(match.group('name').strip())} (+{match.group('pts')}p)")
        return ""

    stripped = pattern.sub(replacer, text)
    stripped = re.sub(r"\s*,\s*,", ", ", stripped)
    stripped = re.sub(r"^\s*,\s*", "", stripped)
    stripped = re.sub(r"\s*,\s*$", "", stripped)
    stripped = re.sub(r"\s+", " ", stripped).strip()

    return enhancements, stripped


def add_weapons_from_text(counter: dict, text: str):
    if not text:
        return

    text = text.strip()

    paren_match = re.search(r"\(([^()]*)\)\s*$", text)
    if paren_match:
        inside = paren_match.group(1).strip()
        if inside:
            text = inside

    for item in split_top_level_commas(text):
        item = item.strip()
        if not item or is_ignored_weapon_item(item):
            continue

        m = re.match(r"^(?P<count>\d+)x\s+(?P<name>.+)$", item, re.IGNORECASE)
        if m:
            add_count(counter, normalize_weapon_name(m.group("name")), int(m.group("count")))
            continue

        m = re.match(r"^(?P<count>\d+)\s+(?P<name>.+)$", item, re.IGNORECASE)
        if m and not item.lower().startswith("1 with") and not item.lower().startswith("2 with"):
            add_count(counter, normalize_weapon_name(m.group("name")), int(m.group("count")))
            continue

        add_count(counter, normalize_weapon_name(item), 1)


def is_section_header(name: str) -> bool:
    lowered = name.lower().strip()
    return lowered in {
        "character", "characters", "battleline", "vehicle", "vehicles",
        "dedicated transport", "dedicated transports", "other datasheets",
        "allied units", "mounted", "monster", "monsters", "beast", "beasts",
        "infantry", "epic hero", "configuration"
    }


def is_probable_title_line(name: str, pts: int) -> bool:
    lowered = name.lower().strip()
    if pts >= 1500:
        return True
    if lowered.startswith("++ army roster ++"):
        return True
    return False


def parse_regular_formats(raw_text: str):
    raw_lines = raw_text.splitlines()
    units = []
    current_unit = None
    total_points = 0
    points_found = False
    consumed_indexes = set()

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
        "configuration",
        "battle size",
        "show/hide options",
        "army roster",
        "exported with",
        "detachment choice",
        "code chivalric",
    )

    i = 0
    while i < len(raw_lines):
        raw_line = raw_lines[i]
        stripped = raw_line.strip()

        if not stripped:
            i += 1
            continue

        lowered = stripped.lower()

        if set(stripped) == {"+"}:
            consumed_indexes.add(i)
            i += 1
            continue

        if lowered.startswith(ignored_headers):
            consumed_indexes.add(i)
            i += 1
            continue

        enh = parse_standalone_enhancement_line(stripped)
        if enh and current_unit is not None:
            current_unit["enhancements"].append(enh)
            consumed_indexes.add(i)
            i += 1
            continue

        enh = parse_bullet_enhancement_line(stripped)
        if enh and current_unit is not None:
            current_unit["enhancements"].append(enh)
            consumed_indexes.add(i)
            i += 1
            continue

        unit_match = re.match(
            r"^(?P<name>.+?)\s*[\(\[](?P<pts>\d+)\s*(?:pts?|points?|p)[\)\]]\s*:?\s*(?P<rest>.*)$",
            stripped,
            re.IGNORECASE,
        )

        if unit_match and not re.match(rf"^{BULLET_RE}", stripped):
            unit_name = clean_unit_name(unit_match.group("name"))
            pts = int(unit_match.group("pts"))
            rest = unit_match.group("rest").strip()

            if is_probable_title_line(unit_name, pts):
                consumed_indexes.add(i)
                current_unit = None
                i += 1
                continue

            if is_section_header(unit_name):
                consumed_indexes.add(i)
                current_unit = None
                i += 1
                continue

            current_unit = {
                "name": pretty_name(unit_name),
                "pts": pts,
                "enhancements": [],
                "weapons": {}
            }
            units.append(current_unit)
            total_points += pts
            points_found = True
            consumed_indexes.add(i)

            if rest:
                inline_enh, stripped_rest = extract_inline_enhancements_and_strip(rest)
                current_unit["enhancements"].extend(inline_enh)
                add_weapons_from_text(current_unit["weapons"], stripped_rest)

            i += 1
            continue

        if current_unit is None:
            i += 1
            continue

        # WTC: 1 with ...
        m = re.match(r"^(?P<count>\d+)\s+with\s+(?P<items>.+)$", stripped, re.IGNORECASE)
        if m:
            count = int(m.group("count"))
            items = m.group("items").strip()

            for item in split_top_level_commas(items):
                item = item.strip()
                if not item:
                    continue
                m2 = re.match(r"^(?P<count2>\d+)x\s+(?P<name>.+)$", item, re.IGNORECASE)
                if m2:
                    add_count(current_unit["weapons"], normalize_weapon_name(m2.group("name")), int(m2.group("count2")) * count)
                else:
                    add_count(current_unit["weapons"], normalize_weapon_name(item), count)

            consumed_indexes.add(i)
            i += 1
            continue

        # bullet line with inline colon payload
        m = re.match(rf"^{BULLET_RE}\s*(?P<count>\d+)x\s+.+?:\s*(?P<items>.+)$", stripped, re.IGNORECASE)
        if m:
            add_weapons_from_text(current_unit["weapons"], m.group("items").strip())
            consumed_indexes.add(i)
            i += 1
            continue

        # bullet line without colon
        m = re.match(rf"^{BULLET_RE}\s*(?P<count>\d+)x\s+(?P<item>.+)$", stripped, re.IGNORECASE)
        if m:
            item_count = int(m.group("count"))
            item_name = m.group("item").strip()
            current_indent = get_indent(raw_line)

            # Is this a subgroup/model header?
            next_nonempty = None
            for j in range(i + 1, len(raw_lines)):
                if raw_lines[j].strip():
                    next_nonempty = j
                    break

            is_model_header = False
            if next_nonempty is not None and get_indent(raw_lines[next_nonempty]) > current_indent:
                is_model_header = True

            if is_model_header:
                consumed_indexes.add(i)
                subgroup_indent = current_indent
                j = i + 1

                while j < len(raw_lines):
                    sub_raw = raw_lines[j]
                    sub_stripped = sub_raw.strip()

                    if not sub_stripped:
                        j += 1
                        continue

                    sub_indent = get_indent(sub_raw)
                    if sub_indent <= subgroup_indent:
                        break

                    enh_sub = parse_bullet_enhancement_line(sub_stripped)
                    if enh_sub:
                        current_unit["enhancements"].append(enh_sub)
                        consumed_indexes.add(j)
                        j += 1
                        continue

                    sub_m = re.match(rf"^{BULLET_RE}\s*(?P<wcount>\d+)x\s+(?P<witem>.+)$", sub_stripped, re.IGNORECASE)
                    if sub_m:
                        add_count(
                            current_unit["weapons"],
                            normalize_weapon_name(sub_m.group("witem")),
                            int(sub_m.group("wcount"))
                        )
                        consumed_indexes.add(j)
                        j += 1
                        continue

                    # indented continuation without bullet
                    if re.match(r"^\d+x\s+.+$", sub_stripped, re.IGNORECASE):
                        add_weapons_from_text(current_unit["weapons"], sub_stripped)
                        consumed_indexes.add(j)
                        j += 1
                        continue

                    j += 1

                i = j
                continue

            else:
                add_count(current_unit["weapons"], normalize_weapon_name(item_name), item_count)
                consumed_indexes.add(i)
                i += 1
                continue

        i += 1

    return units, total_points, points_found, consumed_indexes


def looks_like_2hg_csv(text: str) -> bool:
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    return bool(lines) and lines[0].startswith('"Warhammer 40,000 10th Edition"')


def parse_2hg_csv(text: str):
    grouped = {}
    try:
        reader = csv.reader(StringIO(text))
        for row in reader:
            if len(row) < 6:
                continue

            unit_name = row[2].strip()
            count_str = row[4].strip()
            weapons_text = row[5].strip()

            if not unit_name:
                continue

            try:
                count = int(count_str)
            except ValueError:
                count = 1

            if unit_name not in grouped:
                grouped[unit_name] = {
                    "name": pretty_name(unit_name),
                    "pts": None,
                    "enhancements": [],
                    "weapons": {}
                }

            for item in split_top_level_commas(weapons_text):
                item = item.strip()
                if item:
                    add_count(grouped[unit_name]["weapons"], normalize_weapon_name(item), count)

    except Exception:
        return [], 0, False, set()

    return list(grouped.values()), 0, False, set()


def extract_extra_text(raw_text: str, consumed_indexes: set):
    lines = raw_text.splitlines()
    extras = []

    ignored = {
        "characters", "character", "battleline", "other datasheets",
        "allied units", "configuration", "strike force"
    }

    for idx, raw_line in enumerate(lines):
        if idx in consumed_indexes:
            continue

        stripped = raw_line.strip()
        if not stripped:
            continue

        lowered = stripped.lower()

        if set(stripped) == {"+"}:
            continue
        if lowered.startswith("+ faction keyword"):
            continue
        if lowered.startswith("+ detachment"):
            continue
        if lowered.startswith("+ total army points"):
            continue
        if lowered.startswith("+ warlord"):
            continue
        if lowered.startswith("+ enhancement"):
            continue
        if lowered.startswith("+ number of units"):
            continue
        if lowered.startswith("+ secondary"):
            continue
        if lowered.startswith("faction keyword"):
            continue
        if lowered.startswith("detachment"):
            continue
        if lowered.startswith("total army points"):
            continue
        if lowered.startswith("exported with"):
            continue
        if lowered in ignored:
            continue
        if re.search(r"[\(\[]\d+\s*(pts?|points?)", stripped, re.IGNORECASE):
            continue

        extras.append(raw_line.rstrip())

    return "\n".join(extras).strip()


def shorten_warhammer_list(raw_text: str):
    metadata = parse_metadata(raw_text)

    if looks_like_2hg_csv(raw_text):
        units, total_points, points_found, consumed_indexes = parse_2hg_csv(raw_text)
    else:
        units, total_points, points_found, consumed_indexes = parse_regular_formats(raw_text)

    if not units:
        return "No valid units found.", 0, 0, False, metadata, ""

    extra_text = extract_extra_text(raw_text, consumed_indexes)
    return units, total_points, len(units), points_found, metadata, extra_text


def render_output(units, metadata, extra_text, author_name):
    blocks = []
    header = (
        f"Faction: {metadata['faction']}\n"
        f"Detachment: {metadata['detachment']}\n"
        f"Points: {metadata['points'] if metadata['points'] is not None else 'Unknown'}"
    )
    blocks.append(header)

    for unit in units:
        weapon_bits = [f"{count}x {weapon}" for weapon, count in unit["weapons"].items()]
        weapon_line = ", ".join(weapon_bits)

        if unit["enhancements"]:
            enh_text = " ; ".join(f"Enhancement: {e}" for e in unit["enhancements"])
            if weapon_line:
                weapon_line += f" [{enh_text}]"
            else:
                weapon_line = f"[{enh_text}]"

        unit_block = f"**{unit['name']}** [{unit['pts']}p]\n- {weapon_line}"
        blocks.append(unit_block)

    if extra_text:
        blocks.append(f"{author_name}:\n{extra_text}")

    return "\n\n".join(blocks)


def split_rendered_for_codeblocks(text: str, max_len: int = MAX_MESSAGE_LENGTH):
    blocks = text.split("\n\n")
    chunks = []
    current = ""

    overhead = 8  # ```text\n + \n```

    for block in blocks:
        candidate = block if not current else current + "\n\n" + block
        if len(candidate) + overhead <= max_len:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = block

    if current:
        chunks.append(current)

    return chunks


def looks_like_warhammer_list(text: str) -> bool:
    if looks_like_2hg_csv(text):
        return True

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return False

    unit_like_count = 0
    for line in lines:
        if re.match(r"^.+?[\(\[]\d+\s*(?:pts?|points?|p)[\)\]]", line, re.IGNORECASE):
            unit_like_count += 1
        elif re.match(rf"^{BULLET_RE}\s*\d+x\s+.+?:\s*.+$", line, re.IGNORECASE):
            unit_like_count += 1
        elif re.match(rf"^{BULLET_RE}\s*\d+x\s+.+$", line, re.IGNORECASE):
            unit_like_count += 1
        elif re.match(r"^\d+\s+with\s+.+$", line, re.IGNORECASE):
            unit_like_count += 1

    return unit_like_count >= 2


def contains_list_content(text: str) -> bool:
    if looks_like_2hg_csv(text):
        return True

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False

    for line in lines:
        if re.match(r"^.+?[\(\[]\d+\s*(?:pts?|points?|p)[\)\]]", line, re.IGNORECASE):
            return True
        if re.match(rf"^{BULLET_RE}?\s*Enhancement[s]?:\s*.+$", line, re.IGNORECASE):
            return True
        if re.match(r"^\d+\s+with\s+.+$", line, re.IGNORECASE):
            return True
        if re.match(rf"^{BULLET_RE}\s*\d+x\s+.+$", line, re.IGNORECASE):
            return True

    return False


async def read_text_attachments(message):
    allowed_extensions = (".txt", ".log", ".list", ".md", ".csv")
    parts = []

    for attachment in message.attachments:
        filename = attachment.filename.lower()

        if not filename.endswith(allowed_extensions):
            continue

        if attachment.size and attachment.size > MAX_ATTACHMENT_BYTES:
            continue

        try:
            data = await attachment.read()
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("latin-1", errors="ignore")

            if text.strip():
                parts.append(text)
        except Exception:
            pass

    return "\n".join(parts)


async def get_message_list_text(message):
    parts = []

    if message.content and message.content.strip():
        parts.append(message.content.strip())

    attachment_text = await read_text_attachments(message)
    if attachment_text.strip():
        parts.append(attachment_text)

    return "\n".join(parts).strip()


async def send_compacted_list(channel, rendered_text):
    chunks = split_rendered_for_codeblocks(rendered_text, MAX_MESSAGE_LENGTH)
    for chunk in chunks:
        await channel.send(f"```text\n{chunk}\n```")


async def delete_original_messages(messages):
    failed = False

    for msg in messages:
        try:
            await msg.delete()
        except discord.Forbidden:
            failed = True
        except discord.NotFound:
            pass
        except discord.HTTPException:
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
    author_name = entry["author_name"]

    parsed, total_points, unit_count, points_found, metadata, extra_text = shorten_warhammer_list(combined_text)

    if parsed != "No valid units found.":
        rendered = render_output(parsed, metadata, extra_text, author_name)
        await send_compacted_list(channel, rendered)
        failed = await delete_original_messages(original_messages)

        if failed:
            await channel.send(
                "I compacted the list, but I could not delete the original messages. "
                "Please check that I have Manage Messages permission."
            )


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

    content = await get_message_list_text(message)
    if not content:
        return

    key = (message.channel.id, message.author.id)
    author_name = message.author.display_name

    if content.startswith("!wl"):
        text = content[3:].strip()
        parsed, total_points, unit_count, points_found, metadata, extra_text = shorten_warhammer_list(text)

        if parsed != "No valid units found.":
            rendered = render_output(parsed, metadata, extra_text, author_name)
            await send_compacted_list(message.channel, rendered)
            try:
                await message.delete()
            except Exception:
                pass
        return

    if key in pending_lists:
        if contains_list_content(content):
            pending_lists[key]["parts"].append(content)
            pending_lists[key]["messages"].append(message)

            combined_text = "\n".join(pending_lists[key]["parts"])
            parsed, total_points, unit_count, points_found, metadata, extra_text = shorten_warhammer_list(combined_text)

            old_task = pending_lists[key]["task"]
            old_task.cancel()

            if points_found and total_points >= WAIT_THRESHOLD_POINTS:
                await process_pending_list(message.channel.id, message.author.id)
            else:
                pending_lists[key]["task"] = asyncio.create_task(
                    delayed_process_list(message.channel.id, message.author.id)
                )
            return

    if looks_like_warhammer_list(content):
        parsed, total_points, unit_count, points_found, metadata, extra_text = shorten_warhammer_list(content)

        if parsed == "No valid units found.":
            await bot.process_commands(message)
            return

        if points_found and total_points >= WAIT_THRESHOLD_POINTS:
            rendered = render_output(parsed, metadata, extra_text, author_name)
            await send_compacted_list(message.channel, rendered)
            try:
                await message.delete()
            except Exception:
                pass
        else:
            pending_lists[key] = {
                "parts": [content],
                "messages": [message],
                "task": asyncio.create_task(
                    delayed_process_list(message.channel.id, message.author.id)
                ),
                "author_name": author_name,
            }

    await bot.process_commands(message)


if __name__ == "__main__":
    Thread(target=run_health_server, daemon=True).start()
    bot.run(TOKEN)