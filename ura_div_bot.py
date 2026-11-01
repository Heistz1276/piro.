"""URA | DIV v4: role-based division MVP tracker + live backup/ticket tracker.

Needs: discord.py >= 2.6 (Components V2) and python-dotenv.

ART: the division badge images + crown are NOT stored in this file. On the first start the bot copies them
out of your OLD bot file (the one with EMBEDDED_ICONS / EMBEDDED_CROWN) if it is in the same folder, and saves
them to art_data.py. After that you may delete the old file. (badges_data.py is handled the same way.)

New in v4
  - Fixed leaderboard: ONE card, division badge emoji on the LEFT, "View" button under every division,
    "Division # Is on Lead!" line.
  - "Division # Is on Lead!" in the MVP log and the live post too.
  - Division log images always show (falls back to the leaderboard art if badges_data.py is missing).
  - Live tracking reads bot 1566412027906228274 (every human it pings = a helper), more join wordings,
    /sync, /live list|add|remove|close|refresh|debug.
  - /setup (auto build under category 1565732366595657769, or choose your own channels) and the same
    prompt when the bot joins a server.
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

load_dotenv(Path(__file__).resolve().parent / ".env", override=True)


def env_ids(name: str) -> set[int]:
    return {int(x) for x in re.split(r"[,\s]+", os.getenv(name, "")) if x.isdigit()}


def env_int(name: str, default: int = 0) -> int:
    try:
        value = int(os.getenv(name, "0") or 0)
    except ValueError:
        value = 0
    return value or default


# ----------------------------------------------------------------------------
# CONFIGURATION (.env values are the defaults; /setup saves per-server overrides in the database)
# ----------------------------------------------------------------------------
BOT_NAME = "URA | DIV"
TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = env_int("GUILD_ID")
OWNER_ID = env_int("OWNER_ID", 1455426573741330495)

LOG_ID = env_int("LOG_CHANNEL_ID", 1565771359097716766)
LEADERBOARD_ID = env_int("LEADERBOARD_CHANNEL_ID", 1565732417271234791)
LIVE_POST_ID = env_int("LIVE_POST_CHANNEL_ID", env_int("RAID_UPDATE_CHANNEL_ID", 1565732391107170364))
MVP_BOT_ID = env_int("MVP_BOT_ID", 1563126014702395542)
SOURCE_IDS = env_ids("MVP_CHANNEL_IDS") or {1524379574585000006}
MVP_CHANNEL_ID = sorted(SOURCE_IDS)[0]

LIVE_CATEGORY_IDS = env_ids("LIVE_CATEGORY_ID") or {1507716131551707258}
LIVE_IGNORE_IDS = env_ids("LIVE_IGNORE_IDS") or {1524379571946913862, 1562782175927992340}
LIVE_BOT_IDS = env_ids("LIVE_BOT_IDS") or {1470577242676531372, 1563126014702395542, 1566412027906228274}
LIVE_BOT_IDS |= {1566412027906228274}
# Bots whose messages are "join" messages: every human they ping in a ticket counts as a helper.
LIVE_JOIN_BOT_IDS = env_ids("LIVE_JOIN_BOT_IDS") or {1566412027906228274}
LIVE_SYNC_HOURS = env_int("LIVE_SYNC_HOURS", 12)
SETUP_CATEGORY_ID = env_int("SETUP_CATEGORY_ID", 1565732366595657769)   # auto-build puts the channels here

ENV_DEFAULTS = {"log": LOG_ID, "leaderboard": LEADERBOARD_ID, "live": LIVE_POST_ID,
                "source": MVP_CHANNEL_ID, "category": sorted(LIVE_CATEGORY_IDS)[0]}

RAID_START_NUMBER = env_int("RAID_START_NUMBER", 1)
VERSION = "4.0-live"
_script_dir = Path(__file__).resolve().parent
_db = Path(os.getenv("PIRO_DB_PATH", "piro.sqlite3"))
DB_PATH = str(_db if _db.is_absolute() else _script_dir / _db)
BACKUP_DIR = Path(os.getenv("PIRO_BACKUP_DIR", str(_script_dir / "backups")))
KEEP_BACKUPS = 48
BADGE_DIR = Path(os.getenv("PIRO_BADGE_DIR", str(_script_dir / "badges")))

DEFAULT_DIVISION_ROLES = {
    1: 1517694951650103427, 2: 1517694955835883682, 3: 1517694959291994132,
    4: 1517694962119213056, 5: 1517694969538805831,
}
DIVISION_ROLES = {d: env_int(f"DIVISION_{d}_ROLE_ID", DEFAULT_DIVISION_ROLES[d]) for d in range(1, 6)}
DIVISION_ROLE_IDS = {role_id: d for d, role_id in DIVISION_ROLES.items()}
DIVISION_NAME_RE = re.compile(r"\bdivision\s*[-_ ]?0?([1-5])\b", re.I)


def clean_image_url(d: int) -> str:
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
HELP_RE = re.compile(r"Help Registered|confirmed for help|has joined|joined (?:the )?(?:backup|raid|ticket|help)|"
                     r"is (?:now )?helping|will (?:be )?help", re.I)
HELP_MENTION_RE = re.compile(r"<@!?(\d+)>\s*(?:has confirmed for help|has joined|joined|is (?:now )?helping|will (?:be )?help)", re.I)
NO_PING = discord.AllowedMentions.none()
START_TIME = time.time()

# ----------------------------------------------------------------------------
# DATABASE
# ----------------------------------------------------------------------------
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.execute("PRAGMA synchronous=FULL")
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
    try:
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


def cfg_get(guild_id: int, key: str) -> int | None:
    value = get_setting(f"cfg:{guild_id}:{key}")
    return int(value) if value and value.isdigit() else None


def cfg_set(guild_id: int, key: str, value: int) -> None:
    set_setting(f"cfg:{guild_id}:{key}", str(value))


def apply_config(guild_id: int) -> None:
    """Load the channels saved by /setup for this server (falls back to the .env values)."""
    global LOG_ID, LEADERBOARD_ID, LIVE_POST_ID, SOURCE_IDS, MVP_CHANNEL_ID, LIVE_CATEGORY_IDS
    LOG_ID = cfg_get(guild_id, "log") or LOG_ID
    LEADERBOARD_ID = cfg_get(guild_id, "leaderboard") or LEADERBOARD_ID
    LIVE_POST_ID = cfg_get(guild_id, "live") or LIVE_POST_ID
    source = cfg_get(guild_id, "source")
    if source:
        SOURCE_IDS = {source}
        MVP_CHANNEL_ID = source
    category = cfg_get(guild_id, "category")
    if category:
        LIVE_CATEGORY_IDS = {category}


# ----------------------------------------------------------------------------
# BOT
# ----------------------------------------------------------------------------
OWNER_ONLY_COMMANDS = {"trust", "backup", "restore", "setup"}


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
LB_LOCK = asyncio.Lock()
LB_LAST_PRESS = 0.0
LIVE_SEEN: dict[int, str] = {}
IMAGE_PROBLEM: str | None = None
STARTED = False


# ----------------------------------------------------------------------------
# ART (copied out of the old bot file on the first start -> art_data.py)
# ----------------------------------------------------------------------------
EMBEDDED_ICONS: dict[int, str] = {}
EMBEDDED_CROWN: str = ""


def _extract_block(text: str, name: str, opener: str, closer: str):
    start = text.find(f"{name} = {opener}")
    if start == -1:
        return None
    end = text.index(closer, start) + len(closer)
    return ast.literal_eval(text[start + len(name) + 3:end])


def load_art() -> None:
    global EMBEDDED_ICONS, EMBEDDED_CROWN
    if str(_script_dir) not in sys.path:
        sys.path.insert(0, str(_script_dir))
    try:
        import art_data
        EMBEDDED_ICONS = {int(k): str(v) for k, v in dict(art_data.EMBEDDED_ICONS).items()}
        EMBEDDED_CROWN = str(art_data.EMBEDDED_CROWN)
        return
    except Exception:
        pass
    this_file = Path(__file__).resolve()
    for path in sorted(_script_dir.glob("*.py")):
        if path.resolve() == this_file or path.name in ("art_data.py", "badges_data.py"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
            icons = _extract_block(text, "EMBEDDED_ICONS", "{", "\n}")
            crown = _extract_block(text, "EMBEDDED_CROWN", "(", "\n)")
            if not icons:
                continue
            EMBEDDED_ICONS = {int(k): str(v) for k, v in icons.items()}
            EMBEDDED_CROWN = str(crown or "")
            (_script_dir / "art_data.py").write_text(
                f"EMBEDDED_ICONS = {EMBEDDED_ICONS!r}\nEMBEDDED_CROWN = {EMBEDDED_CROWN!r}\n", encoding="utf-8")
            print(f"Badge art copied from {path.name} into art_data.py")
            return
        except Exception as exc:
            print(f"Could not read art from {path.name}: {exc!r}")
    print("No badge art found (art_data.py missing). Keep your OLD bot file in this folder once so it can be copied.")


# ----------------------------------------------------------------------------
# DIVISION BADGES
# ----------------------------------------------------------------------------
BADGES: dict[int, bytes] = {}
BADGE_NAMES: dict[int, str] = {}
BADGE_SOURCE: dict[int, str] = {}
ICONS: dict[int, bytes] = {}
CROWN_EMOJI = "\U0001F451"
CROWN_NAME = "ura_crown"
DIV_EMOJI: dict[int, str] = dict(DIVISION_EMOJI)     # replaced by the badge emoji once uploaded


def find_badge_file(d: int) -> Path | None:
    if not BADGE_DIR.is_dir():
        return None
    pattern = re.compile(rf"^division[\s_-]*0?{d}\.(png|jpe?g|webp|gif)$", re.I)
    for path in sorted(BADGE_DIR.iterdir()):
        if path.is_file() and pattern.match(path.name) and path.stat().st_size > 100:
            return path
    return None


def find_embedded_badges() -> dict[int, str]:
    if str(_script_dir) not in sys.path:
        sys.path.insert(0, str(_script_dir))
    try:
        from badges_data import EMBEDDED_BADGES as data
        return {int(k): str(v) for k, v in dict(data).items()}
    except Exception:
        pass
    this_file = Path(__file__).resolve()
    for path in sorted(_script_dir.glob("*.py")):
        if path.resolve() == this_file or path.name in ("badges_data.py", "art_data.py"):
            continue
        try:
            data = _extract_block(path.read_text(encoding="utf-8"), "EMBEDDED_BADGES", "{", "\n}")
            if not data:
                continue
            data = {int(k): str(v) for k, v in data.items()}
            (_script_dir / "badges_data.py").write_text("EMBEDDED_BADGES = " + repr(data) + "\n", encoding="utf-8")
            print(f"Built-in badges copied from {path.name} into badges_data.py")
            return data
        except Exception as exc:
            print(f"Could not read badges from {path.name}: {exc!r}")
    return {}


def load_badges() -> None:
    embedded = find_embedded_badges()
    ICONS.clear()
    for d, data in EMBEDDED_ICONS.items():
        ICONS[int(d)] = base64.b64decode(data)
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
        elif d in ICONS:                                  # fallback so the log images are never missing
            BADGES[d] = ICONS[d]
            BADGE_NAMES[d] = f"division{d}.png"
            BADGE_SOURCE[d] = "built-in"
    print(f"Badges: {len(BADGES)}/5 loaded  (custom files: {sum(1 for v in BADGE_SOURCE.values() if v == 'file')}, "
          f"built-in: {sum(1 for v in BADGE_SOURCE.values() if v == 'built-in')})")


async def ensure_app_emojis() -> None:
    """Upload the crown + five division badges once as application emojis (shown LEFT of the text)."""
    global CROWN_EMOJI
    try:
        existing = {e.name: e for e in await bot.fetch_application_emojis()}
    except Exception as exc:
        print(f"Could not read application emojis ({exc!r}); using plain emojis.")
        return

    async def need(name: str, b64: str) -> str | None:
        if name in existing:
            return str(existing[name])
        try:
            return str(await bot.create_application_emoji(name=name, image=base64.b64decode(b64)))
        except Exception as exc:
            print(f"Could not upload emoji {name} ({exc!r}).")
            return None

    if EMBEDDED_CROWN:
        found = await need(CROWN_NAME, EMBEDDED_CROWN)
        if found:
            CROWN_EMOJI = found
    for d, data in EMBEDDED_ICONS.items():
        found = await need(f"ura_div{d}", data)
        if found:
            DIV_EMOJI[int(d)] = found
    print(f"Emojis ready: crown {CROWN_EMOJI}, divisions {' '.join(DIV_EMOJI[d] for d in range(1, 6))}")


def badge_ref(d: int) -> str | None:
    if DIVISION_IMAGE_URLS[d]:
        return DIVISION_IMAGE_URLS[d]
    return f"attachment://{BADGE_NAMES[d]}" if d in BADGES else None


def badge_files() -> list[discord.File]:
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


def leaders_of(totals: dict[int, int]) -> list[int]:
    high = max(totals.values()) if totals else 0
    return [d for d, n in sorted(totals.items()) if high > 0 and n == high]


def lead_text(leaders: list[int]) -> str:
    if not leaders:
        return "No division is on lead yet"
    if len(leaders) == 1:
        return f"Division {leaders[0]} Is on Lead!"
    return " \u00d7 ".join(f"Division {d}" for d in leaders) + " Are on Lead!"


def next_raid_number(guild_id: int) -> int:
    clause, channel_params = source_db_filter()
    row = db.execute(f"SELECT MAX(raid_number) AS m FROM raid_results WHERE guild_id=? AND {clause}",
                     (guild_id, *channel_params)).fetchone()
    return RAID_START_NUMBER if row["m"] is None else int(row["m"]) + 1


def division_totals(guild_id: int) -> tuple[dict[int, int], int]:
    totals = {d: 0 for d in range(1, 6)}
    clause, channel_params = source_db_filter()
    params = (guild_id, *channel_params)
    for row in db.execute(f"SELECT winners_json FROM raid_results WHERE guild_id=? AND counted=1 AND {clause}", params):
        for division in json.loads(row["winners_json"]):
            totals[int(division)] += 1
    raids = db.execute(f"SELECT COUNT(*) AS n FROM raid_results WHERE guild_id=? AND {clause}", params).fetchone()["n"]
    return totals, int(raids)


def fit_list(items: list[str], limit: int) -> str:
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
                old = None
            if old is not None:
                await old.edit(view=view, attachments=files)
                return ref
    sent = await channel.send(view=view, files=files, allowed_mentions=NO_PING)
    return f"{channel.id}:{sent.id}"


async def send_or_edit(channel_id: int, ref: str | None, build, with_files: bool = True) -> str | None:
    """Edit the message saved in `ref` ("channel:message"); send a new one if it is gone or in another channel.
    If Discord rejects the badge images (50035) it is sent again without thumbnails."""
    channel = await get_channel(channel_id)
    if channel is None:
        print(f"Channel {channel_id} is not reachable.")
        return ref
    for images in ((True, False) if with_files else (False,)):
        try:
            return await _deliver(channel, ref, build(images), badge_files() if images else [])
        except discord.HTTPException as exc:
            if images and is_bad_form(exc):
                note_image_problem(exc)
                continue
            raise
    return ref


async def post_new(channel_id: int, build) -> discord.Message | None:
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
    await send(view=LeaderboardView(totals, history_url, True, test), ephemeral=True)


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
# BUTTONS + MODALS
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
        super().__init__(label="Edit", style=discord.ButtonStyle.secondary, custom_id=f"urdiv:edit:{raid_number}")
        self.raid_number = raid_number

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("Only the owner can use Edit.", ephemeral=True)
            return
        await interaction.response.send_modal(RaidEditModal(self.raid_number))


class LeaderboardButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Live Leaderboard", style=discord.ButtonStyle.secondary, custom_id="urdiv:leaderboard")

    async def callback(self, interaction: discord.Interaction):
        if interaction.guild_id is None:
            return
        totals, _ = division_totals(interaction.guild_id)
        await send_private_leaderboard(interaction.response.send_message, totals,
                                       channel_url(interaction.guild_id, LOG_ID))


class LeaderboardRefreshButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Refresh", emoji="\U0001F504", style=discord.ButtonStyle.secondary,
                         custom_id="urdiv:lb_refresh")

    async def callback(self, interaction: discord.Interaction):
        global LB_LAST_PRESS
        if interaction.guild is None:
            return
        await interaction.response.defer()
        if time.time() - LB_LAST_PRESS < 5:
            return
        LB_LAST_PRESS = time.time()
        await refresh_leaderboard(interaction.guild)


class DivisionViewButton(discord.ui.Button):
    """'View' under a division on the fixed leaderboard: lists every raid (backup) it got the MVP in."""

    def __init__(self, d: int):
        super().__init__(label="View", style=discord.ButtonStyle.secondary, custom_id=f"urdiv:lbview:{d}")
        self.d = d

    async def callback(self, interaction: discord.Interaction):
        if interaction.guild_id is None:
            return
        gid, d = interaction.guild_id, self.d
        clause, channel_params = source_db_filter()
        rows = db.execute(f"SELECT * FROM raid_results WHERE guild_id=? AND counted=1 AND {clause} "
                          "ORDER BY raid_number DESC", (gid, *channel_params)).fetchall()
        wins = [r for r in rows if d in [int(x) for x in json.loads(r["winners_json"])]]
        lines, used = [], 0
        for r in wins:
            mine = sum(1 for x in json.loads(r["raiders_json"]) if int(x["division"]) == d)
            line = (f"`#{int(r['raid_number']):04d}`  \u00b7  **{mine}** raiders  \u00b7  "
                    f"[division log]({ref_url(gid, r['log_ref'], LOG_ID)})  \u00b7  "
                    f"[MVP post]({channel_url(gid, r['channel_id'], r['message_id'])})")
            if used + len(line) > 3600:
                lines.append(f"+{len(wins) - len(lines)} more")
                break
            lines.append(line)
            used += len(line) + 1
        text = "\n".join(lines) if lines else "This division has no MVP yet."
        embed = make_embed(f"Division {d}  \u00b7  MVPs: {len(wins)}", text, DIVISION_COLORS[d])
        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=NO_PING)


# ----------------------------------------------------------------------------
# LAYOUTS (Components V2)
# ----------------------------------------------------------------------------
def accent_for(winners: list[int]) -> int:
    return DIVISION_COLORS[winners[0]] if len(winners) == 1 else BRAND_COLOR


class LeaderboardView(discord.ui.LayoutView):
    """ONE card: title + who is on lead, then every division in rank order with its badge emoji on the LEFT,
    the MVP count on the right and a View button under it."""

    def __init__(self, totals: dict[int, int], history_url: str, images: bool = True, test: bool = False,
                 fixed: bool = False):
        super().__init__(timeout=None)
        ordered = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
        card = discord.ui.Container(accent_colour=0xF5C542)
        head = ("## \U0001F3C6 Division MVPs Leaderboard\n"
                f"### {CROWN_EMOJI} {lead_text(leaders_of(totals))}\n"
                "-# Top divisions by MVPs count in the current season.")
        if test:
            head += "\n-# TEST PREVIEW  -  not counted anywhere"
        card.add_item(discord.ui.TextDisplay(head))
        card.add_item(discord.ui.Separator())
        for rank, (d, points) in enumerate(ordered, start=1):
            card.add_item(discord.ui.TextDisplay(
                f"### {DIV_EMOJI[d]}  Division {d}  \u00b7  #{rank}\n{CROWN_EMOJI}  **{points}**  MVPs"))
            row = discord.ui.ActionRow()
            row.add_item(DivisionViewButton(d))
            card.add_item(row)
            card.add_item(discord.ui.Separator())
        stamp = int(datetime.now(timezone.utc).timestamp())
        card.add_item(discord.ui.TextDisplay(f"-# \U0001F552 Updated <t:{stamp}:R>  \u00b7  Total Divisions: 5"))
        actions = discord.ui.ActionRow()
        if fixed:
            actions.add_item(LeaderboardRefreshButton())
        actions.add_item(discord.ui.Button(label="MVP History", url=history_url))
        card.add_item(actions)
        self.add_item(card)


class RaidLogView(discord.ui.LayoutView):
    def __init__(self, raid_number: int, raiders: list[dict], skipped: list, winners: list[int], info_url: str,
                 images: bool = True, test: bool = False, lead: list[int] | None = None):
        super().__init__(timeout=None)
        tallies, _ = tally(raiders)
        by_division: dict[int, list[str]] = {d: [] for d in range(1, 6)}
        for raider in raiders:
            by_division[int(raider["division"])].append(f"<@{raider['user_id']}>")

        panel = discord.ui.Container(accent_colour=accent_for(winners))
        panel.add_item(discord.ui.TextDisplay(f"## DIVISION MVP LOG\n### \U0001F451 {lead_text(lead or [])}"))
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
            panel.add_item(discord.ui.Separator())
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
    raiders = json.loads(row["raiders_json"])
    skipped = json.loads(row["skipped_json"] or "[]")
    _, winners = tally(raiders)
    totals, _ = division_totals(guild_id)
    return RaidLogView(int(row["raid_number"]), raiders, skipped, winners,
                       channel_url(guild_id, row["channel_id"], row["message_id"]), images,
                       lead=leaders_of(totals))


class LiveTicketView(discord.ui.LayoutView):
    """One live post per ticket/backup: divisions 1 to 5 always listed, every helper mentioned under their division."""

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
        leaders = leaders_of({d: len(m) for d, m in by_division.items()})

        panel = discord.ui.Container(accent_colour=0x57F287 if live else 0x747F8D)
        title = "## LIVE DIVISION RAID" if live else "## DIVISION RAID  -  CLOSED"
        panel.add_item(discord.ui.TextDisplay(f"{title}\n### \U0001F451 {lead_text(leaders)}"))
        if test:
            panel.add_item(discord.ui.TextDisplay("-# TEST PREVIEW  -  not tracked"))
        if st.get("opener_id"):
            opener = f"<@{st['opener_id']}>" + (f"  (D{st['opener_division']})" if st.get("opener_division") else "")
        else:
            opener = "waiting for the ticket message"
        head = f"### TICKET  `#{st['name']}`  \u00b7  HELPERS JOINED `{counted:02d}`"
        if st.get("total") is not None and int(st["total"]) != counted:
            head += f"  (ticket shows `{int(st['total']):02d}`)"
        panel.add_item(discord.ui.TextDisplay(f"{head}\n**Opened by:** {opener}"))
        panel.add_item(discord.ui.Separator())
        for d in range(1, 6):
            members = by_division[d]
            body = fit_list(members, 520) if members else "No helpers yet"
            panel.add_item(division_section(f"### DIVISION `{d:02d}`  \u2192  `{len(members):02d}`\n{body}", d, images))
            panel.add_item(discord.ui.Separator())
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
                totals, history_url, images, fixed=True), with_files=False)
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
    channel_id, message_id = row_ids
    _, winners = tally(raiders)
    info_url = channel_url(guild.id, channel_id, message_id)
    lead = leaders_of(division_totals(guild.id)[0])
    try:
        log_ref = await send_or_edit(LOG_ID, log_ref, lambda images: RaidLogView(
            raid_number, raiders, skipped, winners, info_url, images, lead=lead))
    except discord.HTTPException as exc:
        print(f"Could not publish the division log for raid {raid_number}: {exc!r}")
    await refresh_leaderboard(guild)
    return log_ref


def raider_signature(raiders: list[dict]):
    return sorted((int(r["user_id"]), int(r["division"])) for r in raiders)


async def process_mvp(message: discord.Message, *, force: bool = False, publish: bool = True) -> str:
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
    latest = None
    for channel_id in SOURCE_IDS:
        channel = guild.get_channel(channel_id)
        if channel is None or not hasattr(channel, "history"):
            continue
        try:
            async for message in channel.history(limit=200):
                if is_mvp_post(message) and RAID_RE.search(post_text(message)):
                    if latest is None or message.created_at > latest.created_at:
                        latest = message
                    break
        except discord.Forbidden:
            print(f"No history permission in channel {channel_id}; cannot find the latest MVP log.")
    return latest


async def diagnose_channel(guild: discord.Guild) -> str:
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
    routes = {"Division log": LOG_ID, "Fixed leaderboard": LEADERBOARD_ID, "Live ticket posts": LIVE_POST_ID,
              "MVP source": MVP_CHANNEL_ID}
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
        print(f"  Ticket category: {cid}  {'NOT FOUND' if channel is None else type(channel).__name__}")
    print("  Join bots read:", ", ".join(str(i) for i in sorted(LIVE_JOIN_BOT_IDS)))
    print("-------------------------")


def register_saved_views(guild: discord.Guild) -> None:
    saved = get_setting(f"leaderboard_fixed:{guild.id}")
    if saved and ":" in saved:
        try:
            totals, _ = division_totals(guild.id)
            bot.add_view(LeaderboardView(totals, channel_url(guild.id, LOG_ID), images=False, fixed=True),
                         message_id=int(saved.split(":", 1)[1]))
        except Exception as exc:
            print(f"Could not re-arm the leaderboard buttons: {exc!r}")
    rows = db.execute("SELECT * FROM raid_results WHERE guild_id=? AND log_ref IS NOT NULL "
                      "ORDER BY raid_number DESC LIMIT 50", (guild.id,)).fetchall()
    for row in rows:
        try:
            _, mid = row["log_ref"].split(":", 1)
            bot.add_view(build_log_view(guild.id, row, images=False), message_id=int(mid))
        except Exception as exc:
            print(f"Could not re-arm the buttons for raid {row['raid_number']}: {exc!r}")


def reset_leaderboard_once() -> None:
    if get_setting("leaderboard_reset_v3_1") is not None:
        return
    cur = db.execute("UPDATE raid_results SET counted=0")
    db.commit()
    set_setting("leaderboard_reset_v3_1", datetime.now(timezone.utc).isoformat())
    print(f"Fixed leaderboard cleared once: {cur.rowcount} old raid(s) no longer give points.")


# ----------------------------------------------------------------------------
# LIVE TICKET / BACKUP TRACKING
# ----------------------------------------------------------------------------
def live_lock(channel_id: int) -> asyncio.Lock:
    return LIVE_LOCKS.setdefault(channel_id, asyncio.Lock())


def is_live_channel(channel) -> bool:
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

    Normal ticket bots: only "<@member> has confirmed for help / has joined ..." pings count.
    Join bots (LIVE_JOIN_BOT_IDS): every human they ping, except the opener, is a helper."""
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
    join_bot = message.author.id in LIVE_JOIN_BOT_IDS
    if HELP_RE.search(text) or join_bot:
        ids = [int(x) for x in HELP_MENTION_RE.findall(text)]
        if join_bot or not ids:
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


