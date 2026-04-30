from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from discord_bot.db import BotDB

if TYPE_CHECKING:
    from backend.state import AppState

log = logging.getLogger(__name__)


class BotCore(commands.Bot):
    def __init__(self, app_state: AppState) -> None:
        intents = discord.Intents(guilds=True, members=True)
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
        )
        self.app_state = app_state
        self.bot_db: BotDB  # assigned in setup_hook

    async def setup_hook(self) -> None:
        self.bot_db = BotDB()
        await self.bot_db.post_init()
        await self.load_extension("discord_bot.cogs.commands")
        await self.load_extension("discord_bot.cogs.server")
        if sync_guilds := os.environ.get("DISCORD_DEV_GUILD"):
            await self.tree.sync(guild=discord.Object(int(sync_guilds)))
            log.info("Synced command tree to dev guild %s", sync_guilds)
        elif os.environ.get("DISCORD_SYNC"):
            await self.tree.sync()
            log.info("Synced command tree globally")

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else None)
