"""URA | DIV: role-based division MVP tracker.

Needs: discord.py >= 2.6 (Components V2), python-dotenv. Pillow is optional
(only used to draw badges if the files in ./badges are missing).

Channels
  RAID_UPDATE_CHANNEL_ID  -> the main "DIVISION MVP'S" post for every MVP log
  LEADERBOARD_CHANNEL_ID  -> the permanent "LIVE DIVISION LEADERBOARD" (edited in place)
  LOG_CHANNEL_ID          -> the detailed division log, posted after the MVP is registered

Access
  Only OWNER_ID and users added with /trust add can use commands.
  Only OWNER_ID can press the Edit button on an MVP post or use /trust.
"""
import asyncio
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
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()


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

RAID_UPDATE_ID = env_int("RAID_UPDATE_CHANNEL_ID", 1565732391107170364)   # main "DIVISION MVP'S" post
LEADERBOARD_ID = env_int("LEADERBOARD_CHANNEL_ID", 1565732417271234791)   # permanent leaderboard
LOG_ID = env_int("LOG_CHANNEL_ID", 1565771359097716766)                   # division logs
MVP_BOT_ID = env_int("MVP_BOT_ID", 1563126014702395542)                   # "Raid MVPs" app
SOURCE_IDS = env_ids("MVP_CHANNEL_IDS") or {1524379574585000006}          # where MVP logs are posted
MVP_CHANNEL_ID = sorted(SOURCE_IDS)[0]

RAID_START_NUMBER = env_int("RAID_START_NUMBER", 1)
DB_PATH = os.getenv("PIRO_DB_PATH", "piro.sqlite3")
BADGE_DIR = Path(os.getenv("PIRO_BADGE_DIR", str(Path(__file__).resolve().parent / "badges")))

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
    that tab, so it is ignored and the badge file from ./badges is attached instead."""
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
FOOTER_QUOTE = "> **Dominate Division. Claim the MVP.**"
NO_PING = discord.AllowedMentions.none()
START_TIME = time.time()

# ----------------------------------------------------------------------------
# DATABASE
# ----------------------------------------------------------------------------
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
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
db.commit()


def is_trusted(user_id: int) -> bool:
    return db.execute("SELECT 1 FROM trusted WHERE user_id=?", (user_id,)).fetchone() is not None


# ----------------------------------------------------------------------------
# BOT (every slash command is locked to the owner and trusted users)
# ----------------------------------------------------------------------------
class GuardedTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        uid = interaction.user.id
        if uid == OWNER_ID:
            return True
        command = interaction.command
        root = command.root_parent.name if command is not None and command.root_parent else getattr(command, "name", "")
        if root != "trust" and is_trusted(uid):
            return True
        raise app_commands.CheckFailure("not allowed")


intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents, help_command=None,
                   tree_cls=GuardedTree)

PROCESS_LOCK = asyncio.Lock()
STARTED = False


# ----------------------------------------------------------------------------
# DIVISION BADGES (five separate images, one per division)
# ----------------------------------------------------------------------------
BADGES: dict[int, bytes] = {}


def rgb_of(d: int) -> tuple[int, int, int]:
    color = DIVISION_COLORS[d]
    return (color >> 16) & 255, (color >> 8) & 255, color & 255


def draw_badge(d: int) -> bytes | None:
    """Fallback shield badge, used only when badges/divisionN.png is missing."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    try:
        S = 256
        rgb = rgb_of(d)
        light = tuple(min(255, int(c * 0.6 + 255 * 0.4)) for c in rgb) + (255,)
        img = Image.new("RGBA", (S, S), (17, 20, 28, 255))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([4, 4, S - 5, S - 5], radius=28, outline=rgb + (255,), width=5)
        cx = S // 2
        shield = [(cx - 62, 56), (cx + 62, 56), (cx + 62, 136), (cx, 208), (cx - 62, 136)]
        draw.polygon(shield, fill=(14, 17, 24, 255))
        draw.line(shield + [shield[0]], fill=rgb + (255,), width=7, joint="curve")
        crown = [(cx - 34, 118), (cx - 34, 84), (cx - 17, 102), (cx, 78), (cx + 17, 102), (cx + 34, 84), (cx + 34, 118)]
        draw.polygon(crown, fill=light)
        draw.rectangle([cx - 34, 123, cx + 34, 128], fill=light)
        draw.polygon([(cx, 140), (cx + 20, 160), (cx, 180), (cx - 20, 160)], fill=light)
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception as exc:
        print(f"Could not draw badge for division {d}: {exc!r}")
        return None


