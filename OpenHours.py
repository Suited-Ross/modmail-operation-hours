"""
Opening Hours plugin for Modmail (https://github.com/modmail-dev/Modmail)

Features
--------
- Define a weekly opening-hours schedule (per weekday, supports overnight
  ranges like 22:00-06:00, and fully-closed days).
- Automatically flips Modmail's built-in `dm_disabled` setting between
  NONE (open) and NEW_THREADS (closed) so brand new DMs are blocked while
  the bot is "closed", while EXISTING open threads keep working normally.
- When closed, DMs are met with Modmail's normal "new thread blocked"
  embed -- fully customisable (title / description / colour) with the
  `?hours embed` commands, exactly like other Modmail embeds.
- Logs every open <-> closed transition to a channel of your choice.
- Manual override (force open / force closed / back to automatic).
- All settings persist in the bot's database via the plugin partition.

Commands
--------
?hours                       - quick status + command list
?hours status                - detailed open/closed status
?hours schedule               - view the weekly schedule
?hours set <day> <open> <close>   - set hours for a day (or "all")
?hours closed <day>            - mark a day fully closed
?hours open <day>              - unmark a day as closed
?hours timezone <tz>           - set the IANA timezone, e.g. Europe/London
?hours logchannel <#channel>   - channel for open/close logs
?hours toggle                  - enable/disable automatic scheduling
?hours managedm <on|off>       - let this plugin control dm_disabled or not
?hours override <open|closed|auto> - manual override
?hours embed title <text>
?hours embed description <text>
?hours embed color <hex>
?hours embed reset
?hours embed preview
"""

import re
from datetime import datetime, timedelta, time as dtime

import discord
from discord.ext import commands, tasks

from core import checks
from core.models import DMDisabled, PermissionLevel, getLogger

logger = getLogger(__name__)

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_NAMES = {
    "mon": "Monday",
    "tue": "Tuesday",
    "wed": "Wednesday",
    "thu": "Thursday",
    "fri": "Friday",
    "sat": "Saturday",
    "sun": "Sunday",
}
DAY_ALIASES = {
    "monday": "mon", "mon": "mon",
    "tuesday": "tue", "tue": "tue", "tues": "tue",
    "wednesday": "wed", "wed": "wed",
    "thursday": "thu", "thu": "thu", "thur": "thu", "thurs": "thu",
    "friday": "fri", "fri": "fri",
    "saturday": "sat", "sat": "sat",
    "sunday": "sun", "sun": "sun",
    "all": "all", "everyday": "all", "every": "all", "daily": "all",
}

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")

DEFAULT_EMBED = {
    "title": "We're currently closed",
    "description": (
        "Thanks for reaching out! Our support team isn't available right now.\n\n"
        "**We'll be back:** {next_open}\n"
        "**Current time:** {time} ({timezone})\n\n"
        "Please try again during our opening hours, or check back for the "
        "schedule with the server's help command."
    ),
}


def _default_schedule():
    return {d: {"open": "09:00", "close": "17:00", "closed": False} for d in DAY_KEYS}


def _default_config():
    return {
        "_id": "config",
        "enabled": True,
        "manage_dm_disabled": True,
        "timezone": "UTC",
        "override": None,  # None | "open" | "closed"
        "schedule": _default_schedule(),
        "log_channel": None,
        "last_state": None,
        "embed": dict(DEFAULT_EMBED),
    }


