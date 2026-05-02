"""All user-facing commands — account, dashboard, play."""

from __future__ import annotations

import contextlib
import math
import os
import random
from typing import TYPE_CHECKING, Any, cast

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

import backend.stats as bstats
from backend import db
from backend.auth import verify_discord_link_code
from discord_bot.cogs.colors import COLOR_LOSS, COLOR_NEUTRAL, COLOR_RECORD, COLOR_WIN
from discord_bot.db.types import DiscordId, GuildId, NotifyTier, Username

if TYPE_CHECKING:
    from discord_bot.bot import BotCore

#  Constants & helpers

SPELLINGBEE_URL = os.environ.get("SPELLINGBEE_URL", "https://spellingbee.app")

_MEDALS = ["🥇", "🥈", "🥉"]

_TIER_DESCRIPTIONS: dict[int, str] = {
    1: "**Critical** — server records and badge announcements only.",
    2: "**Social** — also notified when players you follow complete matches.",
    3: "**Personal** — also notified for streaks at risk, ELO milestones, and weekly summaries.",
    4: "**Ambient** — daily word facts included.",
}


def trend(delta: float | None) -> str:
    if delta is None:
        return "─"
    if delta > 0:
        return f"▲{delta:+.0f}"
    if delta < 0:
        return f"▼{abs(delta):.0f}"
    return "─"


def profile_view(username: str) -> discord.ui.View:
    view = discord.ui.View()
    view.add_item(
        discord.ui.Button(
            label="Open Profile →",
            url=f"{SPELLINGBEE_URL}/user/{username}",
            style=discord.ButtonStyle.link,
        ),
    )
    return view