def load_badges() -> None:
    for d in range(1, 6):
        path = BADGE_DIR / f"division{d}.png"
        if path.is_file():
            BADGES[d] = path.read_bytes()
            continue
        drawn = draw_badge(d)
        if drawn:
            BADGES[d] = drawn
        else:
            print(f"No badge for division {d}: put {path} next to piro.py (or install pillow).")


load_badges()


def badge_ref(d: int) -> str | None:
    if DIVISION_IMAGE_URLS[d]:
        return DIVISION_IMAGE_URLS[d]
    return f"attachment://division{d}.png" if d in BADGES else None


def badge_files() -> list[discord.File]:
    """Files are single-use, so call this again for every send."""
    return [discord.File(io.BytesIO(BADGES[d]), filename=f"division{d}.png")
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


def is_bad_form(exc: discord.HTTPException) -> bool:
    return exc.status == 400 and getattr(exc, "code", None) == 50035


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
                print(f"Discord rejected the badge images ({exc.text}); sending without thumbnails.")
                continue
            raise
    return ref


async def followup_view(interaction: discord.Interaction, build) -> bool:
    """Send a view as a follow-up; falls back to no thumbnails if Discord rejects the images.
    Returns True when the thumbnails were included."""
    for images in (True, False):
        try:
            await interaction.followup.send(view=build(images), files=badge_files() if images else [],
                                            allowed_mentions=NO_PING)
            return images
        except discord.HTTPException as exc:
            if images and is_bad_form(exc):
                print(f"Discord rejected the badge images ({exc.text}); sending without thumbnails.")
                continue
            raise
    return False


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
        member = message.guild.get_member(user_id)
        if member is None:
            try:
                member = await message.guild.fetch_member(user_id)
            except discord.HTTPException:
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


# ----------------------------------------------------------------------------
# LAYOUTS (Components V2)
# ----------------------------------------------------------------------------
def accent_for(winners: list[int]) -> int:
    return DIVISION_COLORS[winners[0]] if len(winners) == 1 else BRAND_COLOR


class RaidUpdateView(discord.ui.LayoutView):
    """The main "DIVISION MVP'S" post. Info opens the MVP log; Open Logs, Live Leaderboard
    and Edit (owner only) are the options at the bottom."""

    def __init__(self, raid_number: int, tallies: dict[int, int], winners: list[int], raider_count: int,
                 info_url: str, logs_url: str, board_url: str, images: bool = True, test: bool = False):
        super().__init__(timeout=None)
        order = sorted(range(1, 6), key=lambda d: (-tallies[d], d))
        panel = discord.ui.Container(accent_colour=accent_for(winners))
        panel.add_item(discord.ui.TextDisplay("## DIVISION MVP'S"))
        if test:
            panel.add_item(discord.ui.TextDisplay("-# TEST PREVIEW  -  not counted anywhere"))
        panel.add_item(discord.ui.Section(
            f"### RAID  `#{raid_number:04d}`\n**DIVISIONAL RAIDERS** - `{raider_count:02d}`",
            accessory=discord.ui.Button(label="Info", url=info_url)))
        panel.add_item(discord.ui.Separator())
        for rank, d in enumerate(order, start=1):
            n = tallies[d]
            tag = "  **MVP**" if d in winners else ""
            panel.add_item(division_section(
                f"### #{rank}  DIVISION `{d:02d}`  \u2192  `{n:02d}`{tag}\n"
                f"{'Member' if n == 1 else 'Members'} Joined", d, images))
        panel.add_item(discord.ui.Separator())
        if winners:
            names = "  \u00d7  ".join(f"__DIVISION {d:02d}__" for d in winners)
        else:
            names = "No MVP awarded"
        panel.add_item(discord.ui.TextDisplay(f"> `DIVISION MVP'S`\n### {names}"))
        row = discord.ui.ActionRow()
        row.add_item(discord.ui.Button(label="Open Logs", url=logs_url))
        row.add_item(discord.ui.Button(label="Live Leaderboard", url=board_url))
        if not test:
            row.add_item(EditButton(raid_number))
        panel.add_item(row)
        panel.add_item(discord.ui.TextDisplay("-# **Dominate Division. Claim the MVP.**"))
        self.add_item(panel)


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
        self.add_item(board)


class RaidLogView(discord.ui.LayoutView):
    """The detailed per-raid log with every counted raider, posted in the log channel."""

    def __init__(self, raid_number: int, raiders: list[dict], skipped: list, winners: list[int], source_url: str,
                 images: bool = True, test: bool = False):
        super().__init__(timeout=None)
        tallies, _ = tally(raiders)
        order = sorted(range(1, 6), key=lambda d: (-tallies[d], d))
        by_division: dict[int, list[str]] = {d: [] for d in range(1, 6)}
        for raider in raiders:
            by_division[int(raider["division"])].append(f"<@{raider['user_id']}>")

        panel = discord.ui.Container(accent_colour=accent_for(winners))
        panel.add_item(discord.ui.TextDisplay("## DIVISION MVP LOG"))
        if test:
            panel.add_item(discord.ui.TextDisplay("-# TEST PREVIEW  -  not counted anywhere"))
        mvp = "  \u00d7  ".join(f"DIVISION {d:02d}" for d in winners) if winners else "No MVP awarded"
        panel.add_item(discord.ui.Section(
            f"### RAID  `#{raid_number:04d}`  \u00b7  RAIDERS COUNTED `{len(raiders):02d}`\n**MVP:** {mvp}",
            accessory=discord.ui.Button(label="MVP Log", url=source_url)))
        panel.add_item(discord.ui.Separator())
        for d in order:
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
        self.add_item(panel)


def build_update_view(guild_id: int, row, images: bool = True) -> RaidUpdateView:
    """Rebuild the main MVP post from a saved raid (used to re-arm the Edit button after a restart)."""
    raiders = json.loads(row["raiders_json"])
    tallies, winners = tally(raiders)
    return RaidUpdateView(
        int(row["raid_number"]), tallies, winners, len(raiders),
        channel_url(guild_id, row["channel_id"], row["message_id"]),
        ref_url(guild_id, row["log_ref"], LOG_ID),
        channel_url(guild_id, LEADERBOARD_ID), images)


# ----------------------------------------------------------------------------
# LEADERBOARD + MVP PROCESSING
# ----------------------------------------------------------------------------
async def clear_own_messages(channel_id: int) -> None:
    """Delete the bot's own recent messages in a channel so the new leaderboard is the first thing there."""
    channel = await get_channel(channel_id)
    if channel is None or not hasattr(channel, "history") or bot.user is None:
        return
    try:
        async for m in channel.history(limit=100):
            if m.author.id == bot.user.id:
                try:
                    await m.delete()
                except discord.HTTPException:
                    pass
    except discord.HTTPException as exc:
        print(f"Could not clear old messages in {channel_id}: {exc!r}")


async def refresh_leaderboard(guild: discord.Guild, force_new: bool = False):
    """Edit the permanent leaderboard in place (or post it if it does not exist)."""
    key = f"board:{guild.id}"
    saved = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    ref = saved["value"] if saved else None
    if force_new:
        await delete_ref(ref)
        legacy = db.execute("SELECT value FROM settings WHERE key=?", (f"leaderboard2:{guild.id}",)).fetchone()
        if legacy:
            await delete_ref(legacy["value"])
            db.execute("DELETE FROM settings WHERE key=?", (f"leaderboard2:{guild.id}",))
            db.commit()
        await clear_own_messages(LEADERBOARD_ID)
        ref = None
    totals, _ = division_totals(guild.id)
    view = LeaderboardView(totals, channel_url(guild.id, LOG_ID))
    new_ref = await send_or_edit(LEADERBOARD_ID, ref, lambda images: view, with_files=False)
    if new_ref and new_ref != ref:
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, new_ref))
        db.commit()
    return new_ref