class OpeningHours(commands.Cog, name="Opening Hours"):
    """Automatic opening-hours scheduling for Modmail."""

    def __init__(self, bot):
        self.bot = bot
        self.db = bot.plugin_db.get_partition(self)
        self._config_cache = None
        self.scheduler_loop.start()

    def cog_unload(self):
        self.scheduler_loop.cancel()

    async def get_config(self):
        if self._config_cache is not None:
            return self._config_cache
        cfg = await self.db.find_one({"_id": "config"})
        if cfg is None:
            cfg = _default_config()
            await self.db.insert_one(cfg)
        else:
            defaults = _default_config()
            changed = False
            for key, value in defaults.items():
                if key not in cfg:
                    cfg[key] = value
                    changed = True
            if "embed" in cfg:
                for key, value in DEFAULT_EMBED.items():
                    if key not in cfg["embed"]:
                        cfg["embed"][key] = value
                        changed = True
            if changed:
                await self.save_config(cfg)
        self._config_cache = cfg
        return cfg

    async def save_config(self, cfg):
        self._config_cache = cfg
        await self.db.find_one_and_update(
            {"_id": "config"}, {"$set": cfg}, upsert=True
        )

    def _tz(self, cfg):
        from zoneinfo import ZoneInfo

        try:
            return ZoneInfo(cfg["timezone"])
        except Exception:
            return ZoneInfo("UTC")

    def is_open_now(self, cfg, now=None):
        if not cfg.get("enabled", True):
            return True
        override = cfg.get("override")
        if override == "open":
            return True
        if override == "closed":
            return False

        tz = self._tz(cfg)
        now = now or datetime.now(tz)
        day_key = DAY_KEYS[now.weekday()]
        day_cfg = cfg["schedule"].get(day_key)
        if not day_cfg or day_cfg.get("closed"):
            return False

        oh, om = map(int, day_cfg["open"].split(":"))
        ch, cm = map(int, day_cfg["close"].split(":"))
        cur = now.time()
        open_t, close_t = dtime(oh, om), dtime(ch, cm)

        if open_t == close_t:
            return True 
        if open_t < close_t:
            return open_t <= cur < close_t
        return cur >= open_t or cur < close_t

    def get_next_transition(self, cfg, now=None):
        """Returns (datetime, will_be_open) for the next state change, or None
        if the schedule never changes (e.g. fully open or fully closed)."""
        tz = self._tz(cfg)
        now = now or datetime.now(tz)
        candidates = []
        for delta in range(0, 9):
            day = now + timedelta(days=delta)
            day_key = DAY_KEYS[day.weekday()]
            day_cfg = cfg["schedule"].get(day_key, {"closed": True})
            if day_cfg.get("closed"):
                continue
            oh, om = map(int, day_cfg["open"].split(":"))
            ch, cm = map(int, day_cfg["close"].split(":"))
            open_dt = day.replace(hour=oh, minute=om, second=0, microsecond=0)
            close_dt = day.replace(hour=ch, minute=cm, second=0, microsecond=0)
            if close_dt <= open_dt:
                close_dt += timedelta(days=1)
            if open_dt > now:
                candidates.append((open_dt, True))
            if close_dt > now:
                candidates.append((close_dt, False))
        if not candidates:
            return None
        candidates.sort(key=lambda c: c[0])
        return candidates[0]

    def _format_dt(self, dt):
        return f"<t:{int(dt.timestamp())}:F> (<t:{int(dt.timestamp())}:R>)"

    def _render_embed_text(self, cfg, text):
        tz = self._tz(cfg)
        now = datetime.now(tz)
        nxt = self.get_next_transition(cfg, now)
        next_open_str = "soon"
        if nxt:
            dt, will_open = nxt
            if will_open:
                next_open_str = self._format_dt(dt)
            else:
                next_open_str = "later — we're already open right now"
        return text.format(
            next_open=next_open_str,
            time=now.strftime("%H:%M"),
            timezone=cfg["timezone"],
            server=self.bot.guild.name if self.bot.guild else "our server",
        )

    def build_status_embed(self, cfg):
        tz = self._tz(cfg)
        now = datetime.now(tz)
        open_now = self.is_open_now(cfg, now)
        nxt = self.get_next_transition(cfg, now)

        embed = discord.Embed(
            title="🟢 Currently Open" if open_now else "🔴 Currently Closed",
            color=discord.Color.green() if open_now else discord.Color.red(),
        )
        embed.add_field(name="Timezone", value=cfg["timezone"], inline=True)
        embed.add_field(
            name="Automatic scheduling",
            value="Enabled" if cfg.get("enabled", True) else "Disabled (always open)",
            inline=True,
        )
        embed.add_field(
            name="Override", value=cfg.get("override") or "None (automatic)", inline=True
        )
        if nxt:
            dt, will_open = nxt
            label = "Opens" if will_open else "Closes"
            embed.add_field(name=f"Next: {label}", value=self._format_dt(dt), inline=False)
        embed.add_field(
            name="Manages dm_disabled",
            value="Yes" if cfg.get("manage_dm_disabled", True) else "No",
            inline=True,
        )
        log_channel = cfg.get("log_channel")
        embed.add_field(
            name="Log channel",
            value=f"<#{log_channel}>" if log_channel else "Not set",
            inline=True,
        )
        embed.set_footer(text="Use ?hours schedule to see the full weekly schedule.")
        return embed

    def build_schedule_embed(self, cfg):
        embed = discord.Embed(title="Weekly Opening Hours", color=self.bot.main_color)
        lines = []
        for d in DAY_KEYS:
            day_cfg = cfg["schedule"][d]
            if day_cfg.get("closed"):
                lines.append(f"**{DAY_NAMES[d]}** — Closed")
            else:
                lines.append(f"**{DAY_NAMES[d]}** — {day_cfg['open']} to {day_cfg['close']}")
        embed.description = "\n".join(lines)
        embed.set_footer(text=f"Timezone: {cfg['timezone']}")
        return embed

    def build_preview_embed(self, cfg):
        embed_cfg = cfg["embed"]
        color = embed_cfg.get("color")
        embed = discord.Embed(
            title=self._render_embed_text(cfg, embed_cfg.get("title", DEFAULT_EMBED["title"])),
            description=self._render_embed_text(
                cfg, embed_cfg.get("description", DEFAULT_EMBED["description"])
            ),
            color=int(color, 16) if color else self.bot.error_color,
        )
        if embed_cfg.get("footer"):
            embed.set_footer(text=self._render_embed_text(cfg, embed_cfg["footer"]))
        if embed_cfg.get("thumbnail"):
            embed.set_thumbnail(url=embed_cfg["thumbnail"])
        return embed

    async def sync_core_config(self, cfg):
        """Push the plugin's embed text into Modmail's own
        disabled_new_thread_title / disabled_new_thread_response keys, which
        is what core Modmail actually shows to users when dm_disabled is set
        to NEW_THREADS."""
        self.bot.config["disabled_new_thread_title"] = self._render_embed_text(
            cfg, cfg["embed"].get("title", DEFAULT_EMBED["title"])
        )
        self.bot.config["disabled_new_thread_response"] = self._render_embed_text(
            cfg, cfg["embed"].get("description", DEFAULT_EMBED["description"])
        )
        color = cfg["embed"].get("color")
        if color:
            self.bot.config["error_color"] = color
        await self.bot.config.update()

    async def apply_state(self, cfg, open_now, log=True):
        if cfg.get("manage_dm_disabled", True):
            current = self.bot.config.get("dm_disabled")
            if current != DMDisabled.ALL_THREADS.value:
                if not open_now:
                    await self.sync_core_config(cfg)
                    self.bot.config["dm_disabled"] = DMDisabled.NEW_THREADS.value
                else:
                    self.bot.config["dm_disabled"] = DMDisabled.NONE.value
                await self.bot.config.update()

        if log:
            log_channel_id = cfg.get("log_channel")
            if log_channel_id:
                channel = self.bot.get_channel(int(log_channel_id))
                if channel:
                    tz = self._tz(cfg)
                    now = datetime.now(tz)
                    nxt = self.get_next_transition(cfg, now)
                    embed = discord.Embed(
                        title="🟢 Modmail is now OPEN" if open_now else "🔴 Modmail is now CLOSED",
                        color=discord.Color.green() if open_now else discord.Color.red(),
                        timestamp=discord.utils.utcnow(),
                    )
                    embed.add_field(name="Timezone", value=cfg["timezone"])
                    if nxt:
                        dt, will_open = nxt
                        label = "Closes" if will_open is False else "Opens"
                        embed.add_field(
                            name=f"Next transition",
                            value=self._format_dt(dt),
                            inline=False,
                        )
                    try:
                        await channel.send(embed=embed)
                    except discord.HTTPException:
                        logger.warning("Opening Hours: failed to send log message.")

    @tasks.loop(seconds=45)
    async def scheduler_loop(self):
        try:
            cfg = await self.get_config()
            if not cfg.get("enabled", True):
                return
            open_now = self.is_open_now(cfg)
            if cfg.get("last_state") is None:
                cfg["last_state"] = open_now
                await self.save_config(cfg)
                await self.apply_state(cfg, open_now, log=False)
                return
            if open_now != cfg["last_state"]:
                cfg["last_state"] = open_now
                await self.save_config(cfg)
                await self.apply_state(cfg, open_now, log=True)
        except Exception:
            logger.error("Opening Hours: error in scheduler loop.", exc_info=True)

    @scheduler_loop.before_loop
    async def before_scheduler_loop(self):
        await self.bot.wait_until_ready()

  
    @commands.group(invoke_without_command=True, aliases=["hour", "openinghours"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def hours(self, ctx):
        """Opening hours system. Run a subcommand to configure it."""
        cfg = await self.get_config()
        embed = self.build_status_embed(cfg)
        embed.add_field(
            name="Commands",
            value=(
                "`?hours status` · `?hours schedule` · `?hours set <day> <open> <close>`\n"
                "`?hours closed <day>` · `?hours open <day>` · `?hours timezone <tz>`\n"
                "`?hours logchannel <#channel>` · `?hours toggle` · `?hours managedm <on|off>`\n"
                "`?hours override <open|closed|auto>`\n"
                "`?hours embed title|description|color|reset|preview`"
            ),
            inline=False,
        )
        await ctx.send(embed=embed)

    @hours.command(name="status")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def hours_status(self, ctx):
        """Show detailed open/closed status."""
        cfg = await self.get_config()
        await ctx.send(embed=self.build_status_embed(cfg))

    @hours.command(name="schedule")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def hours_schedule(self, ctx):
        """View the weekly opening-hours schedule."""
        cfg = await self.get_config()
        await ctx.send(embed=self.build_schedule_embed(cfg))

    @hours.command(name="set")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def hours_set(self, ctx, day: str, open_time: str, close_time: str):
        """Set opening hours for a day. Use `all` to apply to every day.

        Example: `?hours set mon 09:00 17:00`
        Overnight ranges are supported, e.g. `?hours set fri 22:00 06:00`.
        """
        day = day.lower()
        if day not in DAY_ALIASES:
            return await ctx.send(
                embed=discord.Embed(
                    description=f"`{day}` is not a valid day. Use a day name or `all`.",
                    color=self.bot.error_color,
                )
            )
        if not TIME_RE.match(open_time) or not TIME_RE.match(close_time):
            return await ctx.send(
                embed=discord.Embed(
                    description="Times must be in 24-hour `HH:MM` format, e.g. `09:00`.",
                    color=self.bot.error_color,
                )
            )

        cfg = await self.get_config()
        target = DAY_ALIASES[day]
        days = DAY_KEYS if target == "all" else [target]
        for d in days:
            cfg["schedule"][d] = {"open": open_time, "close": close_time, "closed": False}
        await self.save_config(cfg)

        await ctx.send(
            embed=discord.Embed(
                description=f"Updated hours for **{'every day' if target == 'all' else DAY_NAMES[target]}**: "
                f"{open_time} - {close_time}.",
                color=self.bot.main_color,
            )
        )

    @hours.command(name="closed")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def hours_closed(self, ctx, day: str):
        """Mark a day as fully closed."""
        day = day.lower()
        if day not in DAY_ALIASES or DAY_ALIASES[day] == "all":
            return await ctx.send(
                embed=discord.Embed(
                    description=f"`{day}` is not a valid single day.", color=self.bot.error_color
                )
            )
        cfg = await self.get_config()
        d = DAY_ALIASES[day]
        cfg["schedule"][d]["closed"] = True
        await self.save_config(cfg)
        await ctx.send(
            embed=discord.Embed(
                description=f"**{DAY_NAMES[d]}** is now marked as fully closed.",
                color=self.bot.main_color,
            )
        )

    @hours.command(name="open")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def hours_open_day(self, ctx, day: str):
        """Unmark a day as closed (it will use its last saved open/close times)."""
        day = day.lower()
        if day not in DAY_ALIASES or DAY_ALIASES[day] == "all":
            return await ctx.send(
                embed=discord.Embed(
                    description=f"`{day}` is not a valid single day.", color=self.bot.error_color
                )
            )
        cfg = await self.get_config()
        d = DAY_ALIASES[day]
        cfg["schedule"][d]["closed"] = False
        await self.save_config(cfg)
        await ctx.send(
            embed=discord.Embed(
                description=f"**{DAY_NAMES[d]}** is no longer marked as closed "
                f"({cfg['schedule'][d]['open']} - {cfg['schedule'][d]['close']}).",
                color=self.bot.main_color,
            )
        )

    @hours.command(name="timezone", aliases=["tz"])
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def hours_timezone(self, ctx, tz_name: str):
        """Set the timezone used for the schedule, e.g. `Europe/London`, `America/New_York`."""
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            return await ctx.send(
                embed=discord.Embed(
                    description=f"`{tz_name}` is not a recognised IANA timezone name. "
                    "Examples: `UTC`, `Europe/London`, `America/New_York`, `Asia/Tokyo`.",
                    color=self.bot.error_color,
                )
            )
        cfg = await self.get_config()
        cfg["timezone"] = tz_name
        await self.save_config(cfg)
        await ctx.send(
            embed=discord.Embed(
                description=f"Timezone set to **{tz_name}**.", color=self.bot.main_color
            )
        )

    @hours.command(name="logchannel")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def hours_logchannel(self, ctx, channel: discord.TextChannel = None):
        """Set (or clear, if no channel given) the channel used to log open/close events."""
        cfg = await self.get_config()
        cfg["log_channel"] = channel.id if channel else None
        await self.save_config(cfg)
        if channel:
            await ctx.send(
                embed=discord.Embed(
                    description=f"Open/close events will now be logged in {channel.mention}.",
                    color=self.bot.main_color,
                )
            )
        else:
            await ctx.send(
                embed=discord.Embed(description="Log channel cleared.", color=self.bot.main_color)
            )

    @hours.command(name="toggle")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def hours_toggle(self, ctx):
        """Enable or disable automatic scheduling. When disabled, the bot is always open."""
        cfg = await self.get_config()
        cfg["enabled"] = not cfg.get("enabled", True)
        await self.save_config(cfg)
        if not cfg["enabled"]:
            # Immediately make sure we're not stuck closed.
            await self.apply_state(cfg, True, log=False)
        await ctx.send(
            embed=discord.Embed(
                description=f"Automatic scheduling is now **{'enabled' if cfg['enabled'] else 'disabled'}**.",
                color=self.bot.main_color,
            )
        )

    @hours.command(name="managedm")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def hours_managedm(self, ctx, state: str):
        """Whether this plugin is allowed to control Modmail's dm_disabled setting. `on` or `off`."""
        state = state.lower()
        if state not in ("on", "off"):
            return await ctx.send(
                embed=discord.Embed(description="Use `on` or `off`.", color=self.bot.error_color)
            )
        cfg = await self.get_config()
        cfg["manage_dm_disabled"] = state == "on"
        await self.save_config(cfg)
        await ctx.send(
            embed=discord.Embed(
                description=f"Managing `dm_disabled` automatically is now **{state}**.",
                color=self.bot.main_color,
            )
        )

    @hours.command(name="override")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def hours_override(self, ctx, mode: str):
        """Manually force the state: `open`, `closed`, or `auto` to go back to the schedule."""
        mode = mode.lower()
        if mode not in ("open", "closed", "auto"):
            return await ctx.send(
                embed=discord.Embed(
                    description="Use `open`, `closed`, or `auto`.", color=self.bot.error_color
                )
            )
        cfg = await self.get_config()
        cfg["override"] = None if mode == "auto" else mode
        await self.save_config(cfg)

        open_now = self.is_open_now(cfg)
        cfg["last_state"] = open_now
        await self.save_config(cfg)
        await self.apply_state(cfg, open_now, log=True)

        await ctx.send(
            embed=discord.Embed(
                description=f"Override set to **{mode}**. The bot is now "
                f"**{'open' if open_now else 'closed'}**.",
                color=self.bot.main_color,
            )
        )

    @hours.group(name="embed", invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def hours_embed(self, ctx):
        """Customise the embed users see when messaging during closed hours."""
        await ctx.send_help(ctx.command)

    @hours_embed.command(name="title")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def hours_embed_title(self, ctx, *, text: str):
        """Set the title of the closed-hours embed."""
        cfg = await self.get_config()
        cfg["embed"]["title"] = text
        await self.save_config(cfg)
        if not self.is_open_now(cfg):
            await self.sync_core_config(cfg)
        await ctx.send(
            embed=discord.Embed(description="Embed title updated.", color=self.bot.main_color)
        )

    @hours_embed.command(name="description", aliases=["desc"])
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def hours_embed_description(self, ctx, *, text: str):
        """Set the description of the closed-hours embed.

        Placeholders: {next_open}, {time}, {timezone}, {server}
        """
        cfg = await self.get_config()
        cfg["embed"]["description"] = text
        await self.save_config(cfg)
        if not self.is_open_now(cfg):
            await self.sync_core_config(cfg)
        await ctx.send(
            embed=discord.Embed(
                description="Embed description updated.", color=self.bot.main_color
            )
        )

    @hours_embed.command(name="footer")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def hours_embed_footer(self, ctx, *, text: str = None):
        """Set (or clear) the footer of the closed-hours embed."""
        cfg = await self.get_config()
        cfg["embed"]["footer"] = text
        await self.save_config(cfg)
        await ctx.send(
            embed=discord.Embed(description="Embed footer updated.", color=self.bot.main_color)
        )

    @hours_embed.command(name="thumbnail")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def hours_embed_thumbnail(self, ctx, url: str = None):
        """Set (or clear) the thumbnail image of the closed-hours embed."""
        cfg = await self.get_config()
        cfg["embed"]["thumbnail"] = url
        await self.save_config(cfg)
        await ctx.send(
            embed=discord.Embed(description="Embed thumbnail updated.", color=self.bot.main_color)
        )

    @hours_embed.command(name="color", aliases=["colour"])
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def hours_embed_color(self, ctx, hex_color: str):
        """Set the embed colour, e.g. `?hours embed color #E74C3C`.

        Note: this also sets Modmail's global `error_color`, since that is
        what core Modmail uses to colour the closed-hours message.
        """
        if not HEX_RE.match(hex_color):
            return await ctx.send(
                embed=discord.Embed(
                    description="Please provide a valid hex colour, e.g. `#E74C3C`.",
                    color=self.bot.error_color,
                )
            )
        clean = hex_color.lstrip("#")
        cfg = await self.get_config()
        cfg["embed"]["color"] = clean
        await self.save_config(cfg)
        if not self.is_open_now(cfg):
            await self.sync_core_config(cfg)
        await ctx.send(
            embed=discord.Embed(
                description=f"Embed colour set to `#{clean}`.", color=int(clean, 16)
            )
        )

    @hours_embed.command(name="reset")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def hours_embed_reset(self, ctx):
        """Reset the closed-hours embed to its default text and colour."""
        cfg = await self.get_config()
        cfg["embed"] = dict(DEFAULT_EMBED)
        await self.save_config(cfg)
        if not self.is_open_now(cfg):
            await self.sync_core_config(cfg)
        await ctx.send(
            embed=discord.Embed(
                description="Closed-hours embed reset to default.", color=self.bot.main_color
            )
        )

    @hours_embed.command(name="preview")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def hours_embed_preview(self, ctx):
        """Preview the embed users see when they DM during closed hours."""
        cfg = await self.get_config()
        await ctx.send(
            content="This is what users see when they message during closed hours:",
            embed=self.build_preview_embed(cfg),
        )


async def setup(bot):
    await bot.add_cog(OpeningHours(bot))