def live_relevant(text: str, author_id: int | None = None) -> bool:
    if OPENED_RE.search(text) or LIVE_TOTAL_RE.search(text) or HELP_RE.search(text):
        return True
    return author_id in LIVE_JOIN_BOT_IDS and bool(MENTION_RE.search(text))


async def live_catch_up(st: dict, channel, skip_id: int | None = None) -> None:
    """Read the bots' earlier messages in a ticket channel (used when tracking starts late or on /sync)."""
    try:
        async for old in channel.history(limit=500, oldest_first=True):
            if old.id != skip_id and old.author.bot and not is_me(old.author):
                await live_apply(st, channel.guild, old)
    except discord.HTTPException:
        pass


async def live_bootstrap(channel) -> None:
    async with live_lock(channel.id):
        if live_load(channel.id) is not None:
            return
        st = live_new(channel)
        await live_catch_up(st, channel)
        live_refresh_divisions(st, channel.guild)
        live_store(st)
        await live_publish(channel.guild, st)


async def live_ingest(message: discord.Message, channel=None) -> None:
    channel = channel or message.channel
    text = post_text(message)
    if not live_relevant(text, message.author.id):
        if re.search(r"help|ticket|join", text, re.I) or message.author.id in LIVE_JOIN_BOT_IDS:
            print(f"[live] ignored a message from {message.author} in #{channel.name} (no pattern): "
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
    """A tracked bot's message outside a ticket (base/log channel): find the ticket it is about
    (a #channel mention, or the ticket's name in the text) and add the helpers there."""
    text = post_text(message)
    if not live_relevant(text, message.author.id):
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
    rows = db.execute("SELECT channel_id FROM live_tickets WHERE guild_id=? AND status='live'", (guild.id,)).fetchall()
    for row in rows:
        if guild.get_channel(row["channel_id"]) is None:
            await live_close(guild, row["channel_id"])


async def live_sync(guild: discord.Guild) -> None:
    """After a restart: start tracking tickets created while the bot was offline (newer than LIVE_SYNC_HOURS)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LIVE_SYNC_HOURS)
    for category_id in LIVE_CATEGORY_IDS:
        category = guild.get_channel(category_id)
        for ticket in getattr(category, "text_channels", []):
            if is_live_channel(ticket) and ticket.created_at >= cutoff:
                await live_bootstrap(ticket)


def ticket_channels(guild: discord.Guild) -> list[discord.TextChannel]:
    out = []
    for category_id in LIVE_CATEGORY_IDS:
        category = guild.get_channel(category_id)
        out += [c for c in getattr(category, "text_channels", []) if is_live_channel(c)]
    return out


async def live_sync_now(guild: discord.Guild) -> list[tuple[discord.TextChannel, int]]:
    """/sync: re-read EVERY ticket/backup channel in the ticket category (any age), rebuild the helper
    lists from the bots' messages and refresh each live post."""
    results = []
    for ticket in ticket_channels(guild):
        async with live_lock(ticket.id):
            st = live_load(ticket.id) or live_new(ticket)
            st["status"] = "live"
            st["name"] = ticket.name
            await live_catch_up(st, ticket)
            live_refresh_divisions(st, guild)
            live_store(st)
            await live_publish(guild, st)
            results.append((ticket, len(st["helpers"])))
    for base_id in LIVE_IGNORE_IDS:                 # tracked-bot messages posted in the base channels
        base = guild.get_channel(base_id)
        if base is None or not hasattr(base, "history"):
            continue
        try:
            async for m in base.history(limit=100, oldest_first=True):
                if m.author.bot and not is_me(m.author):
                    await live_from_log(m)
        except discord.HTTPException:
            pass
    return results


async def live_manual(guild: discord.Guild, channel, member: discord.Member, remove: bool) -> str:
    async with live_lock(channel.id):
        st = live_load(channel.id) or live_new(channel)
        if remove:
            before = len(st["helpers"])
            st["helpers"] = [h for h in st["helpers"] if h["user_id"] != member.id]
            result = "removed" if len(st["helpers"]) < before else "was not in the list"
        elif any(h["user_id"] == member.id for h in st["helpers"]):
            result = "is already in the list"
        else:
            st["helpers"].append({"user_id": member.id, "display_name": member.display_name,
                                  "division": division_of(member)[0]})
            result = "added"
        live_store(st)
        await live_publish(guild, st)
    return f"<@{member.id}> {result} in <#{channel.id}>."


# ----------------------------------------------------------------------------
# SETUP (/setup + when the bot joins a server)
# ----------------------------------------------------------------------------
def can_setup(user) -> bool:
    return user.id == OWNER_ID or (isinstance(user, discord.Member) and user.guild_permissions.manage_guild)


def setup_embed(guild: discord.Guild) -> discord.Embed:
    def show(key: str) -> str:
        cid = cfg_get(guild.id, key) or ENV_DEFAULTS[key]
        return f"<#{cid}>" if guild.get_channel(cid) else "not set"
    text = ("**Auto build** creates the channels for you under one category.\n"
            "**Choose channels** lets you pick your own: the channel you pick for logs gets the division logs, "
            "the leaderboard channel gets the fixed leaderboard, and so on.\n\n"
            f"MVP source: {show('source')}\nDivision logs: {show('log')}\nFixed leaderboard: {show('leaderboard')}\n"
            f"Live tracking posts: {show('live')}\nTicket category: {show('category')}")
    return make_embed(f"{BOT_NAME} setup", text)


async def make_text(guild: discord.Guild, category: discord.CategoryChannel, name: str, topic: str, read_only: bool):
    for existing in category.text_channels:
        if existing.name == name:
            return existing
    overwrites = {guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True,
                                                        attach_files=True, read_message_history=True,
                                                        use_external_emojis=True)}
    if read_only:
        overwrites[guild.default_role] = discord.PermissionOverwrite(send_messages=False)
    return await guild.create_text_channel(name, category=category, topic=topic, overwrites=overwrites,
                                           reason=f"{BOT_NAME} auto build")


