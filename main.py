"""URA | DIV: role-based division MVP tracker + live ticket tracker.

Needs: discord.py >= 2.6 (Components V2) and python-dotenv. Pillow is NOT needed:
the five division badges are built into this file (a file in ./badges overrides them).

Channels
  LOG_CHANNEL_ID          -> the division MVP log (one post per MVP log, edited when recounted)
  LEADERBOARD_CHANNEL_ID  -> the fixed live division leaderboard (one post, edited after every log)
  LIVE_POST_CHANNEL_ID    -> live ticket posts (one per ticket, edited as helpers join)
  LIVE_SOURCE_IDS         -> category (or channel) where the ticket bot creates its tickets

Access
  Only OWNER_ID and users added with /trust add can use commands.
  Only OWNER_ID can press the Edit button on an MVP log or use /trust, /backup, /restore.
"""
import asyncio
import base64
import io
import json
import os
import platform
import re
import sqlite3
import time
from datetime import datetime, timezone
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
LIVE_SOURCE_IDS = env_ids("LIVE_SOURCE_IDS") or {1562782175927992340}     # ticket category (or channel) to watch
LIVE_BOT_IDS = env_ids("LIVE_BOT_IDS") or {1470577242676531372, 1563126014702395542}   # bots whose ticket messages are read

RAID_START_NUMBER = env_int("RAID_START_NUMBER", 1)
VERSION = "3.0-live"
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
RAID_RE = re.compile(r"\bRaid\b[\s`*_:\-]*#\s*0*(\d+)", re.I)
MENTION_RE = re.compile(r"<@!?(\d+)>")
OPENED_RE = re.compile(r"Ticket opened by\s*<@!?(\d+)>", re.I)
LIVE_TOTAL_RE = re.compile(r"Live Helpers\D{0,12}(\d+)", re.I)
HELP_RE = re.compile(r"Help Registered|confirmed for help", re.I)
FOOTER_QUOTE = "> **Dominate Division. Claim the MVP.**"
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


def load_badges() -> None:
    BADGES.clear()
    BADGE_NAMES.clear()
    BADGE_SOURCE.clear()
    for d in range(1, 6):
        path = find_badge_file(d)
        if path is not None:
            BADGES[d] = path.read_bytes()
            BADGE_NAMES[d] = f"division{d}{path.suffix.lower()}"
            BADGE_SOURCE[d] = "file"
        elif d in EMBEDDED_BADGES:
            BADGES[d] = base64.b64decode(EMBEDDED_BADGES[d])
            BADGE_NAMES[d] = f"division{d}.png"
            BADGE_SOURCE[d] = "built-in"
    print(f"Badges: {len(BADGES)}/5 loaded  (custom files: {sum(1 for v in BADGE_SOURCE.values() if v == 'file')}, "
          f"built-in: {sum(1 for v in BADGE_SOURCE.values() if v == 'built-in')})")


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
    totals = {d: 0 for d in range(1, 6)}
    clause, channel_params = source_db_filter()
    params = (guild_id, *channel_params)
    for row in db.execute(f"SELECT winners_json FROM raid_results WHERE guild_id=? AND {clause}", params):
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
            except discord.HTTPException:
                old = None
            if old is not None:
                await old.edit(view=view, attachments=files)
                return ref
    sent = await channel.send(view=view, files=files, allowed_mentions=NO_PING)
    return f"{channel.id}:{sent.id}"


async def send_or_edit(channel_id: int, ref: str | None, build, with_files: bool = True) -> str | None:
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
            return await _deliver(channel, ref, build(images), badge_files() if images else [])
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
        await interaction.response.send_message(
            view=LeaderboardView(totals, channel_url(interaction.guild_id, LOG_ID)), ephemeral=True)


# ----------------------------------------------------------------------------
# LAYOUTS (Components V2)
# ----------------------------------------------------------------------------
def accent_for(winners: list[int]) -> int:
    return DIVISION_COLORS[winners[0]] if len(winners) == 1 else BRAND_COLOR


