-- Discord bot tables — loaded against the shared game database on bot startup.
-- The game runs without these tables. The bot cannot run without the game tables.
-- All FKs reference game tables directly; no cross-file lookups needed.

CREATE TABLE IF NOT EXISTS discord_users (
    discord_id   TEXT    PRIMARY KEY,
    username     TEXT    NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    notify_tier  INTEGER NOT NULL DEFAULT 1 CHECK(notify_tier BETWEEN 1 AND 4),
    flex_opt_in  INTEGER NOT NULL DEFAULT 0 CHECK(flex_opt_in IN (0, 1)),
    last_delivered_ts INTEGER NOT NULL DEFAULT 0,
    linked_at    INTEGER NOT NULL DEFAULT (UNIXEPOCH())
) STRICT;
CREATE INDEX IF NOT EXISTS ix_du_username ON discord_users(username);

CREATE TABLE IF NOT EXISTS guild_config (
    guild_id             TEXT PRIMARY KEY,
    announce_channel_id  TEXT,
    linked_role_id       TEXT,
    leaderboard_post     INTEGER NOT NULL DEFAULT 1 CHECK(leaderboard_post IN (0, 1))
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS guild_members (
    guild_id    TEXT    NOT NULL,
    discord_id  TEXT    NOT NULL REFERENCES discord_users(discord_id) ON DELETE CASCADE,
    PRIMARY KEY (guild_id, discord_id)
) STRICT, WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_gm_discord_id ON guild_members(discord_id);

CREATE TABLE IF NOT EXISTS guild_goals (
    id          INTEGER PRIMARY KEY,
    guild_id    TEXT    NOT NULL UNIQUE,
    metric      TEXT    NOT NULL CHECK(metric IN ('correct', 'attempts', 'matches', 'wins')),
    target      INTEGER NOT NULL CHECK(target > 0),
    start_ts    INTEGER NOT NULL DEFAULT (UNIXEPOCH()),
    deadline_ts INTEGER
) STRICT;

-- Guild-scoped game queries use this view as their scope filter.
-- Replaces the Python-side get_linked_usernames() list roundtrip.
--
-- Usage in any guild-scoped query:
--   WHERE username IN (SELECT username FROM guild_linked_users WHERE guild_id = ?)
DROP VIEW IF EXISTS guild_linked_users;
CREATE VIEW guild_linked_users AS
SELECT gm.guild_id, du.username, du.discord_id, du.notify_tier
FROM guild_members gm
JOIN discord_users du ON du.discord_id = gm.discord_id;
