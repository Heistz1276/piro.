# SLASH COMMANDS: PER-DIVISION HELP LEADERBOARDS (/leaderboarddivision1 ... /leaderboarddivision5)
# ----------------------------------------------------------------------------
# READ-ONLY: these commands only read the existing `live_tickets` (helpers) and `raid_results` (MVP)
# tables. They never write, reset or alter any saved data, so every count simply continues.
#
# Access: ONLY the three user IDs below can use them (enforced in GuardedTree.interaction_check in
# main.py AND again inside each command). Having Manage Server or being "trusted" is not enough.
# Note: Discord slash-command names must be lowercase, so /LeaderboardDivision1 is /leaderboarddivision1.
DIV_LB_ALLOWED = {1455426573741330495, 1388408557560401991, 1104824804315508866}
DIV_LB_COMMANDS = {f"leaderboarddivision{d}" for d in range(1, 6)}
DIV_LB_PAGE_SIZE = 10
DIV_LB_MEDALS = {1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949"}


def div_lb_collect(guild: discord.Guild, d: int) -> dict:
    """Collect help counts for ONE division from the saved ticket helper lists (nothing is written)."""
    role_id = DIVISION_ROLES.get(d)
    counts: dict[int, int] = {}
    names: dict[int, str] = {}
    tickets = 0
    for row in db.execute("SELECT helpers_json FROM live_tickets WHERE guild_id=?", (guild.id,)):
        try:
            helpers = json.loads(row["helpers_json"] or "[]")
        except (TypeError, ValueError):
            continue
        seen: set[int] = set()
        counted_here = False
        for helper in helpers:
            try:
                user_id = int(helper["user_id"])
            except (KeyError, TypeError, ValueError):
                continue
            if user_id in seen:
                continue
            seen.add(user_id)
            member = guild.get_member(user_id)
            if member is not None:
                if member.bot:
                    continue
                in_division = (any(r.id == role_id for r in member.roles) if role_id
                               else division_of(member)[0] == d)
                name = member.name
            else:  # left the server: fall back to the division saved with the helper
                in_division = helper.get("division") == d
                name = str(helper.get("display_name") or user_id)
            if not in_division:
                continue
            counts[user_id] = counts.get(user_id, 0) + 1
            names[user_id] = name
            counted_here = True
        tickets += 1 if counted_here else 0

    ranking = sorted(counts.items(), key=lambda item: (-item[1], item[0]))

    raids_joined = mvp_raids = 0
    for row in db.execute("SELECT raiders_json, winners_json FROM raid_results WHERE guild_id=? AND counted=1",
                          (guild.id,)):
        try:
            winners = {int(x) for x in json.loads(row["winners_json"])}
            mine = sum(1 for r in json.loads(row["raiders_json"]) if int(r["division"]) == d)
        except (TypeError, ValueError, KeyError):
            continue
        if mine:
            raids_joined += 1
        if d in winners:
            mvp_raids += 1

    members = 0
    for member in guild.members:
        if member.bot:
            continue
        if role_id:
            members += 1 if any(r.id == role_id for r in member.roles) else 0
        else:
            members += 1 if division_of(member)[0] == d else 0

    totals, _ = division_totals(guild.id)
    return {"ranking": ranking, "names": names, "tickets": tickets, "total_helps": sum(counts.values()),
            "raids_joined": raids_joined, "mvp_raids": mvp_raids, "mvp_awards": totals.get(d, 0),
            "members": members}


class DivisionLeaderboardView(discord.ui.LayoutView):
    """Components V2 card styled like the Server Total Leaderboard: stats, division MVPs, 10 per page."""

    def __init__(self, guild: discord.Guild, d: int, page: int, viewer_id: int):
        super().__init__(timeout=900)
        self.guild, self.d, self.viewer_id = guild, d, viewer_id
        data = div_lb_collect(guild, d)
        ranking, names = data["ranking"], data["names"]
        pages = max(1, -(-len(ranking) // DIV_LB_PAGE_SIZE))
        self.page = max(0, min(page, pages - 1))
        self.pages = pages
        start = self.page * DIV_LB_PAGE_SIZE
        chunk = ranking[start:start + DIV_LB_PAGE_SIZE]

        emoji = DIVISION_EMOJI.get(d, "")
        card = discord.ui.Container(accent_colour=DIVISION_COLORS.get(d, BRAND_COLOR))
        card.add_item(discord.ui.TextDisplay(
            f"## {emoji} Division {d} Leaderboard\n"
            f"Leaderboard for the number of times Division {d} members helped.\n"
            f"**Server:** {guild.name}"))
        card.add_item(discord.ui.Separator())

        avg = (data["total_helps"] / len(ranking)) if ranking else 0
        card.add_item(discord.ui.TextDisplay(
            f"### \U0001F4CA Division {d} Stats\n"
            f"**Division:** <@&{DIVISION_ROLES[d]}>\n"
            f"**Members:** `{data['members']}`  \u00b7  **Active helpers:** `{len(ranking)}`\n"
            f"**Total helps:** `{data['total_helps']}`  \u00b7  **Backups helped in:** `{data['tickets']}`\n"
            f"**MVP awards:** `{data['mvp_awards']}`  \u00b7  **Raids joined:** `{data['raids_joined']}`  "
            f"\u00b7  **MVP raids:** `{data['mvp_raids']}`\n"
            f"**Average helps per helper:** `{avg:.1f}`"))
        card.add_item(discord.ui.Separator())

        if ranking:
            mvp_lines = []
            for place, (user_id, helps) in enumerate(ranking[:3], start=1):
                mvp_lines.append(f"{DIV_LB_MEDALS[place]} <@{user_id}>  \u00b7  Helps: `{helps}`")
            mvp_text = "\n".join(mvp_lines)
        else:
            mvp_text = "No helps recorded for this division yet."
        card.add_item(discord.ui.TextDisplay(f"### \U0001F31F Division {d} MVPs (most backups joined)\n{mvp_text}"))
        card.add_item(discord.ui.Separator())

        lines = []
        for offset, (user_id, helps) in enumerate(chunk):
            rank = start + offset + 1
            marker = DIV_LB_MEDALS.get(rank, f"**{rank}.**")
            lines.append(f"{marker} `@{names.get(user_id, user_id)}`\n-# Helps: `{helps}`")
        body = "\n".join(lines) if lines else "Nobody from this division has helped yet."
        card.add_item(discord.ui.TextDisplay(f"### \U0001F3C6 Rankings\n{body}"))
        card.add_item(discord.ui.Separator())

        stamp = int(datetime.now(timezone.utc).timestamp())
        card.add_item(discord.ui.TextDisplay(
            f"-# Last updated: <t:{stamp}:R>  \u2022  Total members: {data['members']}"))

        row = discord.ui.ActionRow()
        for label, target, off in (("\u23EE", 0, self.page == 0), ("\u25C0", self.page - 1, self.page == 0)):
            row.add_item(self._nav(label, target, off))
        row.add_item(discord.ui.Button(label=f"{self.page + 1}/{pages}", style=discord.ButtonStyle.secondary,
                                       disabled=True))
        last = self.page >= pages - 1
        for label, target, off in (("\u25B6", self.page + 1, last), ("\u23ED", pages - 1, last)):
            row.add_item(self._nav(label, target, off))
        card.add_item(row)
        self.add_item(card)

    def _nav(self, label: str, target: int, disabled: bool) -> discord.ui.Button:
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, disabled=disabled)

        async def callback(interaction: discord.Interaction):
            if interaction.user.id not in DIV_LB_ALLOWED:
                await interaction.response.send_message("You can't use this.", ephemeral=True)
                return
            await interaction.response.edit_message(
                view=DivisionLeaderboardView(self.guild, self.d, target, self.viewer_id))

        button.callback = callback
        return button


def make_division_lb_command(d: int) -> None:
    @bot.tree.command(name=f"leaderboarddivision{d}",
                      description=f"Division {d} help leaderboard, stats and MVPs (restricted)")
    @app_commands.guild_only()
    async def division_lb_command(interaction: discord.Interaction):
        if interaction.user.id not in DIV_LB_ALLOWED:
            await interaction.response.send_message("You are not allowed to use this command.", ephemeral=True)
            return
        await interaction.response.send_message(
            view=DivisionLeaderboardView(interaction.guild, d, 0, interaction.user.id),
            allowed_mentions=NO_PING)


for _d in range(1, 6):
    make_division_lb_command(_d)


# ----------------------------------------------------------------------------