class LeaderboardView(discord.ui.LayoutView):
    """The permanent "LIVE DIVISION LEADERBOARD"."""

    def __init__(self, totals: dict[int, int], history_url: str, test: bool = False):
        super().__init__(timeout=None)
        ordered = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
        top = ordered[0][1]
        leaders = [d for d, points in ordered if top > 0 and points == top]

        board = discord.ui.Container()
        board.add_item(discord.ui.TextDisplay("# \U0001F4CA LIVE DIVISION LEADERBOARD"))
        board.add_item(discord.ui.TextDisplay("`URA DIVISIONS`" + ("\n-# TEST PREVIEW  -  not counted anywhere" if test else "")))
        board.add_item(discord.ui.Separator())
        for rank, (d, points) in enumerate(ordered, start=1):
            board.add_item(discord.ui.TextDisplay(f"- #{rank}  **DIVISION {d}**  \u279c  `{points:02d}` **MVPS**"))
            board.add_item(discord.ui.Separator())
        leading = "  \u00d7  ".join(f"`DIVISION {d:02d}`" for d in leaders) if leaders else "`No MVPs yet`"
        board.add_item(discord.ui.TextDisplay(f"### __Leading Division__ - {leading}"))
        board.add_item(discord.ui.Separator())
        board.add_item(discord.ui.TextDisplay(f"\u27a4 [MVP History]({history_url})"))
        board.add_item(discord.ui.TextDisplay(FOOTER_QUOTE))
        stamp = int(datetime.now(timezone.utc).timestamp())
        board.add_item(discord.ui.TextDisplay(f"-# Updated <t:{stamp}:R>  \u00b7  {BOT_NAME}"))
        self.add_item(board)


class RaidLogView(discord.ui.LayoutView):
    """The per-raid division log: divisions always listed 1 to 5, the MVP tag moves to the winner(s).
    Options: Info (opens the MVP log), Live Leaderboard (private), Edit (owner only)."""

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
        notes = []
        if skipped:
            listing = fit_list([f"{who} ({why})" for who, why in skipped], 300)
            notes.append(f"### NOT COUNTED - `{len(skipped):02d}`\n{listing}")
        multi = sum(1 for r in raiders if r.get("multi"))
        if multi:
            notes.append(f"-# {multi} raider(s) hold more than one division role and were counted for their highest.")
        stamp = int(datetime.now(timezone.utc).timestamp())
        notes.append(f"-# Logged <t:{stamp}:f>  \u00b7  {BOT_NAME}")
        panel.add_item(discord.ui.Separator())
        panel.add_item(discord.ui.TextDisplay("\n".join(notes)))
        row = discord.ui.ActionRow()
        row.add_item(discord.ui.Button(label="Info", url=info_url))
        row.add_item(LeaderboardButton())
        if not test:
            row.add_item(EditButton(raid_number))
        panel.add_item(row)
        self.add_item(panel)


def build_log_view(guild_id: int, row, images: bool = True) -> RaidLogView:
    """Rebuild the MVP log from a saved raid (used to re-arm the Edit button after a restart)."""
    raiders = json.loads(row["raiders_json"])
    skipped = json.loads(row["skipped_json"] or "[]")
    _, winners = tally(raiders)
    return RaidLogView(int(row["raid_number"]), raiders, skipped, winners,
                       channel_url(guild_id, row["channel_id"], row["message_id"]), images)


