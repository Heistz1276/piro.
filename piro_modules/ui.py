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
        await interaction.response.send_message(
            embed=make_embed(f"Edit raid {self.raid_number:04d}", "Choose the members who participated in this backup."),
            view=RaidParticipantsView(self.raid_number), ephemeral=True)


class ParticipantSelect(discord.ui.UserSelect):
    def __init__(self, raid_number: int, *, remove: bool):
        label = "Remove participants" if remove else "Add participants"
        super().__init__(placeholder=label, min_values=1, max_values=25)
        self.raid_number = raid_number
        self.remove = remove

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID or interaction.guild is None:
            await interaction.response.send_message("Only the owner can edit participants.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        member_ids = [member.id for member in self.values]
        result = await apply_edit(interaction.guild, self.raid_number,
                                  [] if self.remove else member_ids,
                                  member_ids if self.remove else [])
        await interaction.followup.send(embed=make_embed("Raid participants updated", result, 0x57F287),
                                        ephemeral=True, allowed_mentions=NO_PING)


class RaidParticipantsView(discord.ui.View):
    def __init__(self, raid_number: int):
        super().__init__(timeout=300)
        self.add_item(ParticipantSelect(raid_number, remove=False))
        self.add_item(ParticipantSelect(raid_number, remove=True))


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
        if time.time() - LB_LAST_PRESS < 5:
            await interaction.response.send_message("Please wait a few seconds before refreshing again.", ephemeral=True)
            return
        LB_LAST_PRESS = time.time()
        await interaction.response.defer(ephemeral=True, thinking=True)
        refreshed = await refresh_leaderboard(interaction.guild)
        if refreshed is None:
            await interaction.followup.send("I couldn't refresh the fixed leaderboard. Check my channel permissions.",
                                            ephemeral=True)
        else:
            await interaction.followup.send("Leaderboard refreshed.", ephemeral=True)


class DivisionViewButton(discord.ui.Button):
    """Compact division button: lists every raid (backup) it got the MVP in."""

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


class MyRankButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="My Rank", style=discord.ButtonStyle.secondary, custom_id="urdiv:my_rank")

    async def callback(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Use this in the server leaderboard.", ephemeral=True)
            return
        embed = build_my_rank_embed(interaction.guild, interaction.user)
        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=NO_PING)


# ----------------------------------------------------------------------------
# LAYOUTS (Components V2)
# ----------------------------------------------------------------------------
def accent_for(winners: list[int]) -> int:
    return DIVISION_COLORS[winners[0]] if len(winners) == 1 else BRAND_COLOR


class RaidUpdateView(discord.ui.LayoutView):
    """The per-raid result card sent to the configured results channel."""

    def __init__(self, raid_number: int, tallies: dict[int, int], winners: list[int], raider_count: int,
                 source_url: str, logs_url: str, test: bool = False):
        super().__init__(timeout=None)
        order = sorted(range(1, 6), key=lambda d: (-tallies[d], d))
        lead = leaders_of({d: tallies[d] for d in range(1, 6)})
        panel = discord.ui.Container(accent_colour=accent_for(winners))
        panel.add_item(discord.ui.TextDisplay(
            f"## 🏆 **DIVISION MVP RESULT**\n{lead_text(lead)}" +
            ("\n-# TEST PREVIEW · not counted" if test else "")))
        panel.add_item(discord.ui.Section(
            f"### **RAID #{raid_number:04d}**\n**DIVISIONAL RAIDERS:** `{raider_count:02d}`",
            accessory=discord.ui.Button(label="MVP Log", url=source_url)))
        panel.add_item(discord.ui.Separator())
        for d in order:
            medal = "  ·  **MVP**" if d in winners else ""
            panel.add_item(discord.ui.TextDisplay(
                f"## {DIV_EMOJI[d]} **DIVISION {d:02d}**{medal}\n"
                f"**MEMBERS JOINED:** `{tallies[d]:02d}`"))
            panel.add_item(discord.ui.Separator())
        winners_text = " × ".join(f"**DIVISION {d:02d}**" for d in winners) if winners else "No MVP awarded"
        panel.add_item(discord.ui.TextDisplay(f"### 🏆 **MVP DIVISION(S)**\n{winners_text}"))
        row = discord.ui.ActionRow()
        row.add_item(discord.ui.Button(label="Division Log", url=logs_url))
        row.add_item(LeaderboardButton())
        if not test:
            row.add_item(EditButton(raid_number))
        panel.add_item(row)
        self.add_item(panel)


