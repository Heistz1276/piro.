"""URA | DIV: role-based division MVP tracker + live ticket tracker.

Needs: discord.py >= 2.6 (Components V2) and python-dotenv. Pillow is NOT needed.
Badges: the five built-in division badges live in badges_data.py (created automatically on the
first start from your previous bot file if it is in the same folder). A file in ./badges overrides them.
The fixed leaderboard uses its own five badge images + a crown emoji, both built into this file.
(The crown is uploaded once as an application emoji on the first start; no setup needed.)

Channels
  LOG_CHANNEL_ID          -> the division MVP log (one post per MVP log, edited when recounted)
  LEADERBOARD_CHANNEL_ID  -> the fixed live division leaderboard (one post, edited after every log)
  LIVE_POST_CHANNEL_ID    -> live ticket posts (one per ticket, edited as helpers join)
  LIVE_CATEGORY_ID        -> the category where tickets are opened. EVERY new channel created in it
                             is a ticket, except the base channels listed in LIVE_IGNORE_IDS.

Access
  Only OWNER_ID and users added with /trust add can use commands.
  Only OWNER_ID can press the Edit button on an MVP log or use /trust, /backup, /restore.
"""
import ast
import asyncio
import base64
import io
import json
import os
import platform
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env", override=True)   # the .env next to this file always wins


def env_ids(name: str) -> set[int]:
    return {int(x) for x in re.split(r"[,\s]+", os.getenv(name, "")) if x.isdigit()}


def env_int(name: str, default: int = 0) -> int:
    """Return the .env number, or the default when the variable is missing or 0."""
    try:
        value = int(os.getenv(name, "0") or 0)
    except ValueError:
        value = 0
    return value or default


# ----------------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------------
BOT_NAME = "URA | DIV"
TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = env_int("GUILD_ID")
OWNER_ID = env_int("OWNER_ID", 1455426573741330495)

LOG_ID = env_int("LOG_CHANNEL_ID", 1565771359097716766)                   # division MVP logs
LEADERBOARD_ID = env_int("LEADERBOARD_CHANNEL_ID", 1565732417271234791)   # fixed leaderboard
LIVE_POST_ID = env_int("LIVE_POST_CHANNEL_ID", env_int("RAID_UPDATE_CHANNEL_ID", 1565732391107170364))  # live ticket posts
MVP_BOT_ID = env_int("MVP_BOT_ID", 1563126014702395542)                   # "Raid MVPs" app
SOURCE_IDS = env_ids("MVP_CHANNEL_IDS") or {1524379574585000006}          # where MVP logs are posted
MVP_CHANNEL_ID = sorted(SOURCE_IDS)[0]

LIVE_CATEGORY_IDS = env_ids("LIVE_CATEGORY_ID") or {1507716131551707258}  # tickets are created inside this category
LIVE_IGNORE_IDS = env_ids("LIVE_IGNORE_IDS") or {1524379571946913862, 1562782175927992340}   # base channels, NOT tickets
LIVE_BOT_IDS = env_ids("LIVE_BOT_IDS") or {1470577242676531372, 1563126014702395542}   # bots whose ticket messages are read
LIVE_SYNC_HOURS = env_int("LIVE_SYNC_HOURS", 12)    # after a restart, pick up tickets created in the last N hours

RAID_START_NUMBER = env_int("RAID_START_NUMBER", 1)
VERSION = "3.3-live"
_script_dir = Path(__file__).resolve().parent
_db = Path(os.getenv("PIRO_DB_PATH", "piro.sqlite3"))
DB_PATH = str(_db if _db.is_absolute() else _script_dir / _db)      # always next to this file, whatever the working folder
BACKUP_DIR = Path(os.getenv("PIRO_BACKUP_DIR", str(_script_dir / "backups")))
KEEP_BACKUPS = 48
BADGE_DIR = Path(os.getenv("PIRO_BADGE_DIR", str(_script_dir / "badges")))

DEFAULT_DIVISION_ROLES = {
    1: 1517694951650103427,
    2: 1517694955835883682,
    3: 1517694959291994132,
    4: 1517694962119213056,
    5: 1517694969538805831,
}
DIVISION_ROLES = {d: env_int(f"DIVISION_{d}_ROLE_ID", DEFAULT_DIVISION_ROLES[d]) for d in range(1, 6)}
DIVISION_ROLE_IDS = {role_id: d for d, role_id in DIVISION_ROLES.items()}
# Fallback when a role ID is not matched: any role whose name contains "Division 1" ... "Division 5".
DIVISION_NAME_RE = re.compile(r"\bdivision\s*[-_ ]?0?([1-5])\b", re.I)


def clean_image_url(d: int) -> str:
    """Only real http(s) links are usable. A blob: link (copied from a browser tab) only works in
    that tab, so it is ignored and the built-in badge is attached instead."""
    value = os.getenv(f"DIVISION_{d}_IMAGE_URL", "").strip().strip('"').strip("'")
    if value and not re.match(r"https?://", value, re.I):
        print(f"DIVISION_{d}_IMAGE_URL is not a real https link ({value[:40]}...); ignoring it.")
        return ""
    return value


DIVISION_IMAGE_URLS = {d: clean_image_url(d) for d in range(1, 6)}

BRAND_COLOR = 0x5865F2
DIVISION_COLORS = {1: 0xF5C542, 2: 0x3B82F6, 3: 0x22C55E, 4: 0xA855F7, 5: 0xEF4444}
DIVISION_EMOJI = {1: "\U0001F7E8", 2: "\U0001F7E6", 3: "\U0001F7E9", 4: "\U0001F7EA", 5: "\U0001F7E5"}
RAID_RE = re.compile(r"\bRaid\b[\s`*_:\-]*#\s*0*(\d+)", re.I)
MENTION_RE = re.compile(r"<@!?(\d+)>")
CHANNEL_MENTION_RE = re.compile(r"<#(\d+)>")
OPENED_RE = re.compile(r"Ticket opened by\s*<@!?(\d+)>", re.I)
LIVE_TOTAL_RE = re.compile(r"Live Helpers\D{0,12}(\d+)", re.I)
HELP_RE = re.compile(r"Help Registered|confirmed for help", re.I)
HELP_MENTION_RE = re.compile(r"<@!?(\d+)>\s*has confirmed for help", re.I)   # "@member has confirmed for help."
NO_PING = discord.AllowedMentions.none()
START_TIME = time.time()

# ----------------------------------------------------------------------------
# DATABASE
# ----------------------------------------------------------------------------
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.execute("PRAGMA synchronous=FULL")        # flush every write to disk before reporting success
db.execute("PRAGMA busy_timeout=5000")