class LiveTicketView(discord.ui.LayoutView):
    """One live post per ticket: helpers by division, edited every time someone joins."""

    def __init__(self, guild_id: int, st: dict, images: bool = True, ticket_url: str | None = None, test: bool = False):
        super().__init__(timeout=None)
        live = st["status"] == "live"
        by_division: dict[int, list[str]] = {d: [] for d in range(1, 6)}
        unassigned = 0
        for helper in st["helpers"]:
            if helper.get("division"):
                by_division[int(helper["division"])].append(f"<@{helper['user_id']}>")
            else:
                unassigned += 1
        counted = len(st["helpers"])

        panel = discord.ui.Container(accent_colour=0x57F287 if live else 0x747F8D)
        title = "## LIVE DIVISION RAID" if live else "## DIVISION RAID  -  CLOSED"
        if test:
            title += "\n-# TEST PREVIEW  -  not tracked"
        panel.add_item(discord.ui.TextDisplay(title))

        if live or test:
            button = discord.ui.Button(label="Open Ticket", url=ticket_url or channel_url(guild_id, st["channel_id"]))
        else:
            button = discord.ui.Button(label="Ticket Closed", style=discord.ButtonStyle.secondary, disabled=True)
        if st.get("opener_id"):
            opener = f"<@{st['opener_id']}>" + (f"  (D{st['opener_division']})" if st.get("opener_division") else "")
        else:
            opener = "waiting for the ticket message"
        total_line = f"**LIVE HELPERS** - `{counted:02d}`"
        if st.get("total") is not None and int(st["total"]) != counted:
            total_line += f"  (ticket shows `{int(st['total']):02d}`)"
        panel.add_item(discord.ui.Section(
            f"### TICKET  `#{st['name']}`\n{total_line}\n**Opened by** {opener}", accessory=button))
        panel.add_item(discord.ui.Separator())
        for d in range(1, 6):
            members = by_division[d]
            body = fit_list(members, 500) if members else "No helpers yet"
            panel.add_item(division_section(f"### DIVISION `{d:02d}`  \u2192  `{len(members):02d}`\n{body}", d, images))
        panel.add_item(discord.ui.Separator())
        notes = []
        if unassigned:
            notes.append(f"-# {unassigned} helper(s) have no division role and are not counted by division.")
        stamp = int(datetime.now(timezone.utc).timestamp())
        notes.append(f"-# {'Live' if live else 'Closed'}  \u00b7  Updated <t:{stamp}:R>  \u00b7  {BOT_NAME}")
        panel.add_item(discord.ui.TextDisplay("\n".join(notes)))
        self.add_item(panel)


# ----------------------------------------------------------------------------
# FIXED LEADERBOARD + MVP PROCESSING
# ----------------------------------------------------------------------------
async def refresh_leaderboard(guild: discord.Guild) -> discord.Message | None:
    """Create or edit the one fixed leaderboard post. Returns the message (or None if it failed)."""
    key = f"leaderboard_fixed:{guild.id}"
    saved = get_setting(key)
    totals, _ = division_totals(guild.id)
    history_url = channel_url(guild.id, LOG_ID)
    try:
        ref = await send_or_edit(LEADERBOARD_ID, saved, lambda images: LeaderboardView(totals, history_url),
                                 with_files=False)
    except discord.HTTPException as exc:
        print(f"Could not update the fixed leaderboard: {exc!r}")
        return None
    if not ref or ":" not in ref:
        return None
    if ref != saved:
        set_setting(key, ref)
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
    for cid in LIVE_SOURCE_IDS:
        channel = guild.get_channel(cid)
        kind = "NOT FOUND" if channel is None else type(channel).__name__
        print(f"  Ticket source (LIVE_SOURCE_IDS): {cid}  {kind}")
    if len(set(routes.values())) < 3:
        print("  WARNING: two of the channel IDs are identical, so those posts will land in the same channel.")
    print("-------------------------")


def register_saved_views(guild: discord.Guild) -> None:
    """Re-arm the Edit and Live Leaderboard buttons on recent MVP logs after a restart."""
    rows = db.execute("SELECT * FROM raid_results WHERE guild_id=? AND log_ref IS NOT NULL "
                      "ORDER BY raid_number DESC LIMIT 50", (guild.id,)).fetchall()
    for row in rows:
        try:
            _, mid = row["log_ref"].split(":", 1)
            bot.add_view(build_log_view(guild.id, row, images=False), message_id=int(mid))
        except Exception as exc:
            print(f"Could not re-arm the buttons for raid {row['raid_number']}: {exc!r}")


# ----------------------------------------------------------------------------
# LIVE TICKET TRACKING
# ----------------------------------------------------------------------------
def live_lock(channel_id: int) -> asyncio.Lock:
    return LIVE_LOCKS.setdefault(channel_id, asyncio.Lock())