class LeaderboardView(discord.ui.LayoutView):
    """Single-card division leaderboard with one View button under each division."""

    def __init__(self, totals: dict[int, int], history_url: str, images: bool = True, test: bool = False,
                 fixed: bool = False):
        super().__init__(timeout=None)
        ordered = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
        crown = (CROWN_EMOJI or "♛") if images else "♛"
        card = discord.ui.Container(accent_colour=0xF5C542)
        head = (f"## {crown} **DIVISION MVP LEADERBOARD**\n"
                f"### {lead_text(leaders_of(totals))}\n"
                "Top divisions by MVP points this season.")
        if test:
            head += "\n-# TEST PREVIEW  -  not counted anywhere"
        card.add_item(discord.ui.TextDisplay(head))
        card.add_item(discord.ui.Separator())
        for rank, (d, points) in enumerate(ordered, start=1):
            badge = DIV_EMOJI[d] if images else ""
            card.add_item(discord.ui.TextDisplay(
                f"**{badge} Division {d}**  ·  {crown} MVP count: `{points}`"))
            row = discord.ui.ActionRow()
            row.add_item(DivisionViewButton(d))
            if rank == len(ordered):
                if fixed:
                    row.add_item(LeaderboardRefreshButton())
                    row.add_item(discord.ui.Button(emoji="\U0001F4CA", style=discord.ButtonStyle.link, url=history_url))
                row.add_item(MyRankButton())
            card.add_item(row)
            if rank < len(ordered):
                card.add_item(discord.ui.Separator())
        stamp = int(datetime.now(timezone.utc).timestamp())
        clock = "\U0001F552 " if images else ""
        card.add_item(discord.ui.TextDisplay(f"{clock}Updated <t:{stamp}:R>  ·  Total Divisions: 5"))
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
        panel.add_item(discord.ui.TextDisplay(f"**DIVISION MVP LOG**\n{lead_text(lead or [])}"))
        if test:
            panel.add_item(discord.ui.TextDisplay("-# TEST PREVIEW  -  not counted anywhere"))
        mvp = "  \u00d7  ".join(f"DIVISION {d:02d}" for d in winners) if winners else "No MVP awarded"
        panel.add_item(discord.ui.TextDisplay(
            f"**RAID {raid_number:04d}**  ·  RAIDERS COUNTED `{len(raiders):02d}`\n**MVP:** {mvp}"))
        panel.add_item(discord.ui.Separator())
        for d in range(1, 6):
            members = by_division[d]
            head = f"**DIVISION {d:02d}**  ·  COUNT: `{tallies[d]:02d}`" + ("  **MVP**" if d in winners else "")
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


class TicketSessionInfoView(discord.ui.View):
    def __init__(self, source_ticket_url: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="Session Info", url=source_ticket_url))


class LiveTicketView(discord.ui.LayoutView):
    """One live post per ticket/backup: divisions 1 to 5 always listed, every helper mentioned under their division."""

    def __init__(self, guild_id: int, st: dict, images: bool = True, ticket_url: str | None = None,
                 voice_url: str | None = None, mirrored: bool = False, test: bool = False, archived: bool = False):
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
        title = "DIVISION BACKUP LOG" if archived else ("LIVE DIVISION RAID" if live else "DIVISION RAID - CLOSED")
        panel.add_item(discord.ui.TextDisplay(f"{title}\n{lead_text(leaders)}"))
        if test:
            panel.add_item(discord.ui.TextDisplay("-# TEST PREVIEW  -  not tracked"))
        if st.get("opener_id"):
            opener = f"<@{st['opener_id']}>" + (f"  (D{st['opener_division']})" if st.get("opener_division") else "")
        else:
            opener = "waiting for the ticket message"
        head = f"**TICKET**  `#{st['name']}`  \u00b7  HELPERS JOINED `{counted:02d}`"
        if st.get("total") is not None and int(st["total"]) != counted:
            head += f"  (ticket shows `{int(st['total']):02d}`)"
        panel.add_item(discord.ui.TextDisplay(f"{head}\n**Opened by:** {opener}"))
        panel.add_item(discord.ui.Separator())
        details = st.get("details", {})
        detail_sections = (
            ("🌐 Server Region", details.get("Server Region") or details.get("Region")),
            ("👥 Reported Players", details.get("Reported Players") or details.get("Players")),
            ("⚠️ Difficulty Level", details.get("Difficulty Level") or details.get("Difficulty")),
            ("📝 Description", details.get("Description")),
            ("📡 Session Info", details.get("Session Info") or details.get("Server Location")),
        )
        for label, value in detail_sections:
            if value:
                rows = [part.strip().replace("`", "ˋ") for part in str(value).splitlines() if part.strip()]
                if label.startswith("👥") and len(rows) == 1:
                    rows = [part.strip().replace("`", "ˋ") for part in re.split(r",\s*", rows[0]) if part.strip()]
                boxed = "\n".join(f"> `{part[:220]}`" for part in rows[:8])
            else:
                boxed = "> `Waiting for Crusher ticket details…`"
            panel.add_item(discord.ui.TextDisplay(f"### {label}\n{boxed}"))
            panel.add_item(discord.ui.Separator())
        for d in range(1, 6):
            members = by_division[d]
            body = fit_list(members, 520) if members else "No helpers yet"
            panel.add_item(division_section(f"**DIVISION {d:02d}**  ·  COUNT: `{len(members):02d}`\n{body}", d, images))
            panel.add_item(discord.ui.Separator())
        notes = []
        if no_division:
            notes.append(f"-# No division role (counted, not in a division): {fit_list(no_division, 300)}")
        stamp = int(datetime.now(timezone.utc).timestamp())
        notes.append(f"-# {'Live' if live else 'Closed'}  \u00b7  Updated <t:{stamp}:R>  \u00b7  {BOT_NAME}")
        notes.append("### 💡 **DIVISION SUPPORT**\nUse Ticket Info for the original request and Join VC to coordinate.")
        panel.add_item(discord.ui.TextDisplay("\n".join(notes)))
        if not archived:
            bottom = discord.ui.ActionRow()
            if mirrored and ticket_url:
                bottom.add_item(discord.ui.Button(label="Ticket Info", url=ticket_url))
                if voice_url:
                    bottom.add_item(discord.ui.Button(label="Join VC", emoji="🔊", url=voice_url))
                bottom.add_item(LeaderboardButton())
            elif live or test:
                bottom.add_item(discord.ui.Button(label="Open Ticket",
                                                  url=ticket_url or channel_url(guild_id, st["channel_id"])))
                bottom.add_item(LeaderboardButton())
            else:
                bottom.add_item(discord.ui.Button(label="Ticket Closed", style=discord.ButtonStyle.secondary, disabled=True))
                bottom.add_item(LeaderboardButton())
            panel.add_item(bottom)
        else:
            bottom = discord.ui.ActionRow()
            bottom.add_item(LeaderboardButton())
            panel.add_item(bottom)
        self.add_item(panel)


# ----------------------------------------------------------------------------
