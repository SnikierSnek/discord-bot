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
MAX_MESSAGE_LENGTH = 1950
MAX_ATTACHMENT_BYTES = 1_000_000

BULLET_RE = r"[•◦\-\*]"

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


def clean_unit_name(name: str) -> str:
    name = name.strip()
    name = re.sub(r"^[A-Za-z]+\d+:\s*", "", name)
    name = re.sub(r"^\d+x\s+", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[\-–—:;,]+$", "", name).strip()
    return name


def split_long_message(text: str, max_len: int = MAX_MESSAGE_LENGTH):
    chunks = []
    current = ""

    for block in text.split("\n\n"):
        candidate = block if not current else current + "\n\n" + block
        if len(candidate) <= max_len:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = block

    if current:
        chunks.append(current)

    return chunks


def add_count(counter: dict, key: str, amount: int):
    key = key.strip()
    if not key:
        return
    counter[key] = counter.get(key, 0) + amount


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
    return name


def pretty_name(text: str) -> str:
    keep_upper = {"ccw", "dw", "dwk", "las", "melta"}
    words = []

    for word in text.split():
        if word.lower() in keep_upper:
            words.append(word.upper())
            continue

        if "-" in word:
            parts = word.split("-")
            words.append("-".join(p.capitalize() if p else p for p in parts))
        else:
            if word.isupper():
                words.append(word)
            else:
                words.append(word.capitalize())

    return " ".join(words)


def is_ignored_weapon_item(item: str) -> bool:
    lowered = item.strip().lower()
    ignored = {
        "warlord",
        "strike force",
        "battleline",
        "other datasheets",
        "characters",
        "character",
        "allied units",
        "configuration",
        "epic hero",
        "mounted",
        "vehicle",
        "vehicles",
        "monster",
        "monsters",
        "beast",
        "beasts",
        "infantry",
    }
    return lowered in ignored or not lowered


def extract_inline_enhancements_and_strip(text: str):
    enhancements = []

    pattern = re.compile(
        r"(?P<full>(?P<name>[A-Za-z][A-Za-z0-9'’\-\s]+?)(?:\s*\(Aura\))?\s*\[(?P<pts>\d+)\s*(?:pts?|points?|p)\])",
        re.IGNORECASE,
    )

    def replacer(match):
        name = match.group("name").strip()
        pts = match.group("pts").strip()
        enhancements.append(f"{pretty_name(name)} (+{pts}p)")
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

    items = split_top_level_commas(text)

    for item in items:
        item = item.strip()
        if not item or is_ignored_weapon_item(item):
            continue

        m = re.match(r"^(?P<count>\d+)x\s+(?P<name>.+)$", item, re.IGNORECASE)
        if m:
            count = int(m.group("count"))
            name = pretty_name(normalize_weapon_name(m.group("name")))
            add_count(counter, name, count)
            continue

        m = re.match(r"^(?P<count>\d+)\s+(?P<name>.+)$", item, re.IGNORECASE)
        if m and not item.lower().startswith("1 with") and not item.lower().startswith("2 with"):
            count = int(m.group("count"))
            name = pretty_name(normalize_weapon_name(m.group("name")))
            add_count(counter, name, count)
            continue

        add_count(counter, pretty_name(normalize_weapon_name(item)), 1)


def parse_standalone_enhancement_line(line: str):
    stripped = line.strip()
    stripped = re.sub(rf"^{BULLET_RE}\s*", "", stripped)

    m = re.match(
        r"^Enhancement[s]?:\s*(?P<name>.+?)\s*\(\+(?P<pts>\d+)\s*(?:pts?|points?|p)\)\s*$",
        stripped,
        re.IGNORECASE,
    )
    if m:
        enh_name = re.sub(r"\s*\(Aura\)\s*$", "", m.group("name").strip(), flags=re.IGNORECASE)
        enh_pts = m.group("pts").strip()
        return f"{pretty_name(enh_name)} (+{enh_pts}p)"

    m = re.match(r"^Enhancement[s]?:\s*(?P<name>.+?)\s*$", stripped, re.IGNORECASE)
    if m:
        enh_name = re.sub(r"\s*\(Aura\)\s*$", "", m.group("name").strip(), flags=re.IGNORECASE)
        return pretty_name(enh_name)

    return None


def parse_bullet_enhancement_line(stripped: str):
    text = re.sub(rf"^{BULLET_RE}\s*", "", stripped).strip()

    m = re.match(
        r"^(?P<name>.+?)\s*\(\+(?P<pts>\d+)\s*(?:pts?|points?|p)\)\s*$",
        text,
        re.IGNORECASE,
    )
    if m:
        name = re.sub(r"\s*\(Aura\)\s*$", "", m.group("name").strip(), flags=re.IGNORECASE)
        pts = m.group("pts").strip()
        return f"{pretty_name(name)} (+{pts}p)"

    m = re.match(
        r"^(?:Enhancements?|Enhancement):\s*(?P<name>.+)$",
        text,
        re.IGNORECASE,
    )
    if m:
        return pretty_name(m.group("name").strip())

    return None


def get_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def is_probable_title_line(name: str, pts: int) -> bool:
    lowered = name.lower().strip()
    if pts >= 1500:
        return True
    if lowered.startswith("++ army roster ++"):
        return True
    return False


def parse_metadata(raw_text: str):
    lines = raw_text.splitlines()

    faction = None
    detachment = None
    total_points = None

    for line in lines:
        stripped = line.strip()

        m = re.match(r"^\+\s*FACTION KEYWORD:\s*(.+)$", stripped, re.IGNORECASE)
        if m and not faction:
            faction = m.group(1).strip()

        m = re.match(r"^(?:Detachment:|\+\s*DETACHMENT:)\s*(.+)$", stripped, re.IGNORECASE)
        if m and not detachment:
            detachment = m.group(1).strip()

        m = re.match(r"^(?:\+\s*TOTAL ARMY POINTS:)\s*(\d+)\s*pts?$", stripped, re.IGNORECASE)
        if m and not total_points:
            total_points = int(m.group(1))

    if total_points is None:
        for line in lines:
            stripped = line.strip()

            m = re.search(r"\((\d+)\s*Points?\)", stripped, re.IGNORECASE)
            if m:
                value = int(m.group(1))
                if value >= 1000:
                    total_points = value
                    break

            m = re.search(r"\[(\d+)\s*pts?\]", stripped, re.IGNORECASE)
            if m:
                value = int(m.group(1))
                if value >= 1000:
                    total_points = value
                    break

    if faction is None or detachment is None:
        nonempty = [x.strip() for x in lines if x.strip()]
        for idx, line in enumerate(nonempty):
            if re.match(r"^Strike Force\s*\(\d+\s*Points?\)$", line, re.IGNORECASE):
                if idx >= 2 and faction is None:
                    faction = nonempty[idx - 2]
                if idx + 1 < len(nonempty) and detachment is None:
                    detachment = nonempty[idx + 1]
                break

    if detachment:
        detachment = re.sub(r"\s*\([^)]*\)\s*$", "", detachment).strip()

    return {
        "faction": faction or "Unknown",
        "detachment": detachment or "Unknown",
        "total_points": total_points
    }


def parse_regular_formats(raw_text: str):
    raw_lines = raw_text.splitlines()
    results = []
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

    ignored_section_names = {
        "epic hero",
        "character",
        "characters",
        "battleline",
        "mounted",
        "beast",
        "beasts",
        "vehicle",
        "vehicles",
        "monster",
        "monsters",
        "infantry",
        "dedicated transports",
        "other datasheets",
        "allied units",
        "configuration",
    }

    i = 0
    while i < len(raw_lines):
        raw_line = raw_lines[i]
        stripped = raw_line.strip()

        if not stripped:
            i += 1
            continue

        lower_line = stripped.lower()

        if set(stripped) == {"+"}:
            consumed_indexes.add(i)
            i += 1
            continue

        if lower_line.startswith(ignored_headers):
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

            if unit_name.lower() in ignored_section_names:
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
            results.append(current_unit)
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

        m = re.match(r"^(?P<count>\d+)\s+with\s+(?P<items>.+)$", stripped, re.IGNORECASE)
        if m:
            count = int(m.group("count"))
            items = m.group("items").strip()

            split_items = split_top_level_commas(items)
            for item in split_items:
                item = item.strip()
                if not item:
                    continue

                m2 = re.match(r"^(?P<count2>\d+)x\s+(?P<name>.+)$", item, re.IGNORECASE)
                if m2:
                    add_count(current_unit["weapons"], pretty_name(normalize_weapon_name(m2.group("name"))), int(m2.group("count2")) * count)
                else:
                    add_count(current_unit["weapons"], pretty_name(normalize_weapon_name(item)), count)

            consumed_indexes.add(i)
            i += 1
            continue

        m = re.match(rf"^{BULLET_RE}\s*(?P<count>\d+)x\s+.+?:\s*(?P<items>.+)$", stripped, re.IGNORECASE)
        if m:
            items = m.group("items").strip()
            add_weapons_from_text(current_unit["weapons"], items)
            consumed_indexes.add(i)
            i += 1
            continue

        m = re.match(rf"^{BULLET_RE}\s*(?P<count>\d+)x\s+(?P<item>.+)$", stripped, re.IGNORECASE)
        if m:
            item = m.group("item").strip()
            current_indent = get_indent(raw_line)

            next_index = i + 1
            next_nonempty_index = None

            while next_index < len(raw_lines):
                if raw_lines[next_index].strip():
                    next_nonempty_index = next_index
                    break
                next_index += 1

            is_model_header = False
            if next_nonempty_index is not None:
                next_raw = raw_lines[next_nonempty_index]
                next_indent = get_indent(next_raw)
                if next_indent > current_indent:
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
                        weapon_count = int(sub_m.group("wcount"))
                        weapon_item = sub_m.group("witem").strip()

                        add_count(
                            current_unit["weapons"],
                            pretty_name(normalize_weapon_name(weapon_item)),
                            weapon_count
                        )
                        consumed_indexes.add(j)
                        j += 1
                        continue

                    if re.match(r"^\d+x\s+.+$", sub_stripped, re.IGNORECASE):
                        add_weapons_from_text(current_unit["weapons"], sub_stripped)
                        consumed_indexes.add(j)
                        j += 1
                        continue

                    j += 1

                i = j
                continue

            else:
                add_count(
                    current_unit["weapons"],
                    pretty_name(normalize_weapon_name(item)),
                    int(m.group("count"))
                )
                consumed_indexes.add(i)
                i += 1
                continue

        i += 1

    return results, total_points, points_found, consumed_indexes


def looks_like_2hg_csv(text: str) -> bool:
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    if not lines:
        return False
    return lines[0].startswith('"Warhammer 40,000 10th Edition"')


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

            split_items = split_top_level_commas(weapons_text)
            for item in split_items:
                item = item.strip()
                if not item:
                    continue
                add_count(grouped[unit_name]["weapons"], pretty_name(normalize_weapon_name(item)), count)

    except Exception as e:
        print(f"Failed to parse 2HG CSV: {e}")
        return [], 0, False, set()

    return list(grouped.values()), 0, False, set()


def extract_extra_text(raw_text: str, consumed_indexes: set):
    lines = raw_text.splitlines()
    extras = []

    ignored = (
        "characters",
        "character",
        "battleline",
        "other datasheets",
        "allied units",
        "configuration",
        "strike force",
    )

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
            print(f"Skipping large attachment: {attachment.filename} ({attachment.size} bytes)")
            continue

        try:
            data = await attachment.read()
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("latin-1", errors="ignore")

            if text.strip():
                parts.append(text)
                print(f"Read attachment: {attachment.filename}")

        except Exception as e:
            print(f"Failed to read attachment {attachment.filename}: {e}")

    return "\n".join(parts)


async def get_message_list_text(message):
    parts = []

    if message.content and message.content.strip():
        parts.append(message.content.strip())

    attachment_text = await read_text_attachments(message)
    if attachment_text.strip():
        parts.append(attachment_text)

    return "\n".join(parts).strip()


def render_output(units, metadata, extra_text, author_name):
    blocks = []

    header_lines = [
        f"Faction: {metadata['faction']}",
        f"Detachment: {metadata['detachment']}",
        f"Points: {metadata['total_points'] if metadata['total_points'] is not None else 'Unknown'}",
    ]
    blocks.append("\n".join(header_lines))

    for unit in units:
        unit_lines = [f"**{unit['name']}** [{unit['pts']}p]"]

        weapon_bits = [f"{count}x {weapon}" for weapon, count in unit["weapons"].items()]
	weapon_line = "- " + ", ".join(weapon_bits)
        if unit["enhancements"]:
            enh_text = " ; ".join(f"Enhancement: {x}" for x in unit["enhancements"])
            weapon_line += f" [{enh_text}]"

        unit_lines.append(weapon_line)
        blocks.append("\n".join(unit_lines))

    if extra_text:
        blocks.append(f"{author_name}:\n{extra_text}")

    return "\n\n".join(blocks)


async def send_compacted_list(channel, rendered_text):
    chunks = split_long_message(rendered_text, MAX_MESSAGE_LENGTH)
    for chunk in chunks:
        await channel.send(chunk)


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
            except discord.Forbidden:
                await message.channel.send(
                    "I compacted the list, but I could not delete the original command message. "
                    "Please check that I have Manage Messages permission."
                )
            except discord.NotFound:
                pass
            except discord.HTTPException as e:
                print(f"Could not delete command message: {e}")
                await message.channel.send(
                    "I compacted the list, but I could not delete the original command message."
                )
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
                new_task = asyncio.create_task(
                    delayed_process_list(message.channel.id, message.author.id)
                )
                pending_lists[key]["task"] = new_task
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
            except discord.Forbidden:
                await message.channel.send(
                    "I compacted the list, but I could not delete the original message. "
                    "Please check that I have Manage Messages permission."
                )
            except discord.NotFound:
                pass
            except discord.HTTPException as e:
                print(f"Could not delete message: {e}")
                await message.channel.send(
                    "I compacted the list, but I could not delete the original message."
                )
        else:
            task = asyncio.create_task(
                delayed_process_list(message.channel.id, message.author.id)
            )
            pending_lists[key] = {
                "parts": [content],
                "messages": [message],
                "task": task,
                "author_name": author_name,
            }

    await bot.process_commands(message)


if __name__ == "__main__":
    Thread(target=run_health_server, daemon=True).start()
    bot.run(TOKEN)