def is_live_channel(channel) -> bool:
    """True for a ticket channel inside a watched category (or a watched channel/thread)."""
    if channel is None:
        return False
    if channel.id in {MVP_CHANNEL_ID, LOG_ID, LEADERBOARD_ID, LIVE_POST_ID}:
        return False
    if channel.id in LIVE_SOURCE_IDS:
        return True
    if getattr(channel, "category_id", None) in LIVE_SOURCE_IDS:
        return True
    return getattr(channel, "parent_id", None) in LIVE_SOURCE_IDS


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
    """Read one ticket-bot message into the ticket state. Returns True if anything changed."""
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
        ids = [u.id for u in message.mentions] + [int(x) for x in MENTION_RE.findall(text)]
        known = {h["user_id"] for h in st["helpers"]}
        for user_id in dict.fromkeys(ids):
            if user_id in known or user_id == message.author.id:
                continue
            member = await get_member(guild, user_id)
            if member is None or member.bot:
                continue
            division, _ = division_of(member)
            st["helpers"].append({"user_id": member.id, "display_name": member.display_name, "division": division})
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


def live_relevant(text: str) -> bool:
    return bool(OPENED_RE.search(text) or LIVE_TOTAL_RE.search(text) or HELP_RE.search(text))


async def live_ingest(message: discord.Message) -> None:
    """A ticket-bot message appeared (or was edited) in a ticket channel."""
    channel = message.channel
    if not live_relevant(post_text(message)):
        return
    async with live_lock(channel.id):
        st = live_load(channel.id)
        fresh = st is None
        if fresh:
            st = live_new(channel)
            try:
                async for old in channel.history(limit=100, oldest_first=True):   # catch up on earlier messages
                    if old.id != message.id and old.author.id in LIVE_BOT_IDS:
                        await live_apply(st, channel.guild, old)
            except discord.HTTPException:
                pass
        elif st["status"] != "live":
            st["status"] = "live"
            fresh = True
        changed = await live_apply(st, channel.guild, message)
        if fresh or changed:
            await live_publish(channel.guild, st)


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


@bot.event
async def on_message(message: discord.Message):
    if message.guild is None:
        return
    if source_message(message):
        await process_mvp(message)
    elif message.author.bot and is_live_channel(message.channel):
        if message.author.id in LIVE_BOT_IDS:
            await live_ingest(message)
        elif bot.user is None or message.author.id != bot.user.id:
            LIVE_SEEN[message.author.id] = str(message.author)


@bot.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    """The MVP bot may edit its log, and the ticket bot edits its ticket message (Live Helpers count)."""
    if payload.channel_id in SOURCE_IDS:
        message = await try_get_message(payload.channel_id, payload.message_id)
        if message is not None and message.guild and source_message(message):
            await process_mvp(message)
        return
    channel = bot.get_channel(payload.channel_id)
    if channel is not None and is_live_channel(channel):
        message = await try_get_message(payload.channel_id, payload.message_id)
        if message is not None and message.author.id in LIVE_BOT_IDS:
            await live_ingest(message)


