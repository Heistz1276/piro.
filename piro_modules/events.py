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
    await mirror_reconcile(guild)
    await live_reconcile(guild)
    await live_sync(guild)
    for row in db.execute("SELECT channel_id FROM live_tickets WHERE guild_id=? AND status='live'",
                          (guild.id,)).fetchall():
        mirror = mirror_by_division_channel(int(row["channel_id"]))
        st = live_load(int(row["channel_id"])) if mirror else None
        source = guild.get_channel(int(mirror["source_channel_id"])) if mirror else None
        if st is not None:
            # Rebuild active ticket helpers from explicit join notices so old opener/mention
            # false positives are removed after upgrading, then redraw the card without signup buttons.
            st["helpers"] = []
            st["opener_id"] = None
            st["opener_division"] = None
            st["total"] = None
            if isinstance(source, discord.TextChannel):
                await live_catch_up(st, source)
            live_refresh_divisions(st, guild)
            live_store(st)
            await live_publish(guild, st)


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
    if isinstance(message.channel, discord.TextChannel) and mirror_by_division_channel(message.channel.id):
        await update_ticket_messy_prompt(message)
    if source_message(message):
        await process_mvp(message)
        return
    mirror = mirror_by_source(message.channel.id)
    if mirror and message.author.bot and not is_me(message.author):
        target = message.guild.get_channel(int(mirror["division_channel_id"]))
        if isinstance(target, discord.TextChannel):
            await live_ingest(message, channel=target)
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
    mirror = mirror_by_source(payload.channel_id)
    if mirror:
        message = await try_get_message(payload.channel_id, payload.message_id)
        target = bot.get_channel(int(mirror["division_channel_id"]))
        if message is not None and target is not None and message.author.bot and not is_me(message.author):
            await live_ingest(message, channel=target)
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
    if not isinstance(channel, discord.TextChannel):
        return
    if channel.name.casefold().startswith("div |"):
        return  # create_division_ticket bootstraps and pins this channel after saving its source link
    possible_crusher_ticket = (channel.category_id == CRUSHER_TICKET_CATEGORY_ID if CRUSHER_TICKET_CATEGORY_ID
                               else bool(re.search(r"ticket|backup|raid|support", channel.name, re.I)))
    if possible_crusher_ticket and await is_crusher_ticket(channel):
        await create_division_ticket(channel)
        return
    if is_live_channel(channel):
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
    source_mirror = mirror_by_source(channel.id)
    if source_mirror:
        await remove_ticket_mirror(channel.guild, source_mirror, source_was_deleted=True)
        return
    division_mirror = mirror_by_division_channel(channel.id)
    if division_mirror:
        await remove_ticket_mirror(channel.guild, division_mirror, source_was_deleted=False)
        return
    if is_live_channel(channel):
        await live_close(channel.guild, channel.id)


# ----------------------------------------------------------------------------
