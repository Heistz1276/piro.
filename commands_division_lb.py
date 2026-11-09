# SLASH COMMANDS: PER-DIVISION LEADERBOARDS (/leaderboarddivision1 ... /leaderboarddivision5)
# ----------------------------------------------------------------------------
# READ-ONLY: these commands only read the saved `live_tickets` (backup helps) and `raid_results` (raids / MVPs)
# tables. They never write, reset or change any data, so every count simply continues.
#
# Who can use them:
#   /leaderboarddivisionN  ->  members who hold the Division N role (role IDs below) + the 3 admin IDs.
# (Discord slash-command names must be lowercase, so /LeaderboardDivision1 is /leaderboarddivision1.)
DIV_LB_ADMINS = {1455426573741330495, 1388408557560401991, 1104824804315508866}
DIV_LB_ROLE_IDS = {
    1: 1517694951650103427,
    2: 1517694955835883682,
    3: 1517694959291994132,
    4: 1517694962119213056,
    5: 1517694969538805831,
}
DIV_LB_COMMANDS = {f"leaderboarddivision{d}" for d in range(1, 6)}
DIV_LB_PAGE_SIZE = 10
DIV_LB_MEDALS = {1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949"}


def div_lb_can_use(user, d: int) -> bool:
    """Admins can open any division; everyone else only their own division."""
    if user.id in DIV_LB_ADMINS:
        return True
    roles = getattr(user, "roles", None) or []
    return any(r.id == DIV_LB_ROLE_IDS[d] for r in roles)


def div_lb_is_member(member: discord.Member, d: int) -> bool:
    return any(r.id == DIV_LB_ROLE_IDS[d] for r in member.roles)


def div_lb_collect(guild: discord.Guild, d: int) -> dict:
    """Everything shown on the card for ONE division (read-only)."""
    helps: dict[int, int] = {}
    raids: dict[int, int] = {}
    names: dict[int, str] = {}
    tickets = 0

    # --- backup helps (saved ticket helper lists) -----------------------------------------
    for row in db.execute("SELECT helpers_json FROM live_tickets WHERE guild_id=?", (guild.id,)):
        try:
            helpers = json.loads(row["helpers_json"] or "[]")
        except (TypeError, ValueError):
            continue
        seen: set[int] = set()
        counted = False
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
                if member.bot or not div_lb_is_member(member, d):
                    continue
                names[user_id] = member.name
            else:  # left the server: use the division saved with the helper
                try:
                    if int(helper.get("division") or 0) != d:
                        continue
                except (TypeError, ValueError):
                    continue
                names.setdefault(user_id, str(helper.get("display_name") or user_id))
            helps[user_id] = helps.get(user_id, 0) + 1
            counted = True
        tickets += 1 if counted else 0

    # --- raids / MVPs (same source and filter as the main division leaderboard) ----------
    clause, channel_params = source_db_filter()
    totals, total_raids = division_totals(guild.id)
    rows = db.execute(f"SELECT * FROM raid_results WHERE guild_id=? AND counted=1 AND {clause} "
                      "ORDER BY raid_number DESC", (guild.id, *channel_params)).fetchall()
    mvp_rows = []
    raider_slots = 0
    for row in rows:
        try:
            winners = {int(x) for x in json.loads(row["winners_json"])}
            raiders = json.loads(row["raiders_json"])
        except (TypeError, ValueError):
            continue
        mine = 0
        for raider in raiders:
            try:
                if int(raider["division"]) != d:
                    continue
                user_id = int(raider["user_id"])
            except (KeyError, TypeError, ValueError):
                continue
            mine += 1
            raids[user_id] = raids.get(user_id, 0) + 1
            if user_id not in names:
                member = guild.get_member(user_id)
                names[user_id] = member.name if member else str(raider.get("display_name") or user_id)
        raider_slots += mine
        if d in winners:
            mvp_rows.append((row, mine))

    # --- members of the division ----------------------------------------------------------
    members = [m for m in guild.members if not m.bot and div_lb_is_member(m, d)]
    ids = set(helps) | set(raids)
    ranking = sorted(ids, key=lambda u: (-helps.get(u, 0), -raids.get(u, 0), u))

    order = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    place = 1 + next(i for i, (div, _) in enumerate(order) if div == d)

    return {"ranking": ranking, "helps": helps, "raids": raids, "names": names, "tickets": tickets,
            "members": len(members), "member_ids": {m.id for m in members},
            "mvp_count": totals.get(d, 0), "division_place": place, "leaders": leaders_of(totals),
            "total_raids": total_raids, "raider_slots": raider_slots, "unique_raiders": len(raids),
            "mvp_rows": mvp_rows}


def div_lb_percent(part: int, whole: int) -> str:
    return f"{(part * 100.0 / whole):.0f}%" if whole else "0%"


class DivisionLeaderboardView(discord.ui.LayoutView):
    """Components V2 card styled like the Server Total Leaderboard, for ONE division."""

    def __init__(self, guild: discord.Guild, d: int, page: int = 0):
        super().__init__(timeout=900)
        self.guild, self.d = guild, d
        data = div_lb_collect(guild, d)
        self.data = data
        ranking, helps, raids, names = data["ranking"], data["helps"], data["raids"], data["names"]
        pages = max(1, -(-len(ranking) // DIV_LB_PAGE_SIZE))
        self.page = max(0, min(page, pages - 1))
        self.pages = pages
        start = self.page * DIV_LB_PAGE_SIZE
        chunk = ranking[start:start + DIV_LB_PAGE_SIZE]

        badge = DIV_EMOJI.get(d, DIVISION_EMOJI.get(d, ""))
        crown = CROWN_EMOJI or "\u265B"
        card = discord.ui.Container(accent_colour=DIVISION_COLORS.get(d, BRAND_COLOR))

        # header ---------------------------------------------------------------------------
        card.add_item(discord.ui.TextDisplay(
            f"## {badge} Division {d} Leaderboard\n"
            f"Leaderboard for Division {d}: backups helped and raids joined.\n"
            f"**Server:** {guild.name}"))
        card.add_item(discord.ui.Separator())

        # division stats ---------------------------------------------------------------------
        helps_total = sum(helps.values())
        win_rate = div_lb_percent(data["mvp_count"], data["total_raids"])
        lead = lead_text(data["leaders"])
        active = len(data["member_ids"] & set(ranking))
        inactive = max(0, data["members"] - active)
        card.add_item(discord.ui.TextDisplay(
            f"### \U0001F4CA Division {d} Stats\n"
            f"**Division:** <@&{DIV_LB_ROLE_IDS[d]}>  \u00b7  **Standing:** #{data['division_place']} of 5\n"
            f"{crown} **MVP count:** `{data['mvp_count']}`  \u00b7  **MVP rate:** `{win_rate}` of `{data['total_raids']}` raids\n"
            f"**Members:** `{data['members']}`  \u00b7  **Active:** `{active}`  \u00b7  **Inactive:** `{inactive}`\n"
            f"**Backup helps:** `{helps_total}` in `{data['tickets']}` tickets\n"
            f"**Raid entries:** `{data['raider_slots']}`  \u00b7  **Different raiders:** `{data['unique_raiders']}`\n"
            f"-# {lead}"))
        card.add_item(discord.ui.Separator())

        # division MVPs: who joined backups / raids the most ----------------------------------
        top_help = sorted(helps.items(), key=lambda item: (-item[1], item[0]))[:3]
        top_raid = sorted(raids.items(), key=lambda item: (-item[1], item[0]))[:3]
        help_lines = "\n".join(f"{DIV_LB_MEDALS[i]} <@{u}>  \u00b7  `{n}` helps"
                               for i, (u, n) in enumerate(top_help, start=1)) or "No backups helped yet."
        raid_lines = "\n".join(f"{DIV_LB_MEDALS[i]} <@{u}>  \u00b7  `{n}` raids"
                               for i, (u, n) in enumerate(top_raid, start=1)) or "No raids joined yet."
        card.add_item(discord.ui.TextDisplay(
            f"### \U0001F31F Division {d} MVPs\n"
            f"**Most backups helped**\n{help_lines}\n"
            f"**Most raids joined**\n{raid_lines}"))
        card.add_item(discord.ui.Separator())

        # latest MVP raids of this division ----------------------------------------------------
        recent = []
        for row, mine in data["mvp_rows"][:3]:
            try:
                recent.append(f"`#{int(row['raid_number']):04d}`  \u00b7  **{mine}** raiders  \u00b7  "
                              f"[MVP post]({channel_url(guild.id, row['channel_id'], row['message_id'])})")
            except (KeyError, TypeError, ValueError):
                continue
        card.add_item(discord.ui.TextDisplay(
            f"### \U0001F3C6 Latest MVP raids\n" + ("\n".join(recent) if recent else "No MVP raid yet.")))
        card.add_item(discord.ui.Separator())

        # rankings (10 per page) ---------------------------------------------------------------
        lines = []
        for offset, user_id in enumerate(chunk):
            rank = start + offset + 1
            marker = DIV_LB_MEDALS.get(rank, f"**{rank}.**")
            lines.append(f"{marker} `@{names.get(user_id, user_id)}`\n"
                         f"-# Helps: `{helps.get(user_id, 0)}`  \u00b7  Raids: `{raids.get(user_id, 0)}`")
        body = "\n".join(lines) if lines else "Nobody from this division has helped or raided yet."
        card.add_item(discord.ui.TextDisplay(f"### \U0001F4CB Rankings\n{body}"))
        card.add_item(discord.ui.Separator())

        stamp = int(datetime.now(timezone.utc).timestamp())
        card.add_item(discord.ui.TextDisplay(
            f"-# Last updated: <t:{stamp}:R>  \u2022  Total members: {data['members']}"))

        # buttons -------------------------------------------------------------------------------
        top_row = discord.ui.ActionRow()
        top_row.add_item(self._my_rank_button())
        top_row.add_item(self._nav("\U0001F504 Refresh", self.page, False))
        card.add_item(top_row)

        nav = discord.ui.ActionRow()
        first = self.page == 0
        last = self.page >= pages - 1
        nav.add_item(self._nav("\u23EE", 0, first))
        nav.add_item(self._nav("\u25C0", self.page - 1, first))
        nav.add_item(discord.ui.Button(label=f"{self.page + 1}/{pages}", style=discord.ButtonStyle.secondary,
                                       disabled=True))
        nav.add_item(self._nav("\u25B6", self.page + 1, last))
        nav.add_item(self._nav("\u23ED", pages - 1, last))
        card.add_item(nav)
        self.add_item(card)

    def _nav(self, label: str, target: int, disabled: bool) -> discord.ui.Button:
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, disabled=disabled)

        async def callback(interaction: discord.Interaction):
            if not div_lb_can_use(interaction.user, self.d):
                await interaction.response.send_message("This leaderboard is only for Division "
                                                        f"{self.d} members.", ephemeral=True)
                return
            await interaction.response.edit_message(view=DivisionLeaderboardView(self.guild, self.d, target))

        button.callback = callback
        return button

    def _my_rank_button(self) -> discord.ui.Button:
        button = discord.ui.Button(label="My Rank", style=discord.ButtonStyle.secondary)

        async def callback(interaction: discord.Interaction):
            if not div_lb_can_use(interaction.user, self.d):
                await interaction.response.send_message("This leaderboard is only for Division "
                                                        f"{self.d} members.", ephemeral=True)
                return
            data = div_lb_collect(self.guild, self.d)
            uid = interaction.user.id
            if uid in data["ranking"]:
                rank = data["ranking"].index(uid) + 1
                top = div_lb_percent(rank, max(1, data["members"]))
                text = (f"**User:** <@{uid}>\n**Rank:** #{rank} (Top {top})\n"
                        f"**Helps:** {data['helps'].get(uid, 0)}\n**Raids:** {data['raids'].get(uid, 0)}")
            else:
                text = f"**User:** <@{uid}>\nNo backups helped or raids joined in Division {self.d} yet."
            await interaction.response.send_message(
                embed=make_embed(f"\U0001F4CA Your Division {self.d} Stats", text,
                                 DIVISION_COLORS.get(self.d, BRAND_COLOR)),
                ephemeral=True, allowed_mentions=NO_PING)

        button.callback = callback
        return button


def make_division_lb_command(d: int) -> None:
    @bot.tree.command(name=f"leaderboarddivision{d}",
                      description=f"Division {d} leaderboard: stats, MVPs and member rankings")
    @app_commands.guild_only()
    async def division_lb_command(interaction: discord.Interaction):
        if not div_lb_can_use(interaction.user, d):
            await interaction.response.send_message(
                f"Only Division {d} members can use this command.", ephemeral=True)
            return
        await interaction.response.defer()  # building the card can take a moment: never time out
        try:
            view = DivisionLeaderboardView(interaction.guild, d)
        except Exception as exc:
            print(f"/leaderboarddivision{d} failed: {exc!r}")
            await interaction.followup.send("Could not build the leaderboard right now, please try again.",
                                            ephemeral=True)
            return
        await interaction.followup.send(view=view, allowed_mentions=NO_PING)


for _d in range(1, 6):
    make_division_lb_command(_d)


# ----------------------------------------------------------------------------