def ensure_schema() -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS raid_results (
        message_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
        raid_number INTEGER NOT NULL, raiders_json TEXT NOT NULL, tallies_json TEXT NOT NULL,
        winners_json TEXT NOT NULL, created_at TEXT NOT NULL, update_message_id INTEGER
    )""")
    for _col in ("update_ref", "log_ref", "skipped_json"):
        try:
            db.execute(f"ALTER TABLE raid_results ADD COLUMN {_col} TEXT")
        except sqlite3.OperationalError:
            pass
    try:   # counted = 1 -> this raid's MVP counts on the fixed leaderboard; 0 -> kept in history only
        db.execute("ALTER TABLE raid_results ADD COLUMN counted INTEGER NOT NULL DEFAULT 1")
    except sqlite3.OperationalError:
        pass
    db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS trusted (user_id INTEGER PRIMARY KEY)")
    db.execute("""CREATE TABLE IF NOT EXISTS live_tickets (
        channel_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, name TEXT NOT NULL,
        opener_id INTEGER, opener_division INTEGER, reported_total INTEGER,
        helpers_json TEXT NOT NULL DEFAULT '[]', post_ref TEXT, status TEXT NOT NULL DEFAULT 'live',
        created_at TEXT NOT NULL, ended_at TEXT
    )""")
    db.commit()


ensure_schema()


def is_trusted(user_id: int) -> bool:
    return db.execute("SELECT 1 FROM trusted WHERE user_id=?", (user_id,)).fetchone() is not None


def get_setting(key: str) -> str | None:
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))
    db.commit()


# ----------------------------------------------------------------------------
# BOT (every slash command is locked to the owner and trusted users)
# ----------------------------------------------------------------------------
OWNER_ONLY_COMMANDS = {"trust", "backup", "restore"}


class GuardedTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        uid = interaction.user.id
        if uid == OWNER_ID:
            return True
        command = interaction.command
        root = command.root_parent.name if command is not None and command.root_parent else getattr(command, "name", "")
        if root not in OWNER_ONLY_COMMANDS and is_trusted(uid):
            return True
        raise app_commands.CheckFailure("not allowed")


intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents, help_command=None,
                   tree_cls=GuardedTree)

PROCESS_LOCK = asyncio.Lock()
LIVE_LOCKS: dict[int, asyncio.Lock] = {}
LB_LOCK = asyncio.Lock()                 # only one leaderboard refresh at a time (no double posts)
LB_LAST_PRESS = 0.0
LIVE_SEEN: dict[int, str] = {}           # bots seen in ticket channels that are NOT in LIVE_BOT_IDS
IMAGE_PROBLEM: str | None = None         # last reason Discord rejected the badge images
STARTED = False


# ----------------------------------------------------------------------------
# DIVISION BADGES (five separate images, built in; ./badges/divisionN.png overrides them)
# ----------------------------------------------------------------------------
BADGES: dict[int, bytes] = {}
BADGE_NAMES: dict[int, str] = {}         # division -> attachment filename (keeps the real extension)
BADGE_SOURCE: dict[int, str] = {}        # division -> "file" or "built-in" (for diagnostics)


def find_badge_file(d: int) -> Path | None:
    """Find a custom badge in ./badges: division1.png, Division_1.jpg, division-1.webp ..."""
    if not BADGE_DIR.is_dir():
        return None
    pattern = re.compile(rf"^division[\s_-]*0?{d}\.(png|jpe?g|webp|gif)$", re.I)
    for path in sorted(BADGE_DIR.iterdir()):
        if path.is_file() and pattern.match(path.name) and path.stat().st_size > 100:
            return path
    return None


def find_embedded_badges() -> dict[int, str]:
    """Built-in badges (base64). Order: badges_data.py next to this file; otherwise copy them out of
    an older bot file in the same folder (the block `EMBEDDED_BADGES = {...}`) and save badges_data.py."""
    if str(_script_dir) not in sys.path:
        sys.path.insert(0, str(_script_dir))
    try:
        from badges_data import EMBEDDED_BADGES as data
        return {int(k): str(v) for k, v in dict(data).items()}
    except Exception:
        pass
    this_file = Path(__file__).resolve()
    for path in sorted(_script_dir.glob("*.py")):
        if path.resolve() == this_file or path.name == "badges_data.py":
            continue
        try:
            text = path.read_text(encoding="utf-8")
            start = text.find("EMBEDDED_BADGES = {")
            if start == -1:
                continue
            end = text.index("\n}", start) + 2
            data = ast.literal_eval(text[start + len("EMBEDDED_BADGES = "):end])
            data = {int(k): str(v) for k, v in data.items()}
            (_script_dir / "badges_data.py").write_text("EMBEDDED_BADGES = " + repr(data) + "\n", encoding="utf-8")
            print(f"Built-in badges copied from {path.name} into badges_data.py")
            return data
        except Exception as exc:
            print(f"Could not read badges from {path.name}: {exc!r}")
    print("No built-in badges found (badges_data.py missing). Put divisionN.png files in ./badges, or keep your old bot file in this folder once.")
    return {}


def load_badges() -> None:
    embedded = find_embedded_badges()
    load_icons()
    BADGES.clear()
    BADGE_NAMES.clear()
    BADGE_SOURCE.clear()
    for d in range(1, 6):
        path = find_badge_file(d)
        if path is not None:
            BADGES[d] = path.read_bytes()
            BADGE_NAMES[d] = f"division{d}{path.suffix.lower()}"
            BADGE_SOURCE[d] = "file"
        elif d in embedded:
            BADGES[d] = base64.b64decode(embedded[d])
            BADGE_NAMES[d] = f"division{d}.png"
            BADGE_SOURCE[d] = "built-in"
    print(f"Badges: {len(BADGES)}/5 loaded  (custom files: {sum(1 for v in BADGE_SOURCE.values() if v == 'file')}, "
          f"built-in: {sum(1 for v in BADGE_SOURCE.values() if v == 'built-in')})")


# ---- fixed-leaderboard art: five division badges + the crown that sits before every count ----
ICONS: dict[int, bytes] = {}
CROWN_EMOJI = "\U0001F451"               # plain crown until the custom one is ready (or if it cannot be uploaded)
CROWN_NAME = "ura_crown"


def load_icons() -> None:
    ICONS.clear()
    for d, data in EMBEDDED_ICONS.items():
        ICONS[int(d)] = base64.b64decode(data)


def icon_ref(d: int) -> str | None:
    return f"attachment://div{d}.png" if d in ICONS else None


def icon_files() -> list[discord.File]:
    return [discord.File(io.BytesIO(ICONS[d]), filename=f"div{d}.png") for d in range(1, 6) if d in ICONS]


async def ensure_crown_emoji() -> None:
    """Upload the crown once as an application emoji (usable in any server the bot is in)."""
    global CROWN_EMOJI
    try:
        for emoji in await bot.fetch_application_emojis():
            if emoji.name == CROWN_NAME:
                CROWN_EMOJI = str(emoji)
                print(f"Crown emoji ready: {CROWN_EMOJI}")
                return
        emoji = await bot.create_application_emoji(name=CROWN_NAME, image=base64.b64decode(EMBEDDED_CROWN))
        CROWN_EMOJI = str(emoji)
        print(f"Crown emoji uploaded: {CROWN_EMOJI}")
    except Exception as exc:
        print(f"Could not set up the crown emoji ({exc!r}); using the standard crown instead.")


def badge_ref(d: int) -> str | None:
    if DIVISION_IMAGE_URLS[d]:
        return DIVISION_IMAGE_URLS[d]
    return f"attachment://{BADGE_NAMES[d]}" if d in BADGES else None


def badge_files() -> list[discord.File]:
    """Files are single-use, so call this again for every send."""
    return [discord.File(io.BytesIO(BADGES[d]), filename=BADGE_NAMES[d])
            for d in range(1, 6) if d in BADGES and not DIVISION_IMAGE_URLS[d]]


def division_section(text: str, d: int, images: bool = True):
    ref = badge_ref(d) if images else None
    if ref:
        return discord.ui.Section(text, accessory=discord.ui.Thumbnail(ref))
    return discord.ui.TextDisplay(text)


# ----------------------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------------------
def make_embed(title: str, description: str | None = None, color: int = BRAND_COLOR) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    icon = bot.user.display_avatar.url if bot.user else None
    embed.set_footer(text=BOT_NAME, icon_url=icon)
    return embed


def division_of(member: discord.Member) -> tuple[int | None, set[int]]:
    """Return (division, every division found). Role ID is checked first, then the role name.
    If a member holds several division roles, the one with the highest role position wins."""
    found: dict[int, int] = {}
    for role in member.roles:
        d = DIVISION_ROLE_IDS.get(role.id)
        if d is None:
            match = DIVISION_NAME_RE.search(role.name)
            d = int(match.group(1)) if match else None
        if d:
            found[d] = max(found.get(d, -1), role.position)
    if not found:
        return None, set()
    return max(found, key=lambda d: found[d]), set(found)


def component_text(components) -> list[str]:
    out: list[str] = []

    def walk(node):
        content = getattr(node, "content", None)
        if isinstance(content, str):
            out.append(content)
        for attr in ("children", "items"):
            for child in getattr(node, attr, None) or []:
                walk(child)
        accessory = getattr(node, "accessory", None)
        if accessory is not None:
            walk(accessory)

    try:
        for component in components or []:
            walk(component)
    except Exception:
        pass
    return out


def post_text(message: discord.Message) -> str:
    parts = [message.content]
    for embed in message.embeds:
        parts += [embed.title or "", embed.description or ""]
        parts += [f"{f.name} {f.value}" for f in embed.fields]
    parts += component_text(getattr(message, "components", None))
    return " ".join(parts)


def is_mvp_post(message: discord.Message) -> bool:
    if not message.author.bot:
        return False
    if MVP_BOT_ID and message.author.id != MVP_BOT_ID:
        return False
    text = post_text(message)
    return bool(re.search(r"\bMVPs?\b", text, re.I) and re.search(r"\bRaid\b", text, re.I))


def source_message(message: discord.Message) -> bool:
    return message.guild is not None and message.channel.id in SOURCE_IDS


def source_db_filter() -> tuple[str, tuple[int, ...]]:
    ids = tuple(sorted(SOURCE_IDS))
    return f"channel_id IN ({','.join('?' for _ in ids)})", ids


def channel_url(guild_id: int, channel_id: int, message_id: int | None = None) -> str:
    base = f"https://discord.com/channels/{guild_id}/{channel_id}"
    return f"{base}/{message_id}" if message_id else base


def ref_url(guild_id: int, ref: str | None, fallback_channel: int) -> str:
    if ref and ":" in ref:
        cid, mid = ref.split(":", 1)
        return channel_url(guild_id, int(cid), int(mid))
    return channel_url(guild_id, fallback_channel)


def tally(raiders: list[dict]) -> tuple[dict[int, int], list[int]]:
    tallies = {d: 0 for d in range(1, 6)}
    for raider in raiders:
        tallies[int(raider["division"])] += 1
    high = max(tallies.values())
    winners = [d for d, n in tallies.items() if high > 0 and n == high]
    return tallies, winners


def next_raid_number(guild_id: int) -> int:
    clause, channel_params = source_db_filter()
    row = db.execute(f"SELECT MAX(raid_number) AS m FROM raid_results WHERE guild_id=? AND {clause}",
                     (guild_id, *channel_params)).fetchone()
    return RAID_START_NUMBER if row["m"] is None else int(row["m"]) + 1


def division_totals(guild_id: int) -> tuple[dict[int, int], int]:
    """Leaderboard points: every MVP log gives exactly 1 point to each division it declares MVP.
    Raids with counted=0 (cleared by the leaderboard reset) stay in history but give no points."""
    totals = {d: 0 for d in range(1, 6)}
    clause, channel_params = source_db_filter()
    params = (guild_id, *channel_params)
    for row in db.execute(f"SELECT winners_json FROM raid_results WHERE guild_id=? AND counted=1 AND {clause}", params):
        for division in json.loads(row["winners_json"]):
            totals[int(division)] += 1
    raids = db.execute(f"SELECT COUNT(*) AS n FROM raid_results WHERE guild_id=? AND {clause}", params).fetchone()["n"]
    return totals, int(raids)


def fit_list(items: list[str], limit: int) -> str:
    """Join items with commas without going over `limit` characters."""
    out, used = [], 0
    for i, item in enumerate(items):
        if used + len(item) + 2 > limit:
            out.append(f"+{len(items) - i} more")
            break
        out.append(item)
        used += len(item) + 2
    return ", ".join(out)


async def get_channel(channel_id: int):
    if not channel_id:
        return None
    try:
        return bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    except discord.HTTPException:
        return None


async def try_get_message(channel_id: int, message_id: int):
    channel = await get_channel(channel_id)
    if channel is None or not hasattr(channel, "fetch_message"):
        return None
    try:
        return await channel.fetch_message(message_id)
    except discord.HTTPException:
        return None


async def get_member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.HTTPException:
            return None
    return member


def is_bad_form(exc: discord.HTTPException) -> bool:
    return exc.status == 400 and getattr(exc, "code", None) == 50035


def note_image_problem(exc: discord.HTTPException) -> None:
    global IMAGE_PROBLEM
    IMAGE_PROBLEM = str(getattr(exc, "text", "") or exc)[:300]
    print(f"Discord rejected the badge images ({IMAGE_PROBLEM}); sending without thumbnails.")


async def _deliver(channel, ref: str | None, view, files: list) -> str:
    if ref and ":" in ref:
        cid, mid = ref.split(":", 1)
        if int(cid) == channel.id:
            try:
                old = await channel.fetch_message(int(mid))
            except discord.NotFound:
                old = None            # really deleted -> post a new one; any other error is raised, never re-posts
            if old is not None:
                await old.edit(view=view, attachments=files)
                return ref
    sent = await channel.send(view=view, files=files, allowed_mentions=NO_PING)
    return f"{channel.id}:{sent.id}"


async def send_or_edit(channel_id: int, ref: str | None, build, with_files: bool = True, files_fn=None) -> str | None:
    """Edit the message saved in `ref` ("channel:message"); send a new one if it is gone.

    `build(images)` returns a fresh view. If Discord rejects the badge images (error 50035),
    the post is sent again without thumbnails instead of failing.
    """
    channel = await get_channel(channel_id)
    if channel is None:
        print(f"Channel {channel_id} is not reachable.")
        return ref
    for images in ((True, False) if with_files else (False,)):
        try:
            return await _deliver(channel, ref, build(images), (files_fn or badge_files)() if images else [])
        except discord.HTTPException as exc:
            if images and is_bad_form(exc):
                note_image_problem(exc)
                continue
            raise
    return ref


async def post_new(channel_id: int, build) -> discord.Message | None:
    """Send a brand-new view to a specific channel. Falls back to no thumbnails if Discord rejects the images."""
    channel = await get_channel(channel_id)
    if channel is None:
        return None
    for images in (True, False):
        try:
            return await channel.send(view=build(images), files=badge_files() if images else [],
                                      allowed_mentions=NO_PING)
        except discord.HTTPException as exc:
            if images and is_bad_form(exc):
                note_image_problem(exc)
                continue
            print(f"Could not post in {channel_id}: {exc!r}")
            return None
    return None


async def send_private_leaderboard(send, totals: dict[int, int], history_url: str, test: bool = False) -> None:
    """Show the leaderboard only to the person who asked. `send` is interaction.response.send_message
    or interaction.followup.send. Falls back to no thumbnails if Discord rejects the images."""
    try:
        await send(view=LeaderboardView(totals, history_url, True, test), files=icon_files(), ephemeral=True)
    except discord.HTTPException as exc:
        if not is_bad_form(exc):
            raise
        note_image_problem(exc)
        await send(view=LeaderboardView(totals, history_url, False, test), ephemeral=True)


async def delete_ref(ref: str | None) -> None:
    if not ref or ":" not in ref:
        return
    cid, mid = ref.split(":", 1)
    msg = await try_get_message(int(cid), int(mid))
    if msg:
        try:
            await msg.delete()
        except discord.HTTPException:
            pass


async def collect_raiders(message: discord.Message):
    """Read every member pinged in an MVP log and sort them into divisions by role.

    Returns (raiders, skipped). Every pinged human counts. A member holding several
    division roles is counted once, for the highest one. skipped is a list of
    [mention, reason] for pings that could not be counted.
    """
    ids = [int(x) for x in MENTION_RE.findall(post_text(message))] + [u.id for u in message.mentions]
    raiders, skipped = [], []
    for user_id in dict.fromkeys(ids):
        if user_id == message.author.id:
            continue
        member = await get_member(message.guild, user_id)
        if member is None:
            skipped.append([f"<@{user_id}>", "not in the server"])
            continue
        if member.bot:
            continue
        division, found = division_of(member)
        if division is None:
            skipped.append([f"<@{user_id}>", "no division role"])
        else:
            raiders.append({"user_id": member.id, "display_name": member.display_name,
                            "division": division, "multi": len(found) > 1})
    return raiders, skipped


# ----------------------------------------------------------------------------
# EDIT BUTTON + MODAL (owner only)
# ----------------------------------------------------------------------------
class RaidEditModal(discord.ui.Modal):
    def __init__(self, raid_number: int):
        super().__init__(title=f"Edit Raid #{raid_number:04d}")
        self.raid_number = raid_number
        self.add_field = discord.ui.TextInput(
            label="Add raiders (mentions or IDs)", style=discord.TextStyle.paragraph,
            required=False, max_length=1000, placeholder="@member 123456789012345678 ...")
        self.remove_field = discord.ui.TextInput(
            label="Remove raiders (mentions or IDs)", style=discord.TextStyle.paragraph,
            required=False, max_length=1000, placeholder="@member 123456789012345678 ...")
        self.add_item(self.add_field)
        self.add_item(self.remove_field)

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID or interaction.guild is None:
            await interaction.response.send_message("Only the owner can edit this.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        to_add = [int(x) for x in re.findall(r"\d{15,21}", self.add_field.value or "")]
        to_remove = [int(x) for x in re.findall(r"\d{15,21}", self.remove_field.value or "")]
        text = await apply_edit(interaction.guild, self.raid_number, to_add, to_remove)
        await interaction.followup.send(embed=make_embed("Raid edited", text, 0x57F287), ephemeral=True)


class EditButton(discord.ui.Button):
    def __init__(self, raid_number: int):
        super().__init__(label="Edit", style=discord.ButtonStyle.secondary,
                         custom_id=f"urdiv:edit:{raid_number}")
        self.raid_number = raid_number

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("Only the owner can use Edit.", ephemeral=True)
            return
        await interaction.response.send_modal(RaidEditModal(self.raid_number))


class LeaderboardButton(discord.ui.Button):
    """Opens the live leaderboard privately for whoever clicks it."""

    def __init__(self):
        super().__init__(label="Live Leaderboard", style=discord.ButtonStyle.secondary,
                         custom_id="urdiv:leaderboard")

    async def callback(self, interaction: discord.Interaction):
        if interaction.guild_id is None:
            return
        totals, _ = division_totals(interaction.guild_id)
        await send_private_leaderboard(interaction.response.send_message, totals,
                                       channel_url(interaction.guild_id, LOG_ID))


class LeaderboardRefreshButton(discord.ui.Button):
    """Refreshes the fixed leaderboard in place (edits the same post, never posts a new one)."""

    def __init__(self):
        super().__init__(label="Refresh", emoji="\U0001F504", style=discord.ButtonStyle.secondary,
                         custom_id="urdiv:lb_refresh")

    async def callback(self, interaction: discord.Interaction):
        global LB_LAST_PRESS
        if interaction.guild is None:
            return
        await interaction.response.defer()
        if time.time() - LB_LAST_PRESS < 5:       # small cooldown against button spam
            return
        LB_LAST_PRESS = time.time()
        await refresh_leaderboard(interaction.guild)


# ----------------------------------------------------------------------------
# LAYOUTS (Components V2)
# ----------------------------------------------------------------------------
def accent_for(winners: list[int]) -> int:
    return DIVISION_COLORS[winners[0]] if len(winners) == 1 else BRAND_COLOR


class LeaderboardView(discord.ui.LayoutView):
    """The "Division MVPs Leaderboard": a title card, then one card per division in rank order.
    Each card has its colour stripe, rank, the division badge and the crown before the MVP count.
    fixed=True is the permanent post (it has the Refresh button)."""

    def __init__(self, totals: dict[int, int], history_url: str, images: bool = True, test: bool = False,
                 fixed: bool = False):
        super().__init__(timeout=None)
        ordered = sorted(totals.items(), key=lambda item: (-item[1], item[0]))

        head = discord.ui.Container(accent_colour=0xF5C542)
        text = "## \U0001F3C6 Division MVPs Leaderboard\n-# Top divisions by MVPs count in the current season."
        if test:
            text += "\n-# TEST PREVIEW  -  not counted anywhere"
        head.add_item(discord.ui.TextDisplay(text))
        self.add_item(head)

        for rank, (d, points) in enumerate(ordered, start=1):
            row = discord.ui.Container(accent_colour=DIVISION_COLORS[d])
            text = f"### #{rank}  \u00b7  Division {d}\n{CROWN_EMOJI}  **{points}**  MVPs"
            ref = icon_ref(d) if images else None
            row.add_item(discord.ui.Section(text, accessory=discord.ui.Thumbnail(ref)) if ref
                         else discord.ui.TextDisplay(text))
            self.add_item(row)

        foot = discord.ui.Container(accent_colour=0x2B2D31)
        stamp = int(datetime.now(timezone.utc).timestamp())
        foot.add_item(discord.ui.TextDisplay(f"-# \U0001F552 Updated <t:{stamp}:R>  \u00b7  Total Divisions: 5"))
        actions = discord.ui.ActionRow()
        if fixed:
            actions.add_item(LeaderboardRefreshButton())
        actions.add_item(discord.ui.Button(label="MVP History", url=history_url))
        foot.add_item(actions)
        self.add_item(foot)


class RaidLogView(discord.ui.LayoutView):
    """The per-raid division log: divisions always listed 1 to 5, the MVP tag moves to the winner(s).
    One row at the bottom: Info (opens the MVP log), Live Leaderboard (private), Edit (owner only)."""

    def __init__(self, raid_number: int, raiders: list[dict], skipped: list, winners: list[int], info_url: str,
                 images: bool = True, test: bool = False):
        super().__init__(timeout=None)
        tallies, _ = tally(raiders)
        by_division: dict[int, list[str]] = {d: [] for d in range(1, 6)}
        for raider in raiders:
            by_division[int(raider["division"])].append(f"<@{raider['user_id']}>")

        panel = discord.ui.Container(accent_colour=accent_for(winners))
        panel.add_item(discord.ui.TextDisplay("## DIVISION MVP LOG"))
        if test:
            panel.add_item(discord.ui.TextDisplay("-# TEST PREVIEW  -  not counted anywhere"))
        mvp = "  \u00d7  ".join(f"DIVISION {d:02d}" for d in winners) if winners else "No MVP awarded"
        panel.add_item(discord.ui.TextDisplay(
            f"### RAID  `#{raid_number:04d}`  \u00b7  RAIDERS COUNTED `{len(raiders):02d}`\n**MVP:** {mvp}"))
        panel.add_item(discord.ui.Separator())
        for d in range(1, 6):
            members = by_division[d]
            head = f"### DIVISION `{d:02d}`  \u2192  `{tallies[d]:02d}`" + ("  **MVP**" if d in winners else "")
            body = fit_list(members, 520) if members else "No raiders"
            panel.add_item(division_section(f"{head}\n{body}", d, images))
            panel.add_item(discord.ui.Separator())          # a line after every division
        notes = []
        if skipped:
            listing = fit_list([f"{who} ({why})" for who, why in skipped], 300)
            notes.append(f"### NOT COUNTED - `{len(skipped):02d}`\n{listing}")
        multi = sum(1 for r in raiders if r.get("multi"))
        if multi:
            notes.append(f"-# {multi} raider(s) hold more than one division role and were counted for their highest.")
        stamp = int(datetime.now(timezone.utc).timestamp())
        notes.append(f"-# Logged <t:{stamp}:f>  \u00b7  {BOT_NAME}")
        panel.add_item(discord.ui.TextDisplay("\n".join(notes)))
        bottom = discord.ui.ActionRow()
        bottom.add_item(discord.ui.Button(label="Info", url=info_url))
        bottom.add_item(LeaderboardButton())
        if test:
            bottom.add_item(discord.ui.Button(label="Edit", style=discord.ButtonStyle.secondary, disabled=True))
        else:
            bottom.add_item(EditButton(raid_number))
        panel.add_item(bottom)
        self.add_item(panel)


def build_log_view(guild_id: int, row, images: bool = True) -> RaidLogView:
    """Rebuild the MVP log from a saved raid (used to re-arm the Edit button after a restart)."""
    raiders = json.loads(row["raiders_json"])
    skipped = json.loads(row["skipped_json"] or "[]")
    _, winners = tally(raiders)
    return RaidLogView(int(row["raid_number"]), raiders, skipped, winners,
                       channel_url(guild_id, row["channel_id"], row["message_id"]), images)


class LiveTicketView(discord.ui.LayoutView):
    """One live post per ticket, same look as the division MVP log: divisions 1 to 5 are always listed
    (empty at first) and every helper is mentioned under their division as they confirm in the ticket."""

    def __init__(self, guild_id: int, st: dict, images: bool = True, ticket_url: str | None = None, test: bool = False):
        super().__init__(timeout=None)
        live = st["status"] == "live"
        by_division: dict[int, list[str]] = {d: [] for d in range(1, 6)}
        no_division: list[str] = []
        for helper in st["helpers"]:
            mention = f"<@{helper['user_id']}>"
            if helper.get("division"):
                by_division[int(helper["division"])].append(mention)
            else:
                no_division.append(mention)
        counted = len(st["helpers"])

        panel = discord.ui.Container(accent_colour=0x57F287 if live else 0x747F8D)
        panel.add_item(discord.ui.TextDisplay("## LIVE DIVISION RAID" if live else "## DIVISION RAID  -  CLOSED"))
        if test:
            panel.add_item(discord.ui.TextDisplay("-# TEST PREVIEW  -  not tracked"))
        if st.get("opener_id"):
            opener = f"<@{st['opener_id']}>" + (f"  (D{st['opener_division']})" if st.get("opener_division") else "")
        else:
            opener = "waiting for the ticket message"
        head = f"### TICKET  `#{st['name']}`  \u00b7  HELPERS COUNTED `{counted:02d}`"
        if st.get("total") is not None and int(st["total"]) != counted:
            head += f"  (ticket shows `{int(st['total']):02d}`)"
        panel.add_item(discord.ui.TextDisplay(f"{head}\n**Opened by:** {opener}"))
        panel.add_item(discord.ui.Separator())
        for d in range(1, 6):
            members = by_division[d]
            body = fit_list(members, 520) if members else "No helpers yet"
            panel.add_item(division_section(f"### DIVISION `{d:02d}`  \u2192  `{len(members):02d}`\n{body}", d, images))
            panel.add_item(discord.ui.Separator())          # a line after every division
        notes = []
        if no_division:
            notes.append(f"-# No division role (counted, not in a division): {fit_list(no_division, 300)}")
        stamp = int(datetime.now(timezone.utc).timestamp())
        notes.append(f"-# {'Live' if live else 'Closed'}  \u00b7  Updated <t:{stamp}:R>  \u00b7  {BOT_NAME}")
        panel.add_item(discord.ui.TextDisplay("\n".join(notes)))
        bottom = discord.ui.ActionRow()
        if live or test:
            bottom.add_item(discord.ui.Button(label="Open Ticket",
                                              url=ticket_url or channel_url(guild_id, st["channel_id"])))
        else:
            bottom.add_item(discord.ui.Button(label="Ticket Closed", style=discord.ButtonStyle.secondary, disabled=True))
        panel.add_item(bottom)
        self.add_item(panel)


# ----------------------------------------------------------------------------
# FIXED LEADERBOARD + MVP PROCESSING
# ----------------------------------------------------------------------------
async def find_leaderboard_post(channel_id: int) -> str | None:
    """Look for the bot's own fixed leaderboard in the channel (used if the saved link is lost,
    e.g. after switching the database file), so it is edited instead of posted again."""
    channel = await get_channel(channel_id)
    if channel is None or not hasattr(channel, "history") or bot.user is None:
        return None
    try:
        async for m in channel.history(limit=100):
            if m.author.id != bot.user.id:
                continue
            text = post_text(m)
            if "Division MVPs Leaderboard" in text and "TEST PREVIEW" not in text:
                return f"{channel.id}:{m.id}"
    except discord.HTTPException:
        pass
    return None


async def refresh_leaderboard(guild: discord.Guild) -> discord.Message | None:
    """Edit the one fixed leaderboard post (it is only created if it does not exist at all).
    Returns the message (or None if it failed)."""
    async with LB_LOCK:
        key = f"leaderboard_fixed:{guild.id}"
        saved = get_setting(key)
        ref = saved
        if ref and ":" in ref:
            cid, mid = ref.split(":", 1)
            if await try_get_message(int(cid), int(mid)) is None:
                ref = None
        else:
            ref = None
        if ref is None:
            ref = await find_leaderboard_post(LEADERBOARD_ID)
        totals, _ = division_totals(guild.id)
        history_url = channel_url(guild.id, LOG_ID)
        try:
            ref = await send_or_edit(LEADERBOARD_ID, ref, lambda images: LeaderboardView(
                totals, history_url, images, fixed=True), files_fn=icon_files)
        except discord.HTTPException as exc:
            print(f"Could not update the fixed leaderboard: {exc!r}")
            return None
        if not ref or ":" not in ref:
            return None
        if ref != saved:
            set_setting(key, ref)
        set_setting("leaderboard_updated_ts", str(int(time.time())))
        cid, mid = ref.split(":", 1)
        return await try_get_message(int(cid), int(mid))


async def publish_raid(guild: discord.Guild, row_ids: tuple[int, int], raid_number: int, raiders: list[dict],
                       skipped: list, log_ref: str | None) -> str | None:
    """Post (or edit) the division log, then refresh the fixed leaderboard. Returns the log's ref."""
    channel_id, message_id = row_ids
    _, winners = tally(raiders)
    info_url = channel_url(guild.id, channel_id, message_id)
    try:
        log_ref = await send_or_edit(LOG_ID, log_ref, lambda images: RaidLogView(
            raid_number, raiders, skipped, winners, info_url, images))
    except discord.HTTPException as exc:
        print(f"Could not publish the division log for raid {raid_number}: {exc!r}")
    await refresh_leaderboard(guild)
    return log_ref