async def publish_raid(guild: discord.Guild, row_ids: tuple[int, int], raid_number: int, raiders: list[dict],
                       skipped: list, update_ref: str | None, log_ref: str | None):
    """Post (or edit) the division log first, then the main DIVISION MVP'S post. Returns the new refs."""
    channel_id, message_id = row_ids
    tallies, winners = tally(raiders)
    info_url = channel_url(guild.id, channel_id, message_id)
    try:
        log_ref = await send_or_edit(LOG_ID, log_ref, lambda images: RaidLogView(
            raid_number, raiders, skipped, winners, info_url, images))
    except discord.HTTPException as exc:
        print(f"Could not publish the division log for raid {raid_number}: {exc!r}")
    logs_url = ref_url(guild.id, log_ref, LOG_ID)
    board_url = channel_url(guild.id, LEADERBOARD_ID)      # channel link: stays valid when the board is reposted
    try:
        update_ref = await send_or_edit(RAID_UPDATE_ID, update_ref, lambda images: RaidUpdateView(
            raid_number, tallies, winners, len(raiders), info_url, logs_url, board_url, images))
    except discord.HTTPException as exc:
        print(f"Could not publish the MVP update for raid {raid_number}: {exc!r}")
    return update_ref, log_ref


def raider_signature(raiders: list[dict]):
    return sorted((int(r["user_id"]), int(r["division"])) for r in raiders)


