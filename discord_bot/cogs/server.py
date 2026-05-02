"""Server cog — admin commands + notification infrastructure."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from backend import db
from backend.state import MatchCompleteEvent
from discord_bot.cogs.colors import COLOR_LOSS, COLOR_NEUTRAL, COLOR_RECORD
from discord_bot.db.types import DiscordId, GuildId, Username

if TYPE_CHECKING:
    from discord_bot.bot import BotCore

log = logging.getLogger(__name__)


def _admin_only(func: Any) -> Any:
    @functools.wraps(func)
    async def wrapper(
        self: Any,
        interaction: discord.Interaction,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        if not interaction.user.guild_permissions.manage_guild:  # type: ignore[union-attr]
            await interaction.response.send_message(
                "Requires Manage Server permission.",
                ephemeral=True,
            )
            return None
        return await func(self, interaction, *args, **kwargs)

    return wrapper


class ServerCog(commands.Cog):
    def __init__(self, bot: BotCore) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self._consume_loop.start()

    async def cog_unload(self) -> None:
        self._consume_loop.cancel()

    #  Notification loop

    @tasks.loop()
    async def _consume_loop(self) -> None:
        try:
            event = await self.bot.app_state.event_queue.get()
            match event:
                case MatchCompleteEvent(visibility=v) if v not in ("private", "local"):
                    coros = [self._deliver_match_to_followers(event)]
                    if event.rank == 1 and event.n_players >= 3:
                        coros.append(self._deliver_match_announcement(event))
                    await asyncio.gather(*coros)
        except Exception:
            log.exception("Notification consume error")

    @_consume_loop.before_loop
    async def _before_consume(self) -> None:
        await self.bot.wait_until_ready()

    async def _deliver_match_to_followers(self, event: MatchCompleteEvent) -> None:
        follower_ids = await self.bot.bot_db.users.get_notify_followers(Username(event.username))
        embed = discord.Embed(
            description=f"**{event.username}** finished rank #{event.rank} in a match.",
            color=COLOR_NEUTRAL,
        )
        for discord_id in follower_ids:
            user = self.bot.get_user(int(discord_id))
            if user is None:
                continue
            with contextlib.suppress(discord.Forbidden):
                await user.send(embed=embed)

    async def _deliver_match_announcement(self, event: MatchCompleteEvent) -> None:
        channel_ids = await self.bot.bot_db.guilds.get_announce_channels_for_username(
            Username(event.username),
        )
        embed = discord.Embed(
            title="Match Win",
            description=f"**{event.username}** won a {event.n_players}-player match!",
            color=COLOR_RECORD,
        )
        for channel_id in channel_ids:
            channel = self.bot.get_channel(int(channel_id))
            if not isinstance(channel, discord.TextChannel | discord.Thread):
                continue
            with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                await channel.send(embed=embed)

    #  Admin groups

    admin_group = app_commands.Group(
        name="admin",
        description="Admin-only server management.",
        guild_only=True,
    )

    config_group = app_commands.Group(
        name="config",
        description="Configure bot settings for this server.",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=False),
        allowed_contexts=app_commands.AppCommandContext(
            guild=True,
            dm_channel=False,
            private_channel=False,
        ),
    )

    #  Admin commands

    @admin_group.command(
        name="announce",
        description="Post an announcement to the configured announcements channel.",
    )
    @app_commands.describe(message="The message to post")
    @_admin_only
    async def server_announce(self, interaction: discord.Interaction, message: str) -> None:
        guild_id = GuildId(str(interaction.guild_id))
        config = await self.bot.bot_db.guilds.get_config(guild_id)
        if config is None or config.announce_channel_id is None:
            await interaction.response.send_message(
                "No announcement channel set. Use `/config channel` first.",
                ephemeral=True,
            )
            return
        channel = interaction.guild.get_channel(int(config.announce_channel_id))  # type: ignore[union-attr]
        if channel is None or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Configured channel not found. Use `/config channel` to update it.",
                ephemeral=True,
            )
            return
        await channel.send(message)
        await interaction.response.send_message("Announcement posted.", ephemeral=True)

    @admin_group.command(
        name="suspend",
        description="Check a member's game account suspension status. Requires Manage Server.",
    )
    @app_commands.describe(member="The server member to check")
    @_admin_only
    async def server_suspend(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        discord_user = await self.bot.bot_db.users.get_by_discord_id(DiscordId(str(member.id)))
        embed = discord.Embed(color=COLOR_NEUTRAL)
        if discord_user is None:
            embed.description = "That member hasn't linked a Spelling Bee account."
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        row = await db.fetchone(
            "SELECT username, suspended_until,"
            " CASE WHEN suspended_until>UNIXEPOCH() THEN 1 ELSE 0 END AS is_suspended"
            " FROM users WHERE username=?",
            (discord_user.username,),
        )
        if row is None:
            embed.description = "That member hasn't linked a Spelling Bee account."
        elif row["is_suspended"]:
            embed.description = (
                f"**{row['username']}** is suspended until <t:{row['suspended_until']}:F>"
            )
            embed.color = COLOR_LOSS
        else:
            embed.description = f"**{row['username']}** is not suspended."
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @config_group.command(name="channel", description="Set the channel for bot announcements.")
    @app_commands.describe(channel="The text channel to use for announcements")
    @_admin_only
    async def config_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        await self.bot.bot_db.guilds.set_announce_channel(
            GuildId(str(interaction.guild_id)),
            DiscordId(str(channel.id)),
        )
        await interaction.response.send_message(
            f"Announcements channel set to {channel.mention}.",
            ephemeral=True,
        )

    @config_group.command(
        name="role",
        description="Set the role automatically assigned to linked members.",
    )
    @app_commands.describe(role="The role to assign on account link")
    @_admin_only
    async def config_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        await self.bot.bot_db.guilds.set_linked_role(
            GuildId(str(interaction.guild_id)),
            DiscordId(str(role.id)),
        )
        await interaction.response.send_message(
            f"Linked role set to {role.mention}.",
            ephemeral=True,
        )

    @config_group.command(
        name="leaderboard-post",
        description="Toggle the weekly leaderboard digest for this server.",
    )
    @_admin_only
    async def config_leaderboard_post(self, interaction: discord.Interaction) -> None:
        guild_id = GuildId(str(interaction.guild_id))
        config = await self.bot.bot_db.guilds.get_config(guild_id)
        currently_enabled = config is not None and config.leaderboard_post
        new_state = not currently_enabled
        await self.bot.bot_db.guilds.set_leaderboard_post(guild_id, enabled=new_state)
        label = "**enabled**" if new_state else "**disabled**"
        await interaction.response.send_message(
            f"Weekly leaderboard digest {label}.",
            ephemeral=True,
        )


async def setup(bot: BotCore) -> None:
    await bot.add_cog(ServerCog(bot))