async def auto_build(guild: discord.Guild) -> str:
    category = guild.get_channel(SETUP_CATEGORY_ID)
    if not isinstance(category, discord.CategoryChannel):
        category = await guild.create_category("URA | DIV", reason=f"{BOT_NAME} auto build")
    source = await make_text(guild, category, "raid-mvps", "Raid MVP logs from the MVP bot are posted here.", False)
    logs = await make_text(guild, category, "division-logs", "One post per MVP log.", True)
    board = await make_text(guild, category, "division-leaderboard", "The fixed live division leaderboard.", True)
    live = await make_text(guild, category, "live-backups", "Live tracking: who joined each backup.", True)
    tickets = guild.get_channel(cfg_get(guild.id, "category") or ENV_DEFAULTS["category"])
    if not isinstance(tickets, discord.CategoryChannel):
        tickets = await guild.create_category("Tickets", reason=f"{BOT_NAME} auto build")
    for key, ch in (("source", source), ("log", logs), ("leaderboard", board), ("live", live), ("category", tickets)):
        cfg_set(guild.id, key, ch.id)
    apply_config(guild.id)
    await refresh_leaderboard(guild)
    await live_sync_now(guild)
    return (f"Built under **{category.name}**:\nMVP source: {source.mention}\nDivision logs: {logs.mention}\n"
            f"Leaderboard: {board.mention}\nLive tracking: {live.mention}\nTicket category: **{tickets.name}**\n"
            f"Point your MVP bot at {source.mention}.")


class SetupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Auto build", emoji="\U0001F6E0", style=discord.ButtonStyle.success, custom_id="urdiv:setup_auto")
    async def auto_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or not can_setup(interaction.user):
            await interaction.response.send_message("Only the owner or someone with Manage Server can do this.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            text = await auto_build(interaction.guild)
            await interaction.followup.send(embed=make_embed("Auto build done", text, 0x57F287), ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send(embed=make_embed(
                "Auto build failed", "I need the **Manage Channels** permission (and Manage Roles is not needed).", 0xED4245),
                ephemeral=True)
        except discord.HTTPException as exc:
            await interaction.followup.send(embed=make_embed("Auto build failed", f"`{exc!r}`", 0xED4245), ephemeral=True)

    @discord.ui.button(label="Choose channels", emoji="\U0001F4CC", style=discord.ButtonStyle.primary, custom_id="urdiv:setup_choose")
    async def choose_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or not can_setup(interaction.user):
            await interaction.response.send_message("Only the owner or someone with Manage Server can do this.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=make_embed("Choose your channels", "Pick a channel in each menu. It is saved and used right away."),
            view=ChooseView(), ephemeral=True)


PICKS = [
    ("source", "MVP source channel (where the MVP bot posts)", [discord.ChannelType.text]),
    ("log", "Division logs channel", [discord.ChannelType.text]),
    ("leaderboard", "Fixed leaderboard channel", [discord.ChannelType.text]),
    ("live", "Live tracking posts channel", [discord.ChannelType.text]),
    ("category", "Ticket / backup category", [discord.ChannelType.category]),
]


class PickSelect(discord.ui.ChannelSelect):
    def __init__(self, key: str, label: str, types: list):
        super().__init__(placeholder=label, channel_types=types, min_values=1, max_values=1)
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None or not can_setup(interaction.user):
            await interaction.response.send_message("Only the owner or someone with Manage Server can do this.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel = guild.get_channel(self.values[0].id)
        if channel is None:
            await interaction.followup.send("I cannot see that channel.", ephemeral=True)
            return
        if isinstance(channel, discord.TextChannel):
            perms = channel.permissions_for(guild.me)
            if not (perms.view_channel and perms.send_messages and perms.read_message_history):
                await interaction.followup.send(embed=make_embed(
                    "Missing permissions", f"I need View, Send and Read History in {channel.mention}.", 0xED4245), ephemeral=True)
                return
        cfg_set(guild.id, self.key, channel.id)
        apply_config(guild.id)
        notes = {
            "source": f"MVP logs will be read from {channel.mention}.",
            "log": f"Division MVP logs will be posted in {channel.mention}.",
            "leaderboard": f"The fixed leaderboard is now posted in {channel.mention}.",
            "live": f"Live tracking posts will go to {channel.mention}.",
            "category": f"Every new channel in **{channel.name}** is now tracked as a backup/ticket.",
        }
        if self.key == "leaderboard":
            await refresh_leaderboard(guild)
        elif self.key in ("live", "category"):
            await live_sync_now(guild)
        await interaction.followup.send(embed=make_embed("Saved", notes[self.key], 0x57F287), ephemeral=True)


class ChooseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=600)
        for key, label, types in PICKS:
            self.add_item(PickSelect(key, label, types))


# ----------------------------------------------------------------------------
# BACKUPS (hourly DM to the owner + rolling copies on disk)
# ----------------------------------------------------------------------------
def make_backup() -> tuple[Path, Path, str, int]:
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
    for pattern in ("urdiv_*.sqlite3", "urdiv_*.json"):
        for old in sorted(BACKUP_DIR.glob(pattern))[:-KEEP_BACKUPS]:
            try:
                old.unlink()
            except OSError:
                pass
    return sqlite_path, json_path, integrity, len(raids)


async def send_backup_dm(reason: str = "hourly") -> str:
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
    if last and time.time() - int(last) < 50 * 60:
        return
    await send_backup_dm("hourly")


@hourly_backup.before_loop
async def _before_hourly_backup():
    await bot.wait_until_ready()


# ----------------------------------------------------------------------------
# EVENTS
# ----------------------------------------------------------------------------
async def prepare_guild(guild: discord.Guild) -> None:
    try:
        if not guild.chunked:
            await guild.chunk()
    except Exception as exc:
        print(f"Could not load the member list for {guild.id}: {exc!r}")
    apply_config(guild.id)
    await check_channels(guild)
    register_saved_views(guild)
    target = discord.Object(id=guild.id)
    bot.tree.copy_global_to(guild=target)
    await bot.tree.sync(guild=target)
    await refresh_leaderboard(guild)
    await live_reconcile(guild)
    await live_sync(guild)


@bot.event
async def on_ready():
    global STARTED
    print(f"{BOT_NAME} v{VERSION} online as {bot.user}  |  database: {DB_PATH}")
    if STARTED:
        return
    STARTED = True
    load_art()
    load_badges()
    bot.add_view(SetupView())
    await ensure_app_emojis()
    reset_leaderboard_once()
    if not hourly_backup.is_running():
        hourly_backup.start()
    guilds = [bot.get_guild(GUILD_ID)] if GUILD_ID else list(bot.guilds)
    for guild in guilds:
        if guild is not None:
            await prepare_guild(guild)


@bot.event
async def on_guild_join(guild: discord.Guild):
    """The bot was added to a server: register the commands and ask how to set it up."""
    try:
        target = discord.Object(id=guild.id)
        bot.tree.copy_global_to(guild=target)
        await bot.tree.sync(guild=target)
    except Exception as exc:
        print(f"Could not sync commands in {guild.id}: {exc!r}")
    channel = guild.system_channel
    if channel is None or not channel.permissions_for(guild.me).send_messages:
        channel = next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)
    embed = setup_embed(guild)
    embed.description = "**Thanks for adding me! Choose how to set me up.**\n\n" + (embed.description or "")
    if channel is not None:
        try:
            await channel.send(embed=embed, view=SetupView())
            return
        except discord.HTTPException:
            pass
    try:
        owner = bot.get_user(OWNER_ID) or await bot.fetch_user(OWNER_ID)
        await owner.send(f"I joined **{guild.name}** but cannot post there. Run `/setup` in that server.")
    except discord.HTTPException:
        pass


@bot.event
async def on_message(message: discord.Message):
    if message.guild is None:
        return
    if source_message(message):
        await process_mvp(message)
        return
    if not message.author.bot or is_me(message.author):
        return
    if is_live_channel(message.channel):
        if message.author.id not in LIVE_BOT_IDS:
            LIVE_SEEN[message.author.id] = str(message.author)
        await live_ingest(message)                      # any bot: only the ticket/join patterns count
    elif message.channel.id in LIVE_IGNORE_IDS or message.author.id in LIVE_JOIN_BOT_IDS:
        await live_from_log(message)


@bot.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
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
    if not isinstance(channel, discord.TextChannel) or not is_live_channel(channel):
        return
    await live_bootstrap(channel)


@bot.event
async def on_guild_channel_update(before, after):
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
# SLASH COMMANDS: TRUST + SETUP (owner only)
# ----------------------------------------------------------------------------
trust_group = app_commands.Group(name="trust", description="Choose who may use the bot's commands", guild_only=True)


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


@bot.tree.command(name="setup", description="Owner only: auto build the channels or choose your own")
@app_commands.guild_only()
async def setup_command(interaction: discord.Interaction):
    await interaction.response.send_message(embed=setup_embed(interaction.guild), view=SetupView(), ephemeral=True)


# ----------------------------------------------------------------------------
# SLASH COMMANDS: LIVE TRACKING
# ----------------------------------------------------------------------------
live_group = app_commands.Group(name="live", description="Live backup/ticket tracking", guild_only=True)


def pick_ticket(interaction: discord.Interaction, channel):
    channel = channel or interaction.channel
    return channel if is_live_channel(channel) else None


NOT_TICKET = ("That channel is not a tracked ticket/backup (it must be inside the ticket category). "
              "Run `/live status` to see what is watched.")


@live_group.command(name="status", description="Show what the live tracker is watching")
async def live_status(interaction: discord.Interaction):
    guild = interaction.guild
    watching = []
    for cid in sorted(LIVE_CATEGORY_IDS):
        channel = guild.get_channel(cid)
        if channel is None:
            watching.append(f"`{cid}`  NOT FOUND (wrong ID, or the bot cannot see it)")
        elif isinstance(channel, discord.CategoryChannel):
            tickets = [c for c in channel.text_channels if is_live_channel(c)]
            watching.append(f"Category **{channel.name}**  \u00b7  {len(tickets)} channel(s) now")
        else:
            watching.append(f"`{cid}` is not a category (set the category ID)")
    rows = db.execute("SELECT * FROM live_tickets WHERE guild_id=? AND status='live' ORDER BY created_at DESC LIMIT 10",
                      (guild.id,)).fetchall()
    open_tickets = [f"<#{r['channel_id']}>  \u00b7  helpers: {len(json.loads(r['helpers_json']))}" for r in rows]
    embed = make_embed("Live tracker")
    embed.add_field(name="Watching (every new channel in)", value="\n".join(watching), inline=False)
    embed.add_field(name="Base channels (not tickets)", value=" ".join(f"<#{i}>" for i in sorted(LIVE_IGNORE_IDS)) or "None", inline=False)
    embed.add_field(name="Bots read", value=" ".join(f"<@{i}>" for i in sorted(LIVE_BOT_IDS)), inline=True)
    embed.add_field(name="Join bots (every ping = helper)", value=" ".join(f"<@{i}>" for i in sorted(LIVE_JOIN_BOT_IDS)), inline=True)
    embed.add_field(name="Live posts", value=f"<#{LIVE_POST_ID}>", inline=True)
    embed.add_field(name="Open tracked", value="\n".join(open_tickets) if open_tickets else "None right now", inline=False)
    if LIVE_SEEN:
        embed.add_field(name="Other bots seen in ticket channels (not tracked)", inline=False,
                        value="\n".join(f"{name}  `{uid}`" for uid, name in LIVE_SEEN.items())[:900])
    await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=NO_PING)