async def process_mvp(message: discord.Message, *, force: bool = False, publish: bool = True) -> str:
    """Record an MVP log. Returns "new", "updated", "unchanged" or "ignored".

    Also used when an MVP log is edited: the saved record is recounted and the posts are edited.
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
            update_ref, log_ref = row["update_ref"], row["log_ref"]
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
            update_ref = log_ref = None
            ids = (message.channel.id, message.id)
            status = "new"
        db.commit()
        if skipped:
            print(f"Raid #{raid_number}: {len(skipped)} pinged member(s) not counted: " +
                  ", ".join(f"{who} ({why})" for who, why in skipped))

        if publish:
            update_ref, log_ref = await publish_raid(guild, ids, raid_number, raiders, skipped, update_ref, log_ref)
            db.execute("UPDATE raid_results SET update_ref=?, log_ref=? WHERE message_id=?", (update_ref, log_ref, ids[1]))
            db.commit()
            try:
                await refresh_leaderboard(guild)
            except discord.HTTPException as exc:
                print(f"Could not refresh the leaderboard: {exc!r}")
        return status


async def apply_edit(guild: discord.Guild, raid_number: int, to_add: list[int], to_remove: list[int]) -> str:
    """Owner edit from the main post: add or remove raiders, then repost/recount everything."""
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
            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except discord.HTTPException:
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
        skipped = [s for s in skipped if int(re.sub(r"\D", "", s[0]) or 0) not in {int(re.sub(r"\D", "", a)) for a in added}]

        tallies, winners = tally(raiders)
        db.execute("UPDATE raid_results SET raiders_json=?, tallies_json=?, winners_json=?, skipped_json=? WHERE message_id=?",
                   (json.dumps(raiders), json.dumps({str(k): v for k, v in tallies.items()}),
                    json.dumps(winners), json.dumps(skipped), row["message_id"]))
        db.commit()
        ids = (row["channel_id"], row["message_id"])
        update_ref, log_ref = await publish_raid(guild, ids, raid_number, raiders, skipped,
                                                 row["update_ref"], row["log_ref"])
        db.execute("UPDATE raid_results SET update_ref=?, log_ref=? WHERE message_id=?", (update_ref, log_ref, ids[1]))
        db.commit()
        try:
            await refresh_leaderboard(guild)
        except discord.HTTPException as exc:
            print(f"Could not refresh the leaderboard: {exc!r}")

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


def register_saved_views(guild: discord.Guild) -> None:
    """Re-arm the Edit button on recent MVP posts after a restart."""
    rows = db.execute("SELECT * FROM raid_results WHERE guild_id=? AND update_ref IS NOT NULL "
                      "ORDER BY raid_number DESC LIMIT 50", (guild.id,)).fetchall()
    for row in rows:
        try:
            _, mid = row["update_ref"].split(":", 1)
            bot.add_view(build_update_view(guild.id, row, images=False), message_id=int(mid))
        except Exception as exc:
            print(f"Could not re-arm the Edit button for raid {row['raid_number']}: {exc!r}")


# ----------------------------------------------------------------------------
# EVENTS
# ----------------------------------------------------------------------------
@bot.event
async def on_ready():
    global STARTED
    print(f"{BOT_NAME} online as {bot.user}")
    if STARTED:
        return
    STARTED = True
    guilds = [bot.get_guild(GUILD_ID)] if GUILD_ID else list(bot.guilds)
    for guild in guilds:
        if guild is None:
            continue
        try:
            if not guild.chunked:
                await guild.chunk()          # load every member so role lookups are exact
        except Exception as exc:
            print(f"Could not load the member list for {guild.id}: {exc!r}")
        register_saved_views(guild)
        try:
            await refresh_leaderboard(guild, force_new=True)     # every start: remove the old board, post a new one
        except discord.HTTPException as exc:
            print(f"Could not refresh the leaderboard: {exc!r}")
        target = discord.Object(id=guild.id)
        bot.tree.copy_global_to(guild=target)
        await bot.tree.sync(guild=target)


@bot.event
async def on_message(message: discord.Message):
    if message.guild and source_message(message):
        await process_mvp(message)


@bot.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    """The MVP bot may edit its log after posting it; recount when that happens."""
    if payload.channel_id not in SOURCE_IDS:
        return
    message = await try_get_message(payload.channel_id, payload.message_id)
    if message is not None and message.guild and source_message(message):
        await process_mvp(message)


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
# SLASH COMMANDS: MVP TOOLS
# ----------------------------------------------------------------------------
@bot.tree.command(name="help", description="Show every command")
async def help_command(interaction: discord.Interaction):
    embed = make_embed(f"{BOT_NAME} commands")
    embed.add_field(name="Division", inline=False, value=(
        "`/leaderboard` - show the current division leaderboard\n"
        "`/raid` - details of one raid and its raiders\n"
        "`/history` - the last 10 raids\n"
        "`/division` - a member's division and raid stats\n"
        "`/divisions` - how many members each division has\n"
        "`/botinfo` - information about the bot"))
    embed.add_field(name="Management", inline=False, value=(
        "`/test` - preview the latest MVP log publicly; nothing is saved\n"
        "`/refresh` - update the permanent leaderboard (`repost` posts a fresh one)\n"
        "`/recount` - re-read MVP logs and fix the counts (one raid, or all)\n"
        "`/addraid` - record an MVP log the bot missed\n"
        "`/deleteraid` - remove a raid and its posts\n"
        "`/preview` - preview scoring from typed counts\n"
        "`/status` - configuration and totals"))
    embed.add_field(name="Owner only", inline=False, value=(
        "`/trust add` `/trust remove` `/trust list` - choose who can use these commands\n"
        "**Edit** button on a DIVISION MVP'S post - add or remove raiders"))
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="status", description="Show configuration and recorded raid count")
@app_commands.guild_only()
async def status_command(interaction: discord.Interaction):
    totals, raids = division_totals(interaction.guild_id)
    embed = make_embed(f"{BOT_NAME} status")
    embed.add_field(name="Saved raids", value=str(raids), inline=True)
    embed.add_field(name="Next raid number", value=f"#{next_raid_number(interaction.guild_id):04d}", inline=True)
    embed.add_field(name="Badges", value=f"{len(BADGES)}/5 ready", inline=True)
    embed.add_field(name="MVP channel", value=", ".join(f"<#{c}>" for c in sorted(SOURCE_IDS)), inline=True)
    embed.add_field(name="MVP bot", value=f"<@{MVP_BOT_ID}>", inline=True)
    embed.add_field(name="Raid updates", value=f"<#{RAID_UPDATE_ID}>", inline=True)
    embed.add_field(name="Leaderboard", value=f"<#{LEADERBOARD_ID}>", inline=True)
    embed.add_field(name="Division logs", value=f"<#{LOG_ID}>", inline=True)
    embed.add_field(name="Owner", value=f"<@{OWNER_ID}>", inline=True)
    embed.add_field(name="Division roles", value=" \u00b7 ".join(f"D{d}: <@&{r}>" for d, r in DIVISION_ROLES.items()), inline=False)
    embed.add_field(name="MVP awards", value=" \u00b7 ".join(f"D{d}: **{n}**" for d, n in totals.items()), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=NO_PING)


@bot.tree.command(name="leaderboard", description="Show the current division leaderboard")
@app_commands.guild_only()
async def leaderboard_command(interaction: discord.Interaction):
    totals, _ = division_totals(interaction.guild_id)
    await interaction.response.send_message(view=LeaderboardView(totals, channel_url(interaction.guild_id, LOG_ID)))


@bot.tree.command(name="refresh", description="Update the permanent leaderboard")
@app_commands.describe(repost="Delete the old leaderboard message and post a fresh one")
@app_commands.guild_only()
async def refresh_command(interaction: discord.Interaction, repost: bool = False):
    await interaction.response.defer(ephemeral=True, thinking=True)
    ref = await refresh_leaderboard(interaction.guild, force_new=repost)
    if ref:
        cid, mid = ref.split(":", 1)
        text = f"[Open leaderboard]({channel_url(interaction.guild_id, int(cid), int(mid))})"
    else:
        text = "The leaderboard channel could not be reached."
    await interaction.followup.send(embed=make_embed("Leaderboard refreshed" if ref else "Refresh failed", text,
                                                     0x57F287 if ref else 0xED4245), ephemeral=True)


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


@bot.tree.command(name="test", description="Preview the latest MVP log: update, leaderboard and division log (nothing saved)")
@app_commands.guild_only()
async def test_command(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)          # public: everyone can see the result
    message = await latest_mvp_post(interaction.guild)
    if message is None:
        report = await diagnose_channel(interaction.guild)
        await interaction.followup.send(embed=make_embed(
            "No MVP log found",
            f"I couldn't find a recent MVP log with a Raid #number from <@{MVP_BOT_ID}> in <#{MVP_CHANNEL_ID}>.\n\n"
            f"**What the bot sees in that channel**\n{report}", 0xED4245), allowed_mentions=NO_PING)
        return

    raid_number = int(RAID_RE.search(post_text(message)).group(1))
    raiders, skipped = await collect_raiders(message)
    tallies, winners = tally(raiders)
    totals, _ = division_totals(interaction.guild_id)
    saved = db.execute("SELECT 1 FROM raid_results WHERE message_id=?", (message.id,)).fetchone()
    if saved:
        state = "Already recorded: the leaderboard below already includes this raid."
    else:
        for division in winners:
            totals[division] += 1
        state = "Not recorded yet: the leaderboard below is a preview with this raid added. Nothing is saved or counted."
    pinged = len(raiders) + len(skipped)
    lines = [f"Source: [open the MVP log]({message.jump_url})",
             f"Pinged: **{pinged}**  \u00b7  counted: **{len(raiders)}**  \u00b7  not counted: **{len(skipped)}**", state]
    if skipped:
        lines.append("Not counted: " + fit_list([f"{who} ({why})" for who, why in skipped], 700))
    await interaction.followup.send(embed=make_embed(f"Test preview  \u00b7  Raid #{raid_number:04d}", "\n".join(lines), 0xFEE75C),
                                    allowed_mentions=NO_PING)
    board_url = channel_url(interaction.guild_id, LEADERBOARD_ID)
    logs_url = channel_url(interaction.guild_id, LOG_ID)
    shown = await followup_view(interaction, lambda images: RaidUpdateView(
        raid_number, tallies, winners, len(raiders), message.jump_url, logs_url, board_url, images, test=True))
    await interaction.followup.send(
        view=LeaderboardView(totals, logs_url, test=True), allowed_mentions=NO_PING)
    shown_log = await followup_view(interaction, lambda images: RaidLogView(
        raid_number, raiders, skipped, winners, message.jump_url, images, test=True))
    if not (shown and shown_log):
        await interaction.followup.send(embed=make_embed(
            "Badges not shown",
            "Discord rejected the badge image references, so these were sent without thumbnails. "
            "The console shows Discord's exact error.", 0xED4245), allowed_mentions=NO_PING)


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
            "Recount done", f"Raid **#{number:04d}**: {status}. The posts and the leaderboard were updated.", 0x57F287), ephemeral=True)
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
        "The leaderboard was updated. Use `/recount number:` to also refresh a raid's posts.", 0x57F287), ephemeral=True)


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
    text = {"new": "Recorded, posted and the leaderboard updated.", "updated": "Already saved; recounted and updated.",
            "unchanged": "Already saved; nothing changed.", "ignored": "That message is not a valid MVP log (check the bot ID and the Raid #number)."}[status]
    await interaction.followup.send(embed=make_embed("Add raid", text, 0x57F287 if status != "ignored" else 0xED4245), ephemeral=True)


@bot.tree.command(name="deleteraid", description="Remove a recorded raid and its posts")
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


if not TOKEN or TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
    raise RuntimeError("Set your Discord bot token in .env before starting the bot.")
bot.run(TOKEN)