def raider_signature(raiders: list[dict]):
    return sorted((int(r["user_id"]), int(r["division"])) for r in raiders)


async def process_mvp(message: discord.Message, *, force: bool = False, publish: bool = True) -> str:
    """Record an MVP log. Returns "new", "updated", "unchanged" or "ignored".

    Also used when an MVP log is edited: the saved record is recounted and the log is edited.
    If a second log carries a raid number that is already saved, its raiders are merged in
    instead of counting the raid twice.
    """
    if not source_message(message) or not is_mvp_post(message):
        return "ignored"
    async with PROCESS_LOCK:
        raid_match = RAID_RE.search(post_text(message))
        if not raid_match:
            print(f"Skipped MVP post {message.id}: no explicit Raid #number was found.")
            return "ignored"
        raid_number = int(raid_match.group(1))
        raiders, skipped = await collect_raiders(message)
        guild = message.guild

        row = db.execute("SELECT * FROM raid_results WHERE message_id=?", (message.id,)).fetchone()
        merged = False
        if row is None:
            clause, channel_params = source_db_filter()
            row = db.execute(f"SELECT * FROM raid_results WHERE guild_id=? AND raid_number=? AND {clause}",
                             (guild.id, raid_number, *channel_params)).fetchone()
            merged = row is not None

        if row is not None:
            old_raiders = json.loads(row["raiders_json"])
            if merged:
                known = {r["user_id"] for r in raiders}
                raiders = raiders + [r for r in old_raiders if r["user_id"] not in known]
                skipped = json.loads(row["skipped_json"] or "[]") + skipped
            changed = raider_signature(old_raiders) != raider_signature(raiders)
            if not changed and not force:
                return "unchanged"
            tallies, winners = tally(raiders)
            db.execute("UPDATE raid_results SET raiders_json=?, tallies_json=?, winners_json=?, skipped_json=? WHERE message_id=?",
                       (json.dumps(raiders), json.dumps({str(k): v for k, v in tallies.items()}),
                        json.dumps(winners), json.dumps(skipped), row["message_id"]))
            log_ref = row["log_ref"]
            ids = (row["channel_id"], row["message_id"])
            status = "updated" if changed else "unchanged"
        else:
            tallies, winners = tally(raiders)
            db.execute("""INSERT INTO raid_results
                (message_id,guild_id,channel_id,raid_number,raiders_json,tallies_json,winners_json,created_at,skipped_json)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                       (message.id, guild.id, message.channel.id, raid_number, json.dumps(raiders),
                        json.dumps({str(k): v for k, v in tallies.items()}), json.dumps(winners),
                        message.created_at.astimezone(timezone.utc).isoformat(), json.dumps(skipped)))
            log_ref = None
            ids = (message.channel.id, message.id)
            status = "new"
        db.commit()
        if skipped:
            print(f"Raid #{raid_number}: {len(skipped)} pinged member(s) not counted: " +
                  ", ".join(f"{who} ({why})" for who, why in skipped))

        if publish:
            log_ref = await publish_raid(guild, ids, raid_number, raiders, skipped, log_ref)
            db.execute("UPDATE raid_results SET log_ref=? WHERE message_id=?", (log_ref, ids[1]))
            db.commit()
        return status


async def apply_edit(guild: discord.Guild, raid_number: int, to_add: list[int], to_remove: list[int]) -> str:
    """Owner edit from the log: add or remove raiders, then recount and edit the log and leaderboard."""
    async with PROCESS_LOCK:
        clause, channel_params = source_db_filter()
        row = db.execute(f"SELECT * FROM raid_results WHERE guild_id=? AND raid_number=? AND {clause}",
                         (guild.id, raid_number, *channel_params)).fetchone()
        if row is None:
            return f"Raid **#{raid_number:04d}** is not saved."
        raiders = json.loads(row["raiders_json"])
        skipped = json.loads(row["skipped_json"] or "[]")

        drop = set(to_remove)
        removed = [r for r in raiders if r["user_id"] in drop]
        raiders = [r for r in raiders if r["user_id"] not in drop]

        added, failed = [], []
        for user_id in dict.fromkeys(to_add):
            if any(r["user_id"] == user_id for r in raiders):
                continue
            member = await get_member(guild, user_id)
            if member is None:
                failed.append(f"<@{user_id}> (not in the server)")
                continue
            division, found = division_of(member)
            if member.bot or division is None:
                failed.append(f"<@{user_id}> (no division role)")
                continue
            raiders.append({"user_id": member.id, "display_name": member.display_name,
                            "division": division, "multi": len(found) > 1})
            added.append(f"<@{user_id}>")
        # a manually added raider is no longer "not counted"
        added_ids = {int(re.sub(r"\D", "", a)) for a in added}
        skipped = [s for s in skipped if int(re.sub(r"\D", "", s[0]) or 0) not in added_ids]

        tallies, winners = tally(raiders)
        db.execute("UPDATE raid_results SET raiders_json=?, tallies_json=?, winners_json=?, skipped_json=? WHERE message_id=?",
                   (json.dumps(raiders), json.dumps({str(k): v for k, v in tallies.items()}),
                    json.dumps(winners), json.dumps(skipped), row["message_id"]))
        db.commit()
        ids = (row["channel_id"], row["message_id"])
        log_ref = await publish_raid(guild, ids, raid_number, raiders, skipped, row["log_ref"])
        db.execute("UPDATE raid_results SET log_ref=? WHERE message_id=?", (log_ref, ids[1]))
        db.commit()

    lines = [f"Raid **#{raid_number:04d}** updated. Raiders counted: **{len(raiders)}**."]
    if added:
        lines.append("Added: " + fit_list(added, 500))
    if removed:
        lines.append("Removed: " + fit_list([f"<@{r['user_id']}>" for r in removed], 500))
    if failed:
        lines.append("Could not add: " + fit_list(failed, 500))
    if not (added or removed or failed):
        lines.append("Nothing changed.")
    return "\n".join(lines)


async def latest_mvp_post(guild: discord.Guild) -> discord.Message | None:
    """Newest MVP log (with a Raid #number) in the MVP channel."""
    latest = None
    for channel_id in SOURCE_IDS:
        channel = guild.get_channel(channel_id)
        if channel is None or not hasattr(channel, "history"):
            continue
        try:
            async for message in channel.history(limit=200):  # newest first
                if is_mvp_post(message) and RAID_RE.search(post_text(message)):
                    if latest is None or message.created_at > latest.created_at:
                        latest = message
                    break
        except discord.Forbidden:
            print(f"No history permission in channel {channel_id}; cannot find the latest MVP log.")
    return latest


async def diagnose_channel(guild: discord.Guild) -> str:
    """Explain why recent posts in the MVP channel were not accepted as MVP logs."""
    lines = [f"discord.py version: {discord.__version__} (Components V2 logs need 2.6 or newer)"]
    for channel_id in SOURCE_IDS:
        channel = guild.get_channel(channel_id)
        if channel is None:
            lines.append(f"<#{channel_id}>: the bot cannot see this channel (check View Channel).")
            continue
        try:
            shown = empty = 0
            async for m in channel.history(limit=25):
                if shown >= 6:
                    break
                if not m.author.bot:
                    continue
                text = post_text(m)
                shown += 1
                empty += 0 if text.strip() else 1
                preview = " ".join(text.split())[:90] or "(no readable text)"
                has_mvp = "yes" if re.search(r"\bMVPs?\b", text, re.I) else "NO"
                has_raid = "yes" if re.search(r"\bRaid\b", text, re.I) else "NO"
                id_ok = "yes" if m.author.id == MVP_BOT_ID else "NO"
                has_number = "found" if RAID_RE.search(text) else "NOT found"
                lines.append(f"**{m.author}** `{m.author.id}`  \u00b7  bot ID match: {id_ok}  \u00b7  MVP word: {has_mvp}"
                             f"  \u00b7  Raid word: {has_raid}  \u00b7  Raid #: {has_number}\n> {preview}")
            if shown == 0:
                lines.append(f"<#{channel_id}>: no bot posts in the last 25 messages (or the bot cannot read the history).")
            elif empty == shown:
                lines.append("Every bot post has no readable text. Turn on Message Content Intent and use discord.py 2.6+.")
        except discord.Forbidden:
            lines.append(f"<#{channel_id}>: the bot lacks Read Message History here.")
    return "\n".join(lines)[:3000]


async def check_channels(guild: discord.Guild) -> None:
    """Print where each post type goes, so a wrong .env on the host is obvious in the console."""
    routes = {"Division log (LOG_CHANNEL_ID)": LOG_ID,
              "Fixed leaderboard (LEADERBOARD_CHANNEL_ID)": LEADERBOARD_ID,
              "Live ticket posts (LIVE_POST_CHANNEL_ID)": LIVE_POST_ID}
    print("---- channel routing ----")
    for label, cid in routes.items():
        channel = guild.get_channel(cid)
        if channel is None:
            print(f"  {label}: {cid}  NOT FOUND in {guild.name} (wrong ID, or the bot cannot see it)")
            continue
        perms = channel.permissions_for(guild.me)
        ok = perms.view_channel and perms.send_messages
        print(f"  {label}: #{channel.name} ({cid})  {'ok' if ok else 'MISSING View/Send permission'}")
    for cid in LIVE_CATEGORY_IDS:
        channel = guild.get_channel(cid)
        kind = "NOT FOUND" if channel is None else type(channel).__name__
        print(f"  Ticket category (LIVE_CATEGORY_ID): {cid}  {kind}")
    for cid in sorted(LIVE_IGNORE_IDS):
        channel = guild.get_channel(cid)
        print(f"  Base channel, not a ticket (LIVE_IGNORE_IDS): {cid}  {'NOT FOUND' if channel is None else '#' + channel.name}")
    if len(set(routes.values())) < 3:
        print("  WARNING: two of the channel IDs are identical, so those posts will land in the same channel.")
    print("-------------------------")


def register_saved_views(guild: discord.Guild) -> None:
    """Re-arm the Edit and Live Leaderboard buttons on recent MVP logs after a restart."""
    saved = get_setting(f"leaderboard_fixed:{guild.id}")
    if saved and ":" in saved:
        try:
            totals, _ = division_totals(guild.id)
            bot.add_view(LeaderboardView(totals, channel_url(guild.id, LOG_ID), images=False, fixed=True),
                         message_id=int(saved.split(":", 1)[1]))
        except Exception as exc:
            print(f"Could not re-arm the leaderboard Refresh button: {exc!r}")
    rows = db.execute("SELECT * FROM raid_results WHERE guild_id=? AND log_ref IS NOT NULL "
                      "ORDER BY raid_number DESC LIMIT 50", (guild.id,)).fetchall()
    for row in rows:
        try:
            _, mid = row["log_ref"].split(":", 1)
            bot.add_view(build_log_view(guild.id, row, images=False), message_id=int(mid))
        except Exception as exc:
            print(f"Could not re-arm the buttons for raid {row['raid_number']}: {exc!r}")


def reset_leaderboard_once() -> None:
    """One-time clear of the fixed leaderboard points. The raid logs and history are kept;
    old raids are only marked counted=0 so they stop giving points. Runs once, then never again."""
    if get_setting("leaderboard_reset_v3_1") is not None:
        return
    cur = db.execute("UPDATE raid_results SET counted=0")
    db.commit()
    set_setting("leaderboard_reset_v3_1", datetime.now(timezone.utc).isoformat())
    print(f"Fixed leaderboard cleared once: {cur.rowcount} old raid(s) no longer give points.")


# ----------------------------------------------------------------------------
# LIVE TICKET TRACKING
# ----------------------------------------------------------------------------
def live_lock(channel_id: int) -> asyncio.Lock:
    return LIVE_LOCKS.setdefault(channel_id, asyncio.Lock())


def is_live_channel(channel) -> bool:
    """True for a ticket channel: any channel inside the ticket category, except the base
    channels in LIVE_IGNORE_IDS (and the bot's own log/leaderboard/live channels)."""
    if channel is None:
        return False
    if channel.id in LIVE_IGNORE_IDS or channel.id in {MVP_CHANNEL_ID, LOG_ID, LEADERBOARD_ID, LIVE_POST_ID}:
        return False
    if channel.id in LIVE_CATEGORY_IDS:
        return False
    return getattr(channel, "category_id", None) in LIVE_CATEGORY_IDS


def live_new(channel) -> dict:
    return {"channel_id": channel.id, "guild_id": channel.guild.id, "name": channel.name, "opener_id": None,
            "opener_division": None, "total": None, "helpers": [], "post_ref": None, "status": "live",
            "created_at": datetime.now(timezone.utc).isoformat()}


def live_load(channel_id: int) -> dict | None:
    row = db.execute("SELECT * FROM live_tickets WHERE channel_id=?", (channel_id,)).fetchone()
    if row is None:
        return None
    return {"channel_id": row["channel_id"], "guild_id": row["guild_id"], "name": row["name"],
            "opener_id": row["opener_id"], "opener_division": row["opener_division"],
            "total": row["reported_total"], "helpers": json.loads(row["helpers_json"]),
            "post_ref": row["post_ref"], "status": row["status"], "created_at": row["created_at"]}


def live_store(st: dict) -> None:
    ended = datetime.now(timezone.utc).isoformat() if st["status"] != "live" else None
    db.execute("""INSERT OR REPLACE INTO live_tickets
        (channel_id,guild_id,name,opener_id,opener_division,reported_total,helpers_json,post_ref,status,created_at,ended_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
               (st["channel_id"], st["guild_id"], st["name"], st["opener_id"], st["opener_division"], st["total"],
                json.dumps(st["helpers"]), st["post_ref"], st["status"], st["created_at"], ended))
    db.commit()


async def live_apply(st: dict, guild: discord.Guild, message: discord.Message) -> bool:
    """Read one ticket-bot message into the ticket state. Returns True if anything changed.

    "<@member> has confirmed for help." is the only ping that counts as a helper, so other pings in
    the same message (staff, opener, role pings) can never end up in a division by mistake."""
    text = post_text(message)
    changed = False
    opened = OPENED_RE.search(text)
    if opened and not st["opener_id"]:
        user_id = int(opened.group(1))
        member = await get_member(guild, user_id)
        st["opener_id"] = user_id
        st["opener_division"] = division_of(member)[0] if member else None
        changed = True
    total = LIVE_TOTAL_RE.search(text)
    if total and int(total.group(1)) != st["total"]:
        st["total"] = int(total.group(1))
        changed = True
    if HELP_RE.search(text):
        ids = [int(x) for x in HELP_MENTION_RE.findall(text)]
        if not ids:       # unusual wording: fall back to every ping in the message
            ids = [u.id for u in message.mentions] + [int(x) for x in MENTION_RE.findall(text)]
        known = {h["user_id"] for h in st["helpers"]}
        for user_id in dict.fromkeys(ids):
            if user_id in known or user_id == message.author.id or user_id == st["opener_id"]:
                continue
            member = await get_member(guild, user_id)
            if member is None or member.bot:
                continue
            division, _ = division_of(member)
            st["helpers"].append({"user_id": member.id, "display_name": member.display_name, "division": division})
            changed = True
    return changed


def live_refresh_divisions(st: dict, guild: discord.Guild) -> bool:
    """Re-read every helper's division role from the server (a role given or changed after they
    joined is picked up on the next update). Returns True if anything changed."""
    changed = False
    for helper in st["helpers"]:
        member = guild.get_member(helper["user_id"])
        if member is None:
            continue
        division, _ = division_of(member)
        if division != helper.get("division"):
            helper["division"] = division
            changed = True
    return changed


async def live_publish(guild: discord.Guild, st: dict) -> None:
    try:
        ref = await send_or_edit(LIVE_POST_ID, st["post_ref"], lambda images: LiveTicketView(guild.id, st, images))
    except discord.HTTPException as exc:
        print(f"Could not post the live ticket {st['name']}: {exc!r}")
        return
    if ref != st["post_ref"]:
        st["post_ref"] = ref
    live_store(st)


def is_me(user) -> bool:
    return bot.user is not None and user.id == bot.user.id


def live_relevant(text: str) -> bool:
    return bool(OPENED_RE.search(text) or LIVE_TOTAL_RE.search(text) or HELP_RE.search(text))


async def live_catch_up(st: dict, channel, skip_id: int | None = None) -> None:
    """Read the ticket bot's earlier messages in a ticket channel (used when tracking starts late)."""
    try:
        async for old in channel.history(limit=100, oldest_first=True):
            if old.id != skip_id and old.author.bot and not is_me(old.author):
                await live_apply(st, channel.guild, old)
    except discord.HTTPException:
        pass


async def live_bootstrap(channel) -> None:
    """A ticket channel exists: make its live post right away, empty (or with whoever already joined)."""
    async with live_lock(channel.id):
        if live_load(channel.id) is not None:
            return
        st = live_new(channel)
        await live_catch_up(st, channel)
        live_refresh_divisions(st, channel.guild)
        live_store(st)
        await live_publish(channel.guild, st)


async def live_ingest(message: discord.Message, channel=None) -> None:
    """A ticket-bot message appeared (or was edited). `channel` is the ticket it belongs to; it is
    the message's own channel unless the message came from a base/log channel and names the ticket."""
    channel = channel or message.channel
    text = post_text(message)
    if not live_relevant(text):
        if re.search(r"help|ticket", text, re.I):
            print(f"[live] ignored a message from {message.author} in #{channel.name} (no ticket pattern): "
                  f"{' '.join(text.split())[:120]!r}")
        return
    async with live_lock(channel.id):
        st = live_load(channel.id)
        fresh = st is None
        if fresh:
            st = live_new(channel)
            await live_catch_up(st, channel, skip_id=message.id)
        elif st["status"] != "live":
            st["status"] = "live"
            fresh = True
        if st["name"] != channel.name:
            st["name"] = channel.name
            fresh = True
        before = len(st["helpers"])
        changed = await live_apply(st, channel.guild, message)
        if live_refresh_divisions(st, channel.guild):
            changed = True
        print(f"[live] #{channel.name}: read a message from {message.author} ({message.author.id}); "
              f"helpers {before} -> {len(st['helpers'])}; opener={st['opener_id']}")
        if fresh or changed:
            await live_publish(channel.guild, st)


async def live_from_log(message: discord.Message) -> None:
    """A ticket-bot message in one of the base/log channels: find which ticket it is about
    (a #channel mention, or the ticket's name in the text) and add the helpers there."""
    text = post_text(message)
    if not live_relevant(text):
        return
    guild = message.guild
    ids = set(int(x) for x in CHANNEL_MENTION_RE.findall(text)) | {c.id for c in message.channel_mentions}
    targets = [guild.get_channel(i) for i in ids]
    targets = [c for c in targets if c is not None and is_live_channel(c)]
    if not targets:
        lowered = text.lower()
        for category_id in LIVE_CATEGORY_IDS:
            category = guild.get_channel(category_id)
            for ticket in getattr(category, "text_channels", []):
                if is_live_channel(ticket) and len(ticket.name) >= 4 and ticket.name.lower() in lowered:
                    targets.append(ticket)
    for ticket in targets:
        await live_ingest(message, ticket)


async def live_close(guild: discord.Guild, channel_id: int) -> None:
    async with live_lock(channel_id):
        st = live_load(channel_id)
        if st is None or st["status"] != "live":
            return
        st["status"] = "closed"
        live_store(st)
        await live_publish(guild, st)


async def live_reconcile(guild: discord.Guild) -> None:
    """After a restart: close live posts whose ticket channel was deleted while the bot was offline."""
    rows = db.execute("SELECT channel_id FROM live_tickets WHERE guild_id=? AND status='live'", (guild.id,)).fetchall()
    for row in rows:
        if guild.get_channel(row["channel_id"]) is None:
            await live_close(guild, row["channel_id"])


async def live_sync(guild: discord.Guild) -> None:
    """After a restart: start tracking tickets that were created while the bot was offline
    (only channels newer than LIVE_SYNC_HOURS, so old channels are never touched)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LIVE_SYNC_HOURS)
    for category_id in LIVE_CATEGORY_IDS:
        category = guild.get_channel(category_id)
        for ticket in getattr(category, "text_channels", []):
            if is_live_channel(ticket) and ticket.created_at >= cutoff:
                await live_bootstrap(ticket)


# ----------------------------------------------------------------------------
# BACKUPS (hourly DM to the owner + rolling copies on disk)
# ----------------------------------------------------------------------------
def make_backup() -> tuple[Path, Path, str, int]:
    """Snapshot the live database safely (SQLite backup API), verify it, and export a readable JSON copy.
    Returns (sqlite_path, json_path, integrity_result, raid_count)."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    sqlite_path = BACKUP_DIR / f"urdiv_{stamp}.sqlite3"
    json_path = BACKUP_DIR / f"urdiv_{stamp}.json"
    dest = sqlite3.connect(sqlite_path)
    try:
        db.backup(dest)
    finally:
        dest.close()
    check = sqlite3.connect(sqlite_path)
    try:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    raids = []
    for row in db.execute("SELECT * FROM raid_results ORDER BY raid_number"):
        item = dict(row)
        for key in ("raiders_json", "tallies_json", "winners_json", "skipped_json"):
            try:
                item[key.replace("_json", "")] = json.loads(item.pop(key) or "null")
            except (TypeError, ValueError):
                pass
        raids.append(item)
    export = {"version": VERSION, "saved_at": datetime.now(timezone.utc).isoformat(), "raids": raids,
              "settings": {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM settings")},
              "trusted": [r["user_id"] for r in db.execute("SELECT user_id FROM trusted")]}
    json_path.write_text(json.dumps(export, indent=2, ensure_ascii=False), encoding="utf-8")
    for pattern in ("urdiv_*.sqlite3", "urdiv_*.json"):          # keep only the newest copies on disk
        for old in sorted(BACKUP_DIR.glob(pattern))[:-KEEP_BACKUPS]:
            try:
                old.unlink()
            except OSError:
                pass
    return sqlite_path, json_path, integrity, len(raids)


async def send_backup_dm(reason: str = "hourly") -> str:
    """Make a backup and DM both files to the owner. Returns a short status text."""
    try:
        sqlite_path, json_path, integrity, count = make_backup()
    except Exception as exc:
        print(f"Backup failed: {exc!r}")
        return f"Backup failed: {exc!r}"
    try:
        owner = bot.get_user(OWNER_ID) or await bot.fetch_user(OWNER_ID)
        stamp = int(datetime.now(timezone.utc).timestamp())
        await owner.send(
            f"**{BOT_NAME} backup** ({reason})  \u00b7  <t:{stamp}:f>\n"
            f"Raids saved: **{count}**  \u00b7  integrity check: **{integrity}**\n"
            f"Restore with `/restore` and the `.sqlite3` file. The `.json` is a readable copy.",
            files=[discord.File(sqlite_path), discord.File(json_path)])
        set_setting("last_backup_ts", str(stamp))
        return f"Backup sent to your DMs ({count} raids, integrity: {integrity})."
    except discord.Forbidden:
        print("Backup made but the owner's DMs are closed. Open DMs from server members.")
        return "Backup saved on the server, but I can't DM you. Turn on DMs from server members."
    except discord.HTTPException as exc:
        print(f"Could not DM the backup: {exc!r}")
        return f"Backup saved on the server, but the DM failed: {exc!r}"


@tasks.loop(hours=1)
async def hourly_backup():
    last = get_setting("last_backup_ts")
    if last and time.time() - int(last) < 50 * 60:       # avoid DM spam if the bot restarts a lot
        return
    await send_backup_dm("hourly")


@hourly_backup.before_loop
async def _before_hourly_backup():
    await bot.wait_until_ready()


# ----------------------------------------------------------------------------
# EVENTS
# ----------------------------------------------------------------------------
@bot.event
async def on_ready():
    global STARTED
    print(f"{BOT_NAME} v{VERSION} online as {bot.user}  |  database: {DB_PATH}")
    if STARTED:
        return
    STARTED = True
    load_badges()
    await ensure_crown_emoji()
    reset_leaderboard_once()
    if not hourly_backup.is_running():
        hourly_backup.start()
    guilds = [bot.get_guild(GUILD_ID)] if GUILD_ID else list(bot.guilds)
    for guild in guilds:
        if guild is None:
            continue
        try:
            if not guild.chunked:
                await guild.chunk()          # load every member so role lookups are exact
        except Exception as exc:
            print(f"Could not load the member list for {guild.id}: {exc!r}")
        await check_channels(guild)
        register_saved_views(guild)
        target = discord.Object(id=guild.id)
        bot.tree.copy_global_to(guild=target)
        await bot.tree.sync(guild=target)
        await refresh_leaderboard(guild)
        await live_reconcile(guild)
        await live_sync(guild)


@bot.event
async def on_message(message: discord.Message):
    if message.guild is None:
        return
    if source_message(message):
        await process_mvp(message)
    elif message.author.bot and message.channel.id in LIVE_IGNORE_IDS:
        if not is_me(message.author):
            await live_from_log(message)          # ticket bot's log/base channel: find the ticket it talks about
    elif message.author.bot and is_live_channel(message.channel):
        if is_me(message.author):
            return
        if message.author.id not in LIVE_BOT_IDS:
            LIVE_SEEN[message.author.id] = str(message.author)
        await live_ingest(message)                # any bot: only "Ticket opened by / Live Helpers / confirmed for help" texts count


@bot.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    """The MVP bot may edit its log, and the ticket bot edits its ticket message (Live Helpers count)."""
    if payload.channel_id in SOURCE_IDS:
        message = await try_get_message(payload.channel_id, payload.message_id)
        if message is not None and message.guild and source_message(message):
            await process_mvp(message)
        return
    if payload.channel_id in LIVE_IGNORE_IDS:
        message = await try_get_message(payload.channel_id, payload.message_id)
        if message is not None and message.guild and message.author.bot and not is_me(message.author):
            await live_from_log(message)
        return
    channel = bot.get_channel(payload.channel_id)
    if channel is not None and is_live_channel(channel):
        message = await try_get_message(payload.channel_id, payload.message_id)
        if message is not None and message.author.bot and not is_me(message.author):
            await live_ingest(message)


@bot.event
async def on_guild_channel_create(channel):
    """A new channel appeared in the ticket category: it is a ticket, so post its live message right away."""
    if not isinstance(channel, discord.TextChannel) or not is_live_channel(channel):
        return
    await live_bootstrap(channel)


@bot.event
async def on_guild_channel_update(before, after):
    """A channel moved into the ticket category is a ticket too; a renamed ticket updates its live post."""
    if not isinstance(after, discord.TextChannel) or not is_live_channel(after):
        return
    st = live_load(after.id)
    if st is None:
        await live_bootstrap(after)
        return
    if st["status"] == "live" and st["name"] != after.name:
        async with live_lock(after.id):
            st = live_load(after.id)
            if st is not None and st["name"] != after.name:
                st["name"] = after.name
                await live_publish(after.guild, st)


@bot.event
async def on_guild_channel_delete(channel):
    if is_live_channel(channel):
        await live_close(channel.guild, channel.id)


# ----------------------------------------------------------------------------
# SLASH COMMANDS: TRUST (owner only)
# ----------------------------------------------------------------------------
trust_group = app_commands.Group(name="trust", description="Choose who may use the bot's commands",
                                 guild_only=True)


@trust_group.command(name="add", description="Allow a user to use the bot's commands")
@app_commands.describe(user="User to trust")
async def trust_add(interaction: discord.Interaction, user: discord.User):
    if user.bot or user.id == OWNER_ID:
        await interaction.response.send_message(embed=make_embed(
            "Trust", "That user does not need to be trusted.", 0xED4245), ephemeral=True)
        return
    db.execute("INSERT OR IGNORE INTO trusted(user_id) VALUES(?)", (user.id,))
    db.commit()
    await interaction.response.send_message(embed=make_embed(
        "Trust added", f"<@{user.id}> can now use the bot's commands.", 0x57F287),
        ephemeral=True, allowed_mentions=NO_PING)


@trust_group.command(name="remove", description="Stop a user from using the bot's commands")
@app_commands.describe(user="User to remove")
async def trust_remove(interaction: discord.Interaction, user: discord.User):
    cur = db.execute("DELETE FROM trusted WHERE user_id=?", (user.id,))
    db.commit()
    text = f"<@{user.id}> can no longer use the bot's commands." if cur.rowcount else f"<@{user.id}> was not trusted."
    await interaction.response.send_message(embed=make_embed(
        "Trust removed" if cur.rowcount else "Trust", text, 0x57F287 if cur.rowcount else 0xED4245),
        ephemeral=True, allowed_mentions=NO_PING)


@trust_group.command(name="list", description="Show every trusted user")
async def trust_list(interaction: discord.Interaction):
    ids = [r["user_id"] for r in db.execute("SELECT user_id FROM trusted")]
    text = "\n".join(f"<@{i}>" for i in ids) if ids else "No trusted users yet."
    await interaction.response.send_message(embed=make_embed("Trusted users", text),
                                            ephemeral=True, allowed_mentions=NO_PING)


bot.tree.add_command(trust_group)


# ----------------------------------------------------------------------------
# SLASH COMMANDS: LIVE TICKETS
# ----------------------------------------------------------------------------
live_group = app_commands.Group(name="live", description="Live ticket tracking", guild_only=True)


@live_group.command(name="status", description="Show what the live ticket tracker is watching")
async def live_status(interaction: discord.Interaction):
    guild = interaction.guild
    watching = []
    for cid in sorted(LIVE_CATEGORY_IDS):
        channel = guild.get_channel(cid)
        if channel is None:
            watching.append(f"`{cid}`  NOT FOUND (wrong ID, or the bot cannot see it)")
        elif isinstance(channel, discord.CategoryChannel):
            tickets = [c for c in channel.text_channels if is_live_channel(c)]
            watching.append(f"Category **{channel.name}**  \u00b7  {len(tickets)} ticket channel(s) now")
        else:
            watching.append(f"`{cid}` is not a category (set LIVE_CATEGORY_ID to the category ID)")
    rows = db.execute("SELECT * FROM live_tickets WHERE guild_id=? AND status='live' ORDER BY created_at DESC LIMIT 10",
                      (guild.id,)).fetchall()
    open_tickets = [f"<#{r['channel_id']}>  \u00b7  helpers: {len(json.loads(r['helpers_json']))}" for r in rows]
    embed = make_embed("Live ticket tracker")
    embed.add_field(name="Watching (every new channel in)", value="\n".join(watching), inline=False)
    embed.add_field(name="Base channels (not tickets)", value=" ".join(f"<#{i}>" for i in sorted(LIVE_IGNORE_IDS)), inline=False)
    embed.add_field(name="Ticket bots read", value=" ".join(f"<@{i}>" for i in sorted(LIVE_BOT_IDS)), inline=True)
    embed.add_field(name="Live posts", value=f"<#{LIVE_POST_ID}>", inline=True)
    embed.add_field(name="Open tickets tracked", value="\n".join(open_tickets) if open_tickets else "None right now", inline=False)
    if LIVE_SEEN:
        embed.add_field(name="Other bots seen in ticket channels (not read)", inline=False,
                        value="\n".join(f"{name}  `{uid}`" for uid, name in LIVE_SEEN.items())[:900] +
                              "\nIf the ticket bot is listed here, add its ID to LIVE_BOT_IDS in .env.")
    await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=NO_PING)


bot.tree.add_command(live_group)


# ----------------------------------------------------------------------------
# SLASH COMMANDS: MVP TOOLS
# ----------------------------------------------------------------------------
@bot.tree.command(name="help", description="Show every command")
async def help_command(interaction: discord.Interaction):
    embed = make_embed(f"{BOT_NAME} commands")
    embed.add_field(name="Division", inline=False, value=(
        "`/leaderboard` - show the division leaderboard (only you see it)\n"
        "`/raid` - details of one raid and its raiders\n"
        "`/history` - the last 10 raids\n"
        "`/division` - a member's division and raid stats\n"
        "`/divisions` - how many members each division has\n"
        "`/live status` - what the live ticket tracker is watching\n"
        "`/botinfo` - information about the bot"))
    embed.add_field(name="Management", inline=False, value=(
        "`/post` - post or refresh the fixed leaderboard in its channel\n"
        "`/test` - post a test log and a test live post; nothing is saved\n"
        "`/recount` - re-read MVP logs and fix the counts (one raid, or all)\n"
        "`/addraid` - record an MVP log the bot missed\n"
        "`/deleteraid` - remove a raid and its log\n"
        "`/preview` - preview scoring from typed counts\n"
        "`/status` - configuration and totals"))
    embed.add_field(name="Owner only", inline=False, value=(
        "`/trust add` `/trust remove` `/trust list` - choose who can use these commands\n"
        "`/backup` - DM yourself a database backup now (one is also sent every hour)\n"
        "`/restore` - restore the database from a backup `.sqlite3` file\n"
        "**Edit** button on a division log - add or remove raiders"))
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="status", description="Show configuration and recorded raid count")
@app_commands.guild_only()
async def status_command(interaction: discord.Interaction):
    totals, raids = division_totals(interaction.guild_id)
    embed = make_embed(f"{BOT_NAME} status")
    embed.add_field(name="Saved raids", value=str(raids), inline=True)
    embed.add_field(name="Next raid number", value=f"#{next_raid_number(interaction.guild_id):04d}", inline=True)
    embed.add_field(name="Badges", value=f"{len(BADGES)}/5 ready ({sum(1 for v in BADGE_SOURCE.values() if v == 'file')} custom files)", inline=True)
    embed.add_field(name="MVP channel", value=", ".join(f"<#{c}>" for c in sorted(SOURCE_IDS)), inline=True)
    embed.add_field(name="MVP bot", value=f"<@{MVP_BOT_ID}>", inline=True)
    embed.add_field(name="Owner", value=f"<@{OWNER_ID}>", inline=True)
    embed.add_field(name="Division logs", value=f"<#{LOG_ID}>", inline=True)
    embed.add_field(name="Fixed leaderboard", value=f"<#{LEADERBOARD_ID}>", inline=True)
    embed.add_field(name="Live posts", value=f"<#{LIVE_POST_ID}>", inline=True)
    embed.add_field(name="Division roles", value=" \u00b7 ".join(f"D{d}: <@&{r}>" for d, r in DIVISION_ROLES.items()), inline=False)
    embed.add_field(name="MVP awards", value=" \u00b7 ".join(f"D{d}: **{n}**" for d, n in totals.items()), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=NO_PING)


@bot.tree.command(name="leaderboard", description="Show the current division leaderboard (only you see it)")
@app_commands.guild_only()
async def leaderboard_command(interaction: discord.Interaction):
    totals, _ = division_totals(interaction.guild_id)
    await send_private_leaderboard(interaction.response.send_message, totals, channel_url(interaction.guild_id, LOG_ID))


@bot.tree.command(name="post", description="Post or refresh the fixed division leaderboard in its channel")
@app_commands.guild_only()
async def post_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    message = await refresh_leaderboard(interaction.guild)
    if message is None:
        await interaction.followup.send(embed=make_embed(
            "Leaderboard not posted", f"I could not post in <#{LEADERBOARD_ID}>. Check the channel ID and that the bot "
            "can view and send messages there.", 0xED4245), ephemeral=True)
        return
    await interaction.followup.send(embed=make_embed(
        "Leaderboard posted", f"The fixed leaderboard is up to date: {message.jump_url}\n"
        "It updates by itself after every MVP log.", 0x57F287), ephemeral=True)


@bot.tree.command(name="raid", description="Show the details of one recorded raid")
@app_commands.describe(number="Raid number, e.g. 125")
@app_commands.guild_only()
async def raid_command(interaction: discord.Interaction, number: int):
    row = db.execute("SELECT * FROM raid_results WHERE guild_id=? AND raid_number=?", (interaction.guild_id, number)).fetchone()
    if not row:
        await interaction.response.send_message(embed=make_embed(
            "Raid not found", f"No saved raid with number **{number}**.", 0xED4245), ephemeral=True)
        return
    raiders = json.loads(row["raiders_json"])
    skipped = json.loads(row["skipped_json"] or "[]")
    tallies, winners = tally(raiders)
    embed = make_embed(f"Raid #{number:04d}  \u00b7  Raider Info")
    mvp = ", ".join(f"Division {d}" for d in winners) if winners else "None"
    embed.description = (f"Raiders counted: **{len(raiders)}**  \u00b7  not counted: **{len(skipped)}**\n"
                         f"MVP division(s): **{mvp}**\n[Open the MVP log]"
                         f"({channel_url(interaction.guild_id, row['channel_id'], row['message_id'])})")
    for d in range(1, 6):
        members = [f"<@{r['user_id']}>" for r in raiders if int(r["division"]) == d]
        embed.add_field(name=f"Division {d}  \u00b7  {tallies[d]:02d}",
                        value=fit_list(members, 1000) if members else "No raiders", inline=False)
    if skipped:
        embed.add_field(name="Not counted", inline=False,
                        value=fit_list([f"{who} ({why})" for who, why in skipped], 1000))
    await interaction.response.send_message(embed=embed, allowed_mentions=NO_PING)


@bot.tree.command(name="history", description="Show the last 10 recorded raids")
@app_commands.guild_only()
async def history_command(interaction: discord.Interaction):
    rows = db.execute("SELECT raid_number, raiders_json, winners_json FROM raid_results WHERE guild_id=? "
                      "ORDER BY raid_number DESC LIMIT 10", (interaction.guild_id,)).fetchall()
    if not rows:
        await interaction.response.send_message(embed=make_embed("History", "No raids recorded yet."), ephemeral=True)
        return
    lines = []
    for row in rows:
        winners = json.loads(row["winners_json"])
        mvp = " \u00d7 ".join(f"D{d}" for d in winners) if winners else "none"
        lines.append(f"`#{int(row['raid_number']):04d}`  \u00b7  MVP: **{mvp}**  \u00b7  raiders: {len(json.loads(row['raiders_json']))}")
    await interaction.response.send_message(embed=make_embed("Last raids", "\n".join(lines)))


@bot.tree.command(name="division", description="Show a member's division and raid stats")
@app_commands.describe(member="Member to look up (default: you)")
@app_commands.guild_only()
async def division_command(interaction: discord.Interaction, member: discord.Member | None = None):
    member = member or interaction.user
    division, found = division_of(member)
    joined = mvp_raids = 0
    for row in db.execute("SELECT raiders_json, winners_json FROM raid_results WHERE guild_id=?", (interaction.guild_id,)):
        winners = json.loads(row["winners_json"])
        for raider in json.loads(row["raiders_json"]):
            if raider["user_id"] == member.id:
                joined += 1
                mvp_raids += 1 if int(raider["division"]) in winners else 0
    embed = make_embed(member.display_name, color=DIVISION_COLORS.get(division, BRAND_COLOR))
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Division", value=f"Division {division}" if division else "None", inline=True)
    embed.add_field(name="Raids joined", value=str(joined), inline=True)
    embed.add_field(name="Raids with MVP division", value=str(mvp_raids), inline=True)
    if len(found) > 1:
        embed.add_field(name="Note", value="Holds more than one division role; counted for the highest.", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="divisions", description="How many members each division has")
@app_commands.guild_only()
async def divisions_command(interaction: discord.Interaction):
    counts = {d: 0 for d in range(1, 6)}
    for member in interaction.guild.members:
        division, _ = division_of(member)
        if division and not member.bot:
            counts[division] += 1
    await interaction.response.send_message(embed=make_embed(
        "Division members", "\n".join(f"Division {d}: **{n}**" for d, n in counts.items())))


@bot.tree.command(name="test", description="Preview the latest MVP log and a live ticket post (nothing saved or counted)")
@app_commands.guild_only()
async def test_command(interaction: discord.Interaction):
    # Posts a TEST log in the log channel and a TEST live post in the live channel, then shows
    # the leaderboard (with this raid added) privately. Nothing is saved or counted.
    global IMAGE_PROBLEM
    await interaction.response.defer(ephemeral=True, thinking=True)
    IMAGE_PROBLEM = None
    message = await latest_mvp_post(interaction.guild)
    if message is None:
        report = await diagnose_channel(interaction.guild)
        await interaction.followup.send(embed=make_embed(
            "No MVP log found",
            f"I couldn't find a recent MVP log with a Raid #number from <@{MVP_BOT_ID}> in <#{MVP_CHANNEL_ID}>.\n\n"
            f"**What the bot sees in that channel**\n{report}", 0xED4245), ephemeral=True, allowed_mentions=NO_PING)
        return

    raid_number = int(RAID_RE.search(post_text(message)).group(1))
    raiders, skipped = await collect_raiders(message)
    tallies, winners = tally(raiders)
    guild_id = interaction.guild_id

    log_msg = await post_new(LOG_ID, lambda images: RaidLogView(
        raid_number, raiders, skipped, winners, message.jump_url, images, test=True))
    sample = {"channel_id": 0, "name": "medium-backup-0129", "opener_id": interaction.user.id,
              "opener_division": division_of(interaction.user)[0] if isinstance(interaction.user, discord.Member) else None,
              "total": len(raiders), "status": "live", "post_ref": None,
              "helpers": [{"user_id": r["user_id"], "display_name": r["display_name"], "division": r["division"]} for r in raiders]}
    live_msg = await post_new(LIVE_POST_ID, lambda images: LiveTicketView(
        guild_id, sample, images, ticket_url=message.jump_url, test=True))

    totals, _ = division_totals(guild_id)
    already = db.execute("SELECT 1 FROM raid_results WHERE message_id=?", (message.id,)).fetchone()
    if not already:
        for division in winners:
            totals[division] += 1

    thumbs = "all five badges sent" if IMAGE_PROBLEM is None else f"REJECTED by Discord, sent without images: {IMAGE_PROBLEM}"
    lines = [f"Source: [open the MVP log]({message.jump_url})",
             f"Pinged: **{len(raiders) + len(skipped)}**  \u00b7  counted: **{len(raiders)}**  \u00b7  not counted: **{len(skipped)}**",
             f"Nothing is saved or counted.  \u00b7  bot v{VERSION}",
             f"Badges: **{len(BADGES)}/5** loaded  \u00b7  thumbnails: {thumbs}",
             f"Division log: {log_msg.jump_url if log_msg else f'could not post in <#{LOG_ID}>'}",
             f"Live post: {live_msg.jump_url if live_msg else f'could not post in <#{LIVE_POST_ID}>'}"]
    if skipped:
        lines.append("Not counted: " + fit_list([f"{who} ({why})" for who, why in skipped], 700))
    ok = log_msg is not None and live_msg is not None
    await interaction.followup.send(embed=make_embed(
        f"Test preview  \u00b7  Raid #{raid_number:04d}", "\n".join(lines), 0xFEE75C if ok else 0xED4245),
        ephemeral=True, allowed_mentions=NO_PING)
    await send_private_leaderboard(interaction.followup.send, totals, channel_url(guild_id, LOG_ID), test=True)


@bot.tree.command(name="recount", description="Re-read MVP logs and fix the counts")
@app_commands.describe(number="Raid number to recount (leave empty to recount every saved raid)")
@app_commands.guild_only()
async def recount_command(interaction: discord.Interaction, number: int | None = None):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if number is not None:
        row = db.execute("SELECT * FROM raid_results WHERE guild_id=? AND raid_number=?", (interaction.guild_id, number)).fetchone()
        message = await try_get_message(row["channel_id"], row["message_id"]) if row else None
        if message is None:
            await interaction.followup.send(embed=make_embed(
                "Cannot recount", f"Raid **{number}** is not saved, or its MVP log no longer exists.", 0xED4245), ephemeral=True)
            return
        status = await process_mvp(message, force=True)
        await interaction.followup.send(embed=make_embed(
            "Recount done", f"Raid **#{number:04d}**: {status}. The log and the leaderboard were updated.", 0x57F287), ephemeral=True)
        return
    changed = missing = 0
    for row in db.execute("SELECT channel_id, message_id FROM raid_results WHERE guild_id=?", (interaction.guild_id,)).fetchall():
        message = await try_get_message(row["channel_id"], row["message_id"])
        if message is None:
            missing += 1
            continue
        if await process_mvp(message, force=True, publish=False) == "updated":
            changed += 1
    await refresh_leaderboard(interaction.guild)
    await interaction.followup.send(embed=make_embed(
        "Recount done", f"Raids with changed counts: **{changed}**  \u00b7  logs not found: **{missing}**.\n"
        "Totals and the leaderboard are updated. Use `/recount number:` to also refresh one raid's log.", 0x57F287), ephemeral=True)


@bot.tree.command(name="addraid", description="Record an MVP log that the bot missed")
@app_commands.describe(message="Message link or ID of the MVP log")
@app_commands.guild_only()
async def addraid_command(interaction: discord.Interaction, message: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    ids = re.findall(r"\d{15,21}", message)
    target = None
    if ids:
        for channel_id in SOURCE_IDS:
            target = await try_get_message(channel_id, int(ids[-1]))
            if target:
                break
    if target is None:
        await interaction.followup.send(embed=make_embed(
            "Message not found", "I could not find that message in the MVP channel.", 0xED4245), ephemeral=True)
        return
    status = await process_mvp(target, force=True)
    text = {"new": "Recorded and posted.", "updated": "Already saved; recounted and updated.",
            "unchanged": "Already saved; nothing changed.", "ignored": "That message is not a valid MVP log (check the bot ID and the Raid #number)."}[status]
    await interaction.followup.send(embed=make_embed("Add raid", text, 0x57F287 if status != "ignored" else 0xED4245), ephemeral=True)


@bot.tree.command(name="deleteraid", description="Remove a recorded raid and its log")
@app_commands.describe(number="Raid number to remove")
@app_commands.guild_only()
async def deleteraid_command(interaction: discord.Interaction, number: int):
    await interaction.response.defer(ephemeral=True, thinking=True)
    row = db.execute("SELECT * FROM raid_results WHERE guild_id=? AND raid_number=?", (interaction.guild_id, number)).fetchone()
    if not row:
        await interaction.followup.send(embed=make_embed("Raid not found", f"No saved raid **{number}**.", 0xED4245), ephemeral=True)
        return
    await delete_ref(row["update_ref"])
    await delete_ref(row["log_ref"])
    db.execute("DELETE FROM raid_results WHERE message_id=?", (row["message_id"],))
    db.commit()
    await refresh_leaderboard(interaction.guild)
    await interaction.followup.send(embed=make_embed(
        "Raid removed", f"Raid **#{number:04d}** was deleted and the leaderboard updated.", 0x57F287), ephemeral=True)


@bot.tree.command(name="preview", description="Preview an MVP result from typed counts (nothing saved)")
@app_commands.describe(d1="Division 1 raiders", d2="Division 2 raiders", d3="Division 3 raiders",
                       d4="Division 4 raiders", d5="Division 5 raiders")
async def preview_command(interaction: discord.Interaction, d1: app_commands.Range[int, 0, 500],
                          d2: app_commands.Range[int, 0, 500], d3: app_commands.Range[int, 0, 500],
                          d4: app_commands.Range[int, 0, 500], d5: app_commands.Range[int, 0, 500]):
    counts = {1: d1, 2: d2, 3: d3, 4: d4, 5: d5}
    high = max(counts.values())
    winners = [d for d, n in counts.items() if high > 0 and n == high]
    winner_text = ", ".join(f"Division {d}" for d in winners) if winners else "None"
    await interaction.response.send_message(embed=make_embed(
        "MVP scoring preview  \u00b7  nothing saved",
        f"MVP division(s): **{winner_text}**\n\n" + " \u00b7 ".join(f"D{d}: **{n:02d}**" for d, n in counts.items()), 0xFEE75C),
        ephemeral=True)


@bot.tree.command(name="backup", description="Owner only: send a database backup to your DMs now")
async def backup_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    text = await send_backup_dm("manual")
    await interaction.followup.send(embed=make_embed("Backup", text, 0x57F287), ephemeral=True)


@bot.tree.command(name="restore", description="Owner only: restore the database from a backup .sqlite3 file")
@app_commands.describe(file="A urdiv_*.sqlite3 backup file from your DMs")
@app_commands.guild_only()
async def restore_command(interaction: discord.Interaction, file: discord.Attachment):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if interaction.user.id != OWNER_ID:
        await interaction.followup.send("Only the owner can restore a backup.", ephemeral=True)
        return
    if file.size > 25 * 1024 * 1024 or not file.filename.lower().endswith((".sqlite3", ".sqlite", ".db")):
        await interaction.followup.send(embed=make_embed(
            "Restore", "Attach a `.sqlite3` backup file (under 25 MB).", 0xED4245), ephemeral=True)
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    incoming = BACKUP_DIR / "incoming_restore.sqlite3"
    await file.save(incoming)
    try:
        src = sqlite3.connect(f"file:{incoming}?mode=ro", uri=True)
        try:
            integrity = src.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if integrity != "ok" or "raid_results" not in tables:
                await interaction.followup.send(embed=make_embed(
                    "Restore", f"That file is not a valid backup (integrity: {integrity}).", 0xED4245), ephemeral=True)
                return
            make_backup()                      # safety copy of the current data before replacing it
            async with PROCESS_LOCK:
                src.backup(db)
        finally:
            src.close()
        ensure_schema()
        await refresh_leaderboard(interaction.guild)
        totals, raids = division_totals(interaction.guild_id)
        await interaction.followup.send(embed=make_embed(
            "Restore done", f"Raids loaded: **{raids}**.\n"
            "A copy of the previous data was kept in the backups folder.", 0x57F287), ephemeral=True)
    except Exception as exc:
        print(f"Restore failed: {exc!r}")
        await interaction.followup.send(embed=make_embed("Restore failed", f"`{exc!r}`", 0xED4245), ephemeral=True)
    finally:
        try:
            incoming.unlink()
        except OSError:
            pass


@bot.tree.command(name="botinfo", description="Information about the bot")
async def botinfo_command(interaction: discord.Interaction):
    seconds = int(time.time() - START_TIME)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    embed = make_embed(BOT_NAME)
    embed.add_field(name="Uptime", value=f"{days}d {hours}h {minutes}m", inline=True)
    embed.add_field(name="Latency", value=f"{round(bot.latency * 1000)} ms", inline=True)
    embed.add_field(name="Servers", value=str(len(bot.guilds)), inline=True)
    embed.add_field(name="discord.py", value=discord.__version__, inline=True)
    embed.add_field(name="Python", value=platform.python_version(), inline=True)
    embed.add_field(name="Version", value=VERSION, inline=True)
    if interaction.guild_id:
        embed.add_field(name="Saved raids", value=str(division_totals(interaction.guild_id)[1]), inline=True)
    if bot.user:
        embed.set_thumbnail(url=bot.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        embed = make_embed("Not allowed", "Only the owner and trusted users can use this bot's commands.", 0xED4245)
    else:
        embed = make_embed(f"{BOT_NAME} command error", "The command failed. Check the bot console for details.", 0xED4245)
        print(f"Slash command error: {error!r}")
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.HTTPException:
        pass


# ----------------------------------------------------------------------------
# BUILT-IN ART FOR THE FIXED LEADERBOARD (128x128 PNG, base64)
#   EMBEDDED_ICONS: the five division badges     EMBEDDED_CROWN: the crown before every count
# ----------------------------------------------------------------------------
EMBEDDED_ICONS = {
    1: (
        "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"
        "CC"
    ),
    2: (
        "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"
    ),
    3: (
        "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"
    ),
    4: (
        "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"
    ),
    5: (
        "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"
    ),
}

EMBEDDED_CROWN = (
        "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"
        "orAdqgAAAABJRU5ErkJggg=="
)



# ----------------------------------------------------------------------------
# START
# ----------------------------------------------------------------------------
if not TOKEN or TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
    raise RuntimeError("Set your Discord bot token in .env before starting the bot.")
bot.run(TOKEN)