@bot.event
async def on_guild_channel_create(channel):
    """A new ticket channel appeared: post its live message right away."""
    if not isinstance(channel, discord.TextChannel) or not is_live_channel(channel):
        return
    async with live_lock(channel.id):
        if live_load(channel.id) is None:
            st = live_new(channel)
            live_store(st)
            await live_publish(channel.guild, st)


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
    for cid in sorted(LIVE_SOURCE_IDS):
        channel = guild.get_channel(cid)
        if channel is None:
            watching.append(f"`{cid}`  NOT FOUND (wrong ID, or the bot cannot see it)")
        elif isinstance(channel, discord.CategoryChannel):
            watching.append(f"Category **{channel.name}**  \u00b7  {len(channel.text_channels)} text channels")
        else:
            watching.append(f"<#{cid}>  (single channel)")
    rows = db.execute("SELECT * FROM live_tickets WHERE guild_id=? AND status='live' ORDER BY created_at DESC LIMIT 10",
                      (guild.id,)).fetchall()
    open_tickets = [f"<#{r['channel_id']}>  \u00b7  helpers: {len(json.loads(r['helpers_json']))}" for r in rows]
    embed = make_embed("Live ticket tracker")
    embed.add_field(name="Watching", value="\n".join(watching), inline=False)
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
    await interaction.response.send_message(view=LeaderboardView(totals, channel_url(interaction.guild_id, LOG_ID)),
                                            ephemeral=True)


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
    await interaction.followup.send(view=LeaderboardView(totals, channel_url(guild_id, LOG_ID), test=True), ephemeral=True)


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
# BUILT-IN DIVISION BADGES (160x160 PNG, base64). Put your own divisionN.png in ./badges to override.
# ----------------------------------------------------------------------------
EMBEDDED_BADGES = {
    1: (
        "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"
    ),
    2: (
        "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"
    ),
    3: (
        "iVBORw0KGgoAAAANSUhEUgAAAKAAAACgCAMAAAC8EZcfAAAA/1BMVEUXkzgJeRUYj0MyYkocl0UYkkQZlEcoSTxYp3dhvIRB"
        "eVp78qYXgD8f1GEwLEMA//9/f39VqlUQExsWGiQbISwAAAAYk0UNCRVu2JUWWTMixF0kzGIAqlUAfz8jKTUXjEIYi0Iai0UZ"
        "fEAWNioZZjkvNkQXej4VSC0j0WJz45whuVkoMjxWpnZQmW4oaEgPIx4gp1RmxooWDSFJjGUdw1keplIAf38XjEIhmFAA/wAg"
        "HS946qEXjEIAVVUdaEAYjEIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAADS8J3wAAAAQHRSTlMSA6b/HVvc///////+//8BAgP///8A/f//////AwT/QZD9////////////////////////"
        "//8CdP8B///RA/8xTC6JDQAAC7lJREFUeNrtnQt3oroWx+1zHuc+k0gErC2CgtiqM3O07dTOfP9vdffOSwREECzctSar00OT"
        "GH7u7PyzEzmx1+946uXmXgxbSJuSgJu7TWvmuhseA3y9w9/XvZv7D083v68F46YIEN/A9c2l51ktJM/aPtwAz/D1EODra793"
        "s/Wwamvp9uZ3v/+eDwi2vd9KOm/cQhI3hl9gxWEe4LDfu8QKXmD7rJXkh4GHBNte/y4LCHy3FuL5lNJ2APHGtoddeN+/SAPe"
        "9e+RD/FYmwkRwYjPppd72v966ABhy3gC0Ucj3uteloCb194W+Pz28YQRp4LwPQE43OD48CcjVWcyURcjfcFM2WhXZqrPD1c/"
        "qakp6M31+8YADvv3wBxSXYuNdleZrKIyVq7sWFPQy551KTsZAV8vejBygk70r+rlUHTyUAHe9Z8744CaEDr50gBuLrbdMqAy"
        "4W9Uwx7+9LpmQGnCZwWIPdwxAwoTWlsF+D68tCy7a4AwkL1rIEQf/C+MYdHDNE810xc5ZRWrl2vKQyeECAEgf8MsJ6pMMgK1"
        "U9f5SDZCR/OM4OqyXDE+tamxJWbknlBpa0z3W82bGk4EPLWphWU9wPjQgKxridp/AP8A/gGsAjhVqxazfMmqa6kyVq16UVke"
        "oBYjOtJqlIxAlXjlBaXmBiOq1oS1m6JBBpDuFHQy0VI60S8wZXNdlqg+19UXLy9jKKnfVFqop834jRe5brRi9Gw+yAqco2DS"
        "V9WpHTmQIo/WbuoAYN03/eI6IjUR/DYPSOl3DcgaWP83DIhjNxzHks95setv8DQIiMri//JiB+yHPy7+vIxDWgsyDUhPScZ0"
        "LxIriq3YsdZuJBhXY5vtalVtuiwgK4ajvg2mE0DuehoSvmSc+GMLsyAvfglCU5nVBkS5lAkUVCZUV5lM2Wii6SaftOnc2Ap8"
        "wjmHfwT/i9yu7O+VF7KCpuj+nXdlixzAkW5ipKvJLDVl6TLZceFtLOgiN/bsJcIlEvy5hEHjusqQOGpG2uqqKVyv7N0meWf2"
        "81dpH2SJrjZj4kUaKHJExxo6vndJ/MCKI2XIcciSDindt74PYlUWhmJ+1WPCW0kXi2Iv8NNI/nLHi2VL0dlQPVKGlJDQUhj6"
        "BS5eGhDW+CvoJJuqMWFp08UwSPfhlj7cE/6FbN+mJByvHW3IX9KQ1Af3jccHCSuMYl/6Ufgd5GTlSLp4rcZEwnQA5ssuZniZ"
        "MiR0tngt/Fp5aMgY5+3o1yHCCoCWmL+gWanEYkykTMfQdMZsomNzDAm+EbvKkC+enBVjdoCwgg/GaoKVLe+NiVyUfex9QxJ/"
        "unaU/MhGw/qAK9mWEDub7d8vzUD2IaVXpl9j48hWgI5fyQdzdHCugjx37YXLvRulb05yGVW9lCFDb+3o0PGAAv9cFM4kcyPx"
        "jI5Rji2eNR0ppkuNoH1Dcgs1fsV2tykxkxxKoQdvlVcy3VFDchglK5s2IdQo0QpQS0gVuDxDSsAo+N7ITILyrAArmy7LqHwD"
        "Ad1ftGnAYHkyXDKKCM4GaJOaeAIRmvkD2BRgUcoDZLt4fi8YPBNgQFlmZbG7c5WQ/zyAUZAI+Y1Qz4uEulMWDM7jg8dlKAlY"
        "1QfrA3I256dY8OMAnwbH6rQKyL/OZo9HKn0gYAaEX70NBm8/OgOYJuSfBphmX9P5hwC/n3eQ8B+LlEG/zSThp/38+VPydXUA"
        "d+HsLqIuAHz66yqpKvyz5IPk8z27fssFjIKfWaGeF0bUuWuSAgvO3hIjAv7UfLMvZLfcvJodBtR2oPPMnemigS6eDWbftLHE"
        "ADGEjzqXPAL2t6a6uDrgYDa44okBYgjlUOb+FzRrm4DA8oTbgmywn95wAPHFQFRpFxD6k3H2ZZBOYFk9aloGBEecP75lAAfz"
        "J53ZNuBgMBvkJZPbPuCR1Big0UFm1Gh0PsAooPu32UPIDVizIX/hTFIbMC/kP2Vvpjtd/AfwowHfyvHNOm/BLy0Blv+wtZ2I"
        "WuzklksthfyLz4/lUvMhfzmhfvprMCuVGhPqUXaHZFQIWI7v7dAopiwz1TW5N8MXj0/l0o8DPsib90FSf5CQswI2u/3WNCC3"
        "GyAEPps3Dug6cYhoYVCXkJNAGDB0nKg5QDqOXCfA9+0HS15nAxP4cCHNA+Bbs8obmCRbTWYRGxqcSkKfFwKwwmIm+aaR446z"
        "t0kmngc4P7D1AdVDVxEuCwn5k1vgBPD2mOKLxjy5z2H2QIq3PiZZQK31368cx7Xg5oWEfPGmPxfN5wMH4cRzJR8xe1R0Qst8"
        "XlyYCFu5kSAENz/AgBtF7j8PEfJQ8lmR44T82P145YfLkFDaEIUi9xkAH2I/92/Xzn9AQIgUJ2u3DB895ek3JIx9SZjjaZzh"
        "Ut3924lyCNVrOFsLyaJnAaRkDL0jhmGOIHKOO20I6DiZoQx+ESKfj8/NMELPBEg5CKKQbCBMQ0g+AejGjGf4hEiBWpXkI6c9"
        "4EgCB+I4bhQt+QmEXEIJC7rrPSeFyoIP1NT1yvHlA5qXEpJRUKIFUcxRGUK+UCsoAehEFs/KX6Dkj9BDrSfuzAs/js37lM+8"
        "0HXcqRTEcLdBPdcrOAko30Q+X6nbHNibUeYn84m6AKEmOhZXWaMrFETOpWPxFJ8G3BFKd+VkDPJik/2mJtp+5s67ssXpD9my"
        "2HUloZIbTr/M0oCOG8onWcSA5xzk2S0lL7VGcUKy12gWJTcww2UsKN0Q3oPhcyrx8TqPKRP8vwqElGjCx4wFRbmI/tBdLRf/"
        "ph8FSCEgjsSkokYAZ98ygKHxUo4+UZGvJiAgQkgiJl2QGyT8NEuN4rEZ58DnlJ0+mgOUcmMbQTQfNElAUGoTnYYxLGkIoR8M"
        "KAkDLTfGDSUgdKiWPzvCJdcJzZcEJIdicmgCTaMIjRvKqS7karbGt+GMyZGmcjJzABNyOZqnhJok1VWX/cu/dV1JiGLyyQQL"
        "4IBa/gLhCMeaono+2Al1TkRN6EhXY5pUZxEy0llstCsTkr3UgvhDhVvummj58yLFd7QpzWzunJrqTlzlMilwKp7/LANWCAel"
        "/CEfRqcntJzpYpm7Kz98kcyCS8t1JKE/XVJhQQiopfypMlqqqdRFPuAJbxQIdQwLhJ9w0TQmWv6kPJ/WcEOASBhoQYQF6de3"
        "yFLyh/JsMUJJu4DoiIYQTPfZDbU84yr1VL5jPkhL+qBuC5ZCYxm7XL3YWv5cT7hplaaa90HVmG8kOxQRopplarWZA4hqJNNo"
        "pOvtsvSHHSynjPrQoZJQ/ECn68Vx1aZ0WZ4O4kySAkT5l2mis0D+VROmbD7i/hpi2KV+yhyiU714r9yUukjPJPU3TkGyV8J8"
        "Qp79uluxTfqgSiCIsA7gRMeynQOEdZvrhgSnZ1Z/s/0cgDg24lhugXUSUKzNHZRn0lFAnD+ippo6CyBGXfyMgIVTHS1Vpne1"
        "ajeVO5MYoZ5PKqlrokzPz7WbyhXq7FujVS14OKtiU+Q8Pthc4v8/gBs8lOkf3QMM8FCmO3GsFR5NxzpHOLXE8XQAiIcjWiHv"
        "GB+eu3WNJ0vi0WoPlhV0DJD7lrX9jzw9TxyP6HXQBR/w4F/sYnBCT/RxNqJOhLqmbJ4pS0TNJC3UJzalT1ZLHJAo1hYnrknM"
        "QkKX1W0KDbi92KgDEi/618Brd8gLORNnsd7pQzqlCRnvlMZse+/mFNF/X1xvuyTW0MHogTtAuPyN5+ySbtiQ4wGYz+oo254+"
        "qhhPYp12oZc5Qfs97B+1iwPlGc9SDnnLiJyLg3Yv+6/p4557/Rs8LHvqkxYZAQ+PpLYe3ocZQPBDceCzNQ2ZCN1l+qkvTFZh"
        "mcn6ScpU3y8jzBd43nPfGDB55PgdHumNiN50vLBV2l0E6iLIKVtky4J02dGmgqk8jv32vr/JP7QdJpb7yzbPbMcb3z73EweO"
        "p469fx3iqfe3rR0p720v79Xh+4e+OOBdnsz/fPnw4eny+eZ6mMbL+eqFzV2bX1WR/e6FvC+veB3etZJyv76i1/Wv//gfIX53"
        "nRejiugAAAAASUVORK5CYII="
    ),
    4: (
        "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"
    ),
    5: (
        "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"
    ),
}


if not TOKEN or TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
    raise RuntimeError("Set your Discord bot token in .env before starting the bot.")
bot.run(TOKEN)