async def _tier_autocomplete(
    _self: Any,
    _interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    rows = await db.fetchall(
        "SELECT DISTINCT tier FROM word_stats WHERE tier!='' AND tier LIKE ? LIMIT 10",
        (f"{current}%",),
    )
    return [app_commands.Choice(name=r["tier"], value=r["tier"]) for r in rows]


async def _word_autocomplete(
    _self: Any,
    _interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    rows = await db.fetchall(
        "SELECT DISTINCT word FROM word_stats WHERE word LIKE ? LIMIT 10",
        (f"{current}%",),
    )
    return [app_commands.Choice(name=r["word"], value=r["word"]) for r in rows]


_tier_autocomplete.__qualname__ = "Cog._tier_autocomplete"
_word_autocomplete.__qualname__ = "Cog._word_autocomplete"


async def user_autocomplete(
    _self: Any,
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Username autocomplete — guild-scoped when in a guild, global otherwise."""
    if interaction.guild_id:
        rows = await db.fetchall(
            "SELECT du.username FROM discord_users du"
            " JOIN guild_members gm ON gm.discord_id=du.discord_id"
            " WHERE gm.guild_id=? AND du.username LIKE ? LIMIT 10",
            (str(interaction.guild_id), f"{current}%"),
        )
    else:
        rows = await db.fetchall(
            "SELECT username FROM discord_users WHERE username LIKE ? LIMIT 10",
            (f"{current}%",),
        )
    return [app_commands.Choice(name=r["username"], value=r["username"]) for r in rows]


# discord.py's is_inside_class() checks __qualname__ to decide whether to pass the cog
# binding as `self`. Mark module-level autocomplete functions so the binding is applied.
user_autocomplete.__qualname__ = "Cog.user_autocomplete"


async def resolve_username(
    bot: Any,
    interaction: discord.Interaction,
    user_arg: str | None,
    *,
    send_error: bool = True,
) -> str | None:
    if user_arg is not None:
        return user_arg
    user = await bot.bot_db.users.get_by_discord_id(DiscordId(str(interaction.user.id)))
    if user is None:
        if send_error:
            await interaction.response.send_message(
                "Link your account first with `/link`.",
                ephemeral=True,
            )
        return None
    return user.username


#  Play helpers


class ChallengeView(discord.ui.View):
    def __init__(
        self,
        challenger_name: str,
        target_name: str,
        target_discord_id: str,
        base_url: str,
    ) -> None:
        super().__init__(timeout=300)
        self.challenger_name = challenger_name
        self.target_name = target_name
        self.target_discord_id = target_discord_id
        self.base_url = base_url

    def _check_target(self, interaction: discord.Interaction) -> bool:
        return str(interaction.user.id) == self.target_discord_id

    async def _respond(self, interaction: discord.Interaction, *, accepted: bool) -> None:
        if not self._check_target(interaction):
            await interaction.response.send_message("This isn't for you!", ephemeral=True)
            return
        self.stop()
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(view=self)
        msg = (
            f"**{self.target_name}** accepted the challenge! [Start here]({self.base_url}/)"
            if accepted
            else f"**{self.target_name}** declined."
        )
        await interaction.followup.send(msg, ephemeral=not accepted)

    @discord.ui.button(label="✅ Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._respond(interaction, accepted=True)

    @discord.ui.button(label="❌ Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._respond(interaction, accepted=False)


def _make_bracket(players: list[str]) -> str:
    n = 1 if len(players) < 2 else 2 ** math.ceil(math.log2(len(players)))
    players = players + ["BYE"] * (n - len(players))
    lines = ["**Round 1**"]
    lines.extend(f"  {players[i]} vs {players[i + 1]}" for i in range(0, len(players), 2))
    return "\n".join(lines)


#  Account commands
#  Dashboard commands
#  Play commands


class CommandsCog(commands.Cog):
    def __init__(self, bot: BotCore) -> None:
        self.bot = bot
        self._populated_guilds: set[str] = set()

    #  Account groups

    account_group = app_commands.Group(
        name="account",
        description="Link and manage your Spelling Bee account.",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
        allowed_contexts=app_commands.AppCommandContext(
            guild=True,
            dm_channel=True,
            private_channel=True,
        ),
    )

    notify_group = app_commands.Group(
        name="notify",
        description="Manage your notification preferences.",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
        allowed_contexts=app_commands.AppCommandContext(
            guild=True,
            dm_channel=True,
            private_channel=True,
        ),
    )

    #  Dashboard groups

    server_group = app_commands.Group(
        name="server",
        description="Server overview.",
        guild_only=True,
    )

    #  Account helpers

    async def _ensure_guild_populated(self, guild: discord.Guild) -> None:
        guild_id_str = str(guild.id)
        if guild_id_str in self._populated_guilds:
            return
        await self.bot.bot_db.guilds.bulk_add_members(
            GuildId(guild_id_str),
            [DiscordId(str(m.id)) for m in guild.members],
        )
        self._populated_guilds.add(guild_id_str)

    #  Account commands

    @account_group.command(name="link", description="Link your Discord to a Spelling Bee username.")
    @app_commands.describe(
        username="Your Spelling Bee username",
        code="Link code from your account page",
    )
    async def link(self, interaction: discord.Interaction, username: str, code: str) -> None:
        if not verify_discord_link_code(username, code):
            await interaction.response.send_message(
                "Invalid or expired code. Get a fresh one from your account page.",
                ephemeral=True,
            )
            return

        if interaction.guild is not None:
            await self._ensure_guild_populated(interaction.guild)

        try:
            await self.bot.bot_db.users.link(
                DiscordId(str(interaction.user.id)),
                Username(username),
            )
        except aiosqlite.IntegrityError:
            await interaction.response.send_message(
                f"No account found for **{username}**.",
                ephemeral=True,
            )
            return

        if interaction.guild_id is not None:
            await self.bot.bot_db.guilds.add_member(
                GuildId(str(interaction.guild_id)),
                DiscordId(str(interaction.user.id)),
            )

            if interaction.guild is not None:
                guild_config = await self.bot.bot_db.guilds.get_config(
                    GuildId(str(interaction.guild_id)),
                )
                if guild_config is not None and guild_config.linked_role_id is not None:
                    role = interaction.guild.get_role(int(guild_config.linked_role_id))
                    if role is not None:
                        member = interaction.guild.get_member(interaction.user.id)
                        if member is not None:
                            with contextlib.suppress(discord.Forbidden):
                                await member.add_roles(role)

        await interaction.response.send_message(
            f"Linked to **{username}**.",
            ephemeral=False,
        )

    @account_group.command(name="unlink", description="Unlink your Discord from Spelling Bee.")
    async def unlink(self, interaction: discord.Interaction) -> None:
        deleted = await self.bot.bot_db.users.unlink(
            DiscordId(str(interaction.user.id)),
        )
        if deleted:
            await interaction.response.send_message("Account unlinked.", ephemeral=True)
        else:
            await interaction.response.send_message(
                "No linked account found.",
                ephemeral=True,
            )

    @account_group.command(name="whois", description="Look up a member's Spelling Bee account.")
    @app_commands.describe(member="The server member to look up")
    async def whois(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if interaction.guild is not None:
            await self._ensure_guild_populated(interaction.guild)

        user = await self.bot.bot_db.users.get_by_discord_id(
            DiscordId(str(member.id)),
        )
        if user is None:
            await interaction.response.send_message(
                "That member hasn't linked a Spelling Bee account.",
                ephemeral=True,
            )
            return

        profile_url = f"{SPELLINGBEE_URL}/user/{user.username}"
        embed = discord.Embed(
            title=member.display_name,
            description=f"Linked to **{user.username}**",
            color=COLOR_NEUTRAL,
        )
        view = discord.ui.View()
        view.add_item(
            discord.ui.Button(
                label="Open Profile →",
                url=profile_url,
                style=discord.ButtonStyle.link,
            ),
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=False)

    @notify_group.command(name="tier", description="Set your notification verbosity level.")
    @app_commands.describe(level="1=Critical, 2=Social, 3=Personal, 4=Ambient")
    async def notify_tier(
        self,
        interaction: discord.Interaction,
        level: app_commands.Range[int, 1, 4],
    ) -> None:
        user = await self.bot.bot_db.users.get_by_discord_id(
            DiscordId(str(interaction.user.id)),
        )
        if user is None:
            await interaction.response.send_message(
                "Link your account first with `/link`.",
                ephemeral=True,
            )
            return

        await self.bot.bot_db.users.set_notify_tier(
            DiscordId(str(interaction.user.id)),
            cast("NotifyTier", level),
        )
        description = _TIER_DESCRIPTIONS[level]
        await interaction.response.send_message(
            f"Notification tier set to {level}: {description}",
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        await self.bot.bot_db.guilds.add_member(
            GuildId(str(member.guild.id)),
            DiscordId(str(member.id)),
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if member.bot:
            return
        await self.bot.bot_db.guilds.remove_member(
            GuildId(str(member.guild.id)),
            DiscordId(str(member.id)),
        )

    #  Dashboard commands

    @server_group.command(name="stats", description="Server stats overview.")
    async def server_stats(self, interaction: discord.Interaction) -> None:
        d = await bstats.fetch_server_dashboard(str(interaction.guild_id))
        s, lb, hof = d["stats"], d["lb"], d["hof"]
        spot, nem = d["spotlight"], d["nemesis"]
        lines = [
            f"**Members:** {s.members} · **Avg ELO:** {s.avg_elo or '—'}"
            f" · **Matches:** {s.total_matches}",
            f"**Health:** {s.active_today} active today · {s.matches_week} matches this week",
        ]
        if lb:
            lb_text = " · ".join(f"{e.username} {e.elo:.0f}" for e in lb[:5])
            lines.append(f"**Top 5:** {lb_text}")
        if spot:
            lines.append(f"🌟 **Spotlight:** {spot['username']} +{spot['gain']:.0f} ELO this week")
        if nem:
            lines.append(f"🐛 **Nemesis:** {nem['word']} — {nem['accuracy']}% accuracy")
        if hof["peak_elo"]:
            p = hof["peak_elo"]
            lines.append(f"🏆 **Peak ELO:** {p['username']} — {p['peak_elo']:.0f}")
        embed = discord.Embed(
            title=f"🐝 {interaction.guild.name}",  # type: ignore[union-attr]
            description="\n".join(lines),
            color=COLOR_NEUTRAL,
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="me", description="Your stats dashboard.")
    @app_commands.describe(user="Username (default: yourself)")
    @app_commands.autocomplete(user=user_autocomplete)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def me(self, interaction: discord.Interaction, user: str | None = None) -> None:
        username = await resolve_username(self.bot, interaction, user)
        if username is None:
            return
        d = await bstats.fetch_player(username)
        if d is None:
            await interaction.response.send_message("No account found.", ephemeral=False)
            return
        p, r, t = d["profile"], d["rank"], d["today"]
        acc = f"{100 * p.correct / p.words:.1f}%" if p.words else "—"
        rank_line = f"#{r.rank} of {r.total} (top {r.top_pct:.0f}%)" if r else "unranked"
        wpm_line = f"{p.best_wpm} on **{p.best_word}**" if p.best_wpm else "—"
        week = trend(r.elo - r.elo_7d_ago) + " this week" if r and r.elo_7d_ago else ""
        lines = [
            f"**ELO:** {p.elo:.0f} · {rank_line} {week}",
            f"**Games:** {p.games} · **Wins:** {p.wins} · **Accuracy:** {acc}",
            f"**Best WPM:** {wpm_line} · **Best Streak:** {p.best_streak}",
            f"**Current Streak:** {d['streak']}",
            "",
            f"📅 **Today** — {t.matches} matches · {t.correct}/{t.attempts} correct"
            + (f" · best {t.best_wpm:.0f} WPM" if t.best_wpm else ""),
            f"🎖️ {len(d['badges'])} badges earned",
        ]
        embed = discord.Embed(
            title=f"🐝 {username}",
            description="\n".join(lines),
            color=COLOR_NEUTRAL,
        )
        await interaction.response.send_message(
            embed=embed,
            view=profile_view(username),
            ephemeral=user is None,
        )

    @app_commands.command(name="vs", description="Head-to-head record against another player.")
    @app_commands.describe(opponent="Opponent username")
    @app_commands.autocomplete(opponent=user_autocomplete)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def vs(self, interaction: discord.Interaction, opponent: str) -> None:
        caller = await resolve_username(self.bot, interaction, None)
        if caller is None:
            return
        d = await bstats.fetch_versus(caller, opponent)
        h2h, stake = d["h2h"], d["stake"]
        cp, co = d["clutch_player"], d["clutch_opponent"]
        total = h2h.total_games
        win_rate = h2h.player_wins / total if total else 0.0
        gap = abs(h2h.player_elo - h2h.opponent_elo)
        elo_line = (
            f"▲{gap:.0f} ELO ahead"
            if h2h.player_elo >= h2h.opponent_elo
            else f"▼{gap:.0f} ELO behind"
        )
        lines = [
            f"**Record:** {h2h.player_wins}W - {h2h.opponent_wins}L"
            f" ({total} matches · {win_rate:.0%} win rate)",
            f"**ELO:** {elo_line} · stake +{stake.win}/{stake.loss}",
            f"**Clutch:** {caller} {cp['rate']:.0%} vs {opponent} {co['rate']:.0%} (3+ lobbies)",
        ]
        color = COLOR_WIN if h2h.player_wins >= h2h.opponent_wins else COLOR_LOSS
        embed = discord.Embed(
            title=f"{caller} vs {opponent}",
            description="\n".join(lines),
            color=color,
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="word", description="Community stats for a word.")
    @app_commands.describe(word="The word to look up")
    @app_commands.autocomplete(word=_word_autocomplete)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def word(self, interaction: discord.Interaction, word: str) -> None:
        guild_id = str(interaction.guild_id) if interaction.guild_id else None
        linked = await self.bot.bot_db.users.get_by_discord_id(DiscordId(str(interaction.user.id)))
        username = linked.username if linked else None
        d = await bstats.fetch_word_card(word, username=username, guild_id=guild_id)
        if d is None:
            await interaction.response.send_message(f"No stats for **{word}**.", ephemeral=True)
            return
        gs = d["global_stats"]
        race = d["race"]
        lines = [f"**Community:** {gs['attempts']} attempts · {gs['accuracy']:.1f}% accuracy"]
        if race:
            podium = " · ".join(
                f"{_MEDALS[i] if i < 3 else f'{i + 1}.'} **{e.username}** {e.wpm:.0f} WPM"
                for i, e in enumerate(race[:3])
            )
            lines.append(f"**Race:** {podium}")
        if "personal" in d and d["personal"].tries > 0:
            ws = d["personal"]
            pr = f"{ws.hits}/{ws.tries} correct"
            if ws.best_wpm:
                pr += f" · best {ws.best_wpm:.0f} WPM"
            lines.append(f"**Your record:** {pr}")
        if d.get("witnesses"):
            lines.append(f"**Server:** {len(d['witnesses'])} linked member(s) have beaten it")
        embed = discord.Embed(title=f"🔤 {word}", description="\n".join(lines), color=COLOR_NEUTRAL)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="study", description="Study plan — most-missed words and tier gaps.")
    @app_commands.describe(user="Username (default: yourself)")
    @app_commands.autocomplete(user=user_autocomplete)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def study(self, interaction: discord.Interaction, user: str | None = None) -> None:
        username = await resolve_username(self.bot, interaction, user)
        if username is None:
            return
        d = await bstats.fetch_study_plan(username)
        weakest, sl, unbeaten = d["weakest_tier"], d["study"], d["unbeaten"]
        lines: list[str] = []
        if weakest:
            lines.append(
                f"**Weakest tier:** {weakest['tier']} — {float(weakest['accuracy']):.1f}% accuracy",
            )
        if sl:
            lines.append("**Most missed:**")
            for i, item in enumerate(sl[:5]):
                lines.append(f"{i + 1}. **{item.word}** ({item.n} misses)")
        lines.append(f"**Unbeaten words:** {len(unbeaten)}")
        embed = discord.Embed(
            title=f"📚 {username}'s Study Plan",
            description="\n".join(lines) or "Nothing to study!",
            color=COLOR_NEUTRAL,
        )
        await interaction.response.send_message(
            embed=embed,
            view=profile_view(username),
            ephemeral=user is None,
        )

    #  Link commands

    @app_commands.command(name="invite", description="Post a public lobby link to this channel.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def invite(self, interaction: discord.Interaction) -> None:
        url = f"{SPELLINGBEE_URL}/"
        embed = discord.Embed(
            title="🐝 Spelling Bee — Join a Game",
            description=f"[Open →]({url})",
            color=COLOR_NEUTRAL,
        )
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Open →", url=url, style=discord.ButtonStyle.link))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=False)

    @app_commands.command(name="spectate", description="Watch a live public match.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def spectate(self, interaction: discord.Interaction) -> None:
        url = f"{SPELLINGBEE_URL}/public/join"
        embed = discord.Embed(
            title="👀 Spectate",
            description=f"[Open →]({url})",
            color=COLOR_NEUTRAL,
        )
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Open →", url=url, style=discord.ButtonStyle.link))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=False)

    #  Play commands

    @app_commands.command(name="play", description="Start a Spelling Bee game.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(tier="Optional word tier to filter by")
    async def play(
        self,
        interaction: discord.Interaction,
        tier: str | None = None,
    ) -> None:
        if tier:
            url = f"{SPELLINGBEE_URL}/?tier={tier}"
            desc = f"[Click here to open a room]({url})\n\nFiltered to tier: **{tier}**"
        else:
            url = f"{SPELLINGBEE_URL}/"
            desc = f"[Click here to open a room]({url})"

        embed = discord.Embed(
            title="🐝 Start a Game",
            description=desc,
            color=COLOR_WIN,
        )
        await interaction.response.send_message(embed=embed, ephemeral=False)

    @play.autocomplete("tier")
    async def tier_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await _tier_autocomplete(self, interaction, current)

    @app_commands.command(
        name="challenge",
        description="Challenge another player to a match.",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(target="The Spelling Bee username to challenge")
    @app_commands.autocomplete(target=user_autocomplete)
    async def challenge(
        self,
        interaction: discord.Interaction,
        target: str,
    ) -> None:
        caller_name = await resolve_username(self.bot, interaction, None)
        if caller_name is None:
            return

        target_user = await self.bot.bot_db.users.get_by_username(Username(target))
        if target_user is None:
            await interaction.response.send_message(
                f"No linked Discord account found for **{target}**.",
                ephemeral=False,
            )
            return
        target_discord_id = target_user.discord_id

        embed = discord.Embed(
            title=f"⚔️ Challenge from **{caller_name}**",
            description=f"@{target}, you've been challenged to a Spelling Bee match!",
            color=COLOR_RECORD,
        )
        view = ChallengeView(
            challenger_name=caller_name,
            target_name=target,
            target_discord_id=target_discord_id,
            base_url=SPELLINGBEE_URL,
        )
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(
        name="rematch",
        description="Challenge someone to a rematch of your last shared game.",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(user="The Spelling Bee username to rematch")
    @app_commands.autocomplete(user=user_autocomplete)
    async def rematch(self, interaction: discord.Interaction, user: str) -> None:
        caller_name = await resolve_username(self.bot, interaction, None)
        if caller_name is None:
            return

        row = await db.fetchone(
            """
            SELECT mr.match_id, m.ts FROM match_results mr
            JOIN matches m ON m.id = mr.match_id
            WHERE mr.username = ?
              AND mr.match_id IN (
                  SELECT match_id FROM match_results WHERE username = ?
              )
            ORDER BY mr.ts DESC LIMIT 1
            """,
            (caller_name, user),
        )

        if row is None:
            await interaction.response.send_message(
                f"No shared match history found with **{user}**.",
                ephemeral=False,
            )
            return

        ts = row["ts"]
        embed = discord.Embed(
            title=f"🔁 Rematch — Last played <t:{ts}:R>",
            description=f"Ready for a rematch against **{user}**?",
            color=COLOR_WIN,
        )
        view = discord.ui.View()
        view.add_item(
            discord.ui.Button(
                label="Start here →",
                url=f"{SPELLINGBEE_URL}/",
                style=discord.ButtonStyle.link,
            ),
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=False)

    @app_commands.command(
        name="tournament",
        description="Generate a single-elimination tournament bracket.",
    )
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    @app_commands.describe(
        participants="Space-separated usernames (leave blank to use all linked guild members)",
    )
    async def tournament(
        self,
        interaction: discord.Interaction,
        participants: str | None = None,
    ) -> None:
        if participants:
            players = [p.strip() for p in participants.split() if p.strip()]
        else:
            if interaction.guild_id is None:
                await interaction.response.send_message(
                    "Run this command in a server.",
                    ephemeral=True,
                )
                return
            rows = await db.fetchall(
                "SELECT username FROM guild_linked_users WHERE guild_id=?",
                (GuildId(str(interaction.guild_id)),),
            )
            players = [r["username"] for r in rows]

        if len(players) < 2:
            await interaction.response.send_message(
                "Need at least 2 participants to make a bracket.",
                ephemeral=False,
            )
            return

        random.shuffle(players)
        bracket = _make_bracket(players)

        embed = discord.Embed(
            title="🏆 Tournament Bracket",
            description=bracket,
            color=COLOR_RECORD,
        )
        embed.set_footer(text="Players self-report results · Bot provides structure only")
        await interaction.response.send_message(embed=embed)


async def setup(bot: BotCore) -> None:
    await bot.add_cog(CommandsCog(bot))