@live_group.command(name="list", description="List every open live post with its helper count")
async def live_list(interaction: discord.Interaction):
    rows = db.execute("SELECT * FROM live_tickets WHERE guild_id=? AND status='live' ORDER BY created_at DESC",
                      (interaction.guild_id,)).fetchall()
    lines = []
    for r in rows:
        link = ref_url(interaction.guild_id, r["post_ref"], LIVE_POST_ID)
        lines.append(f"<#{r['channel_id']}>  \u00b7  helpers **{len(json.loads(r['helpers_json']))}**  \u00b7  [live post]({link})")
    await interaction.response.send_message(embed=make_embed(
        "Open live posts", "\n".join(lines)[:3900] if lines else "Nothing is being tracked right now."),
        ephemeral=True, allowed_mentions=NO_PING)


@live_group.command(name="add", description="Manually add a helper to a backup/ticket")
@app_commands.describe(member="Who joined", channel="The ticket channel (default: this one)")
async def live_add(interaction: discord.Interaction, member: discord.Member, channel: discord.TextChannel | None = None):
    target = pick_ticket(interaction, channel)
    if target is None:
        await interaction.response.send_message(NOT_TICKET, ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    await interaction.followup.send(await live_manual(interaction.guild, target, member, False),
                                    ephemeral=True, allowed_mentions=NO_PING)


@live_group.command(name="remove", description="Remove a helper from a backup/ticket (until the next /sync)")
@app_commands.describe(member="Who to remove", channel="The ticket channel (default: this one)")
async def live_remove(interaction: discord.Interaction, member: discord.Member, channel: discord.TextChannel | None = None):
    target = pick_ticket(interaction, channel)
    if target is None:
        await interaction.response.send_message(NOT_TICKET, ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    await interaction.followup.send(await live_manual(interaction.guild, target, member, True),
                                    ephemeral=True, allowed_mentions=NO_PING)


@live_group.command(name="close", description="Mark a backup/ticket as closed")
@app_commands.describe(channel="The ticket channel (default: this one)")
async def live_close_command(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    target = pick_ticket(interaction, channel)
    if target is None:
        await interaction.response.send_message(NOT_TICKET, ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    await live_close(interaction.guild, target.id)
    await interaction.followup.send(f"<#{target.id}> marked as closed.", ephemeral=True)


@live_group.command(name="refresh", description="Re-read one backup/ticket and refresh its live post")
@app_commands.describe(channel="The ticket channel (default: this one)")
async def live_refresh(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    target = pick_ticket(interaction, channel)
    if target is None:
        await interaction.response.send_message(NOT_TICKET, ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    async with live_lock(target.id):
        st = live_load(target.id) or live_new(target)
        st["status"] = "live"
        st["name"] = target.name
        await live_catch_up(st, target)
        live_refresh_divisions(st, interaction.guild)
        live_store(st)
        await live_publish(interaction.guild, st)
    await interaction.followup.send(f"<#{target.id}> refreshed. Helpers: **{len(st['helpers'])}**.", ephemeral=True)


@live_group.command(name="debug", description="Show what the bot sees from other bots in a channel")
@app_commands.describe(channel="Channel to inspect (default: this one)")
async def live_debug(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    channel = channel or interaction.channel
    await interaction.response.defer(ephemeral=True, thinking=True)
    lines = [f"Tracked ticket channel: **{'yes' if is_live_channel(channel) else 'NO'}**"]
    try:
        async for m in channel.history(limit=20):
            if not m.author.bot:
                continue
            text = post_text(m)
            join = "join bot" if m.author.id in LIVE_JOIN_BOT_IDS else ("listed" if m.author.id in LIVE_BOT_IDS else "not listed")
            match = "MATCH" if live_relevant(text, m.author.id) else "no pattern"
            preview = " ".join(text.split())[:140] or "(no readable text)"
            lines.append(f"**{m.author}** `{m.author.id}`  \u00b7  {join}  \u00b7  {match}\n> {preview}")
    except discord.Forbidden:
        lines.append("I cannot read the history of that channel.")
    await interaction.followup.send(embed=make_embed("Live debug", "\n".join(lines)[:3900]), ephemeral=True)


bot.tree.add_command(live_group)


@bot.tree.command(name="sync", description="Sync every ongoing backup/ticket into the live tracking")
@app_commands.guild_only()
async def sync_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    results = await live_sync_now(interaction.guild)
    if not results:
        await interaction.followup.send(embed=make_embed(
            "Nothing to sync", "No channels were found in the ticket category. Run `/live status` to check it.", 0xED4245),
            ephemeral=True)
        return
    lines = [f"<#{c.id}>  \u00b7  helpers joined: **{n}**" for c, n in results]
    await interaction.followup.send(embed=make_embed(
        f"Synced {len(results)} backup(s)", "\n".join(lines)[:3900] + f"\nLive posts: <#{LIVE_POST_ID}>", 0x57F287),
        ephemeral=True, allowed_mentions=NO_PING)


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
        "`/botinfo` - information about the bot"))
    embed.add_field(name="Live tracking", inline=False, value=(
        "`/sync` - sync every ongoing backup into the live tracking\n"
        "`/live status` `/live list` - what is watched / open posts\n"
        "`/live add` `/live remove` - fix a helper by hand\n"
        "`/live refresh` `/live close` - refresh or close one backup\n"
        "`/live debug` - see why a bot's message is or is not read"))
    embed.add_field(name="Management", inline=False, value=(
        "`/post` - post or refresh the fixed leaderboard\n"
        "`/test` - post a test log and a test live post; nothing is saved\n"
        "`/recount` - re-read MVP logs and fix the counts\n"
        "`/addraid` - record an MVP log the bot missed\n"
        "`/deleteraid` - remove a raid and its log\n"
        "`/preview` - preview scoring from typed counts\n"
        "`/status` - configuration and totals"))
    embed.add_field(name="Owner only", inline=False, value=(
        "`/setup` - auto build the channels or choose your own\n"
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
    embed.add_field(name="MVP awards", value=" \u00b7 ".join(f"D{d}: **{n}**" for d, n in totals.items()) + f"\n{lead_text(leaders_of(totals))}", inline=False)
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
            "Leaderboard not posted", f"I could not post in <#{LEADERBOARD_ID}>. Check the channel and that the bot "
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


@bot.tree.command(name="test", description="Preview the latest MVP log and a live post (nothing saved or counted)")
@app_commands.guild_only()
async def test_command(interaction: discord.Interaction):
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
    totals, _ = division_totals(guild_id)
    already = db.execute("SELECT 1 FROM raid_results WHERE message_id=?", (message.id,)).fetchone()
    if not already:
        for division in winners:
            totals[division] += 1
    lead = leaders_of(totals)

    log_msg = await post_new(LOG_ID, lambda images: RaidLogView(
        raid_number, raiders, skipped, winners, message.jump_url, images, test=True, lead=lead))
    sample = {"channel_id": 0, "name": "medium-backup-0129", "opener_id": interaction.user.id,
              "opener_division": division_of(interaction.user)[0] if isinstance(interaction.user, discord.Member) else None,
              "total": len(raiders), "status": "live", "post_ref": None,
              "helpers": [{"user_id": r["user_id"], "display_name": r["display_name"], "division": r["division"]} for r in raiders]}
    live_msg = await post_new(LIVE_POST_ID, lambda images: LiveTicketView(
        guild_id, sample, images, ticket_url=message.jump_url, test=True))

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
            make_backup()
            async with PROCESS_LOCK:
                src.backup(db)
        finally:
            src.close()
        ensure_schema()
        apply_config(interaction.guild_id)
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
        embed = make_embed("Not allowed", "Only the owner and trusted users can use this command.", 0xED4245)
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
# START
# ----------------------------------------------------------------------------
if not TOKEN or TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
    raise RuntimeError("Set your Discord bot token in .env before starting the bot.")
bot.run(TOKEN)