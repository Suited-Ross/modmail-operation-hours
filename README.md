# Opening Hours — Modmail Plugin

Adds an automatic opening-hours schedule to your [Modmail](https://github.com/modmail-dev/Modmail) bot.

**What it does:**
- Set a weekly schedule (per day, supports overnight ranges and fully-closed days, in any timezone).
- Every ~45 seconds it checks the schedule and automatically flips the bot between "open" and "closed".
- **While closed**, anyone who DMs the bot for the *first* time gets a nice, fully-customisable embed
  telling them the bot is closed and when it'll be back — no new thread is created. Existing open
  threads are completely unaffected, so staff can keep replying to ongoing conversations.
- **While open**, everything works exactly as normal.
- Every open → closed and closed → open transition is logged to a channel of your choice.
- A manual override (`?hours override open|closed|auto`) lets staff force a state regardless of the clock.

It works by reusing Modmail's own built-in `dm_disabled` / `disabled_new_thread_title` /
`disabled_new_thread_response` settings under the hood — the same mechanism Modmail already uses
for manually disabling new threads — so blocking is 100% reliable and uses code that's already
battle-tested in core Modmail. The plugin just automates it and gives you dedicated `?hours` commands
to configure it, on top of the schedule/logging itself.

---

## Commands

All commands are under `?hours` (assuming your prefix is `?`).

| Command | Permission | Description |
|---|---|---|
| `?hours` | Supporter | Quick status + command list |
| `?hours status` | Supporter | Detailed open/closed status |
| `?hours schedule` | Supporter | View the weekly schedule |
| `?hours set <day> <open> <close>` | Admin | Set hours for a day, or `all` for every day. e.g. `?hours set mon 09:00 17:00` |
| `?hours closed <day>` | Admin | Mark a day fully closed, e.g. `?hours closed sun` |
| `?hours open <day>` | Admin | Un-mark a day as closed |
| `?hours timezone <tz>` | Admin | Set the IANA timezone, e.g. `?hours timezone Europe/London` |
| `?hours logchannel <#channel>` | Admin | Channel where open/close events are logged |
| `?hours toggle` | Admin | Enable/disable automatic scheduling (disabled = always open) |
| `?hours managedm <on\|off>` | Admin | Whether the plugin is allowed to control `dm_disabled` |
| `?hours override <open\|closed\|auto>` | Admin | Force a state, or return to the automatic schedule |
| `?hours embed title <text>` | Admin | Set the closed-hours embed title |
| `?hours embed description <text>` | Admin | Set the closed-hours embed description. Placeholders: `{next_open}`, `{time}`, `{timezone}`, `{server}` |
| `?hours embed color <hex>` | Admin | Set the embed colour, e.g. `#E74C3C` |
| `?hours embed footer <text>` | Admin | Set an embed footer |
| `?hours embed thumbnail <url>` | Admin | Set an embed thumbnail image |
| `?hours embed reset` | Admin | Reset the embed to defaults |
| `?hours embed preview` | Supporter | Preview what users will see |

**Overnight ranges** work fine: `?hours set fri 22:00 06:00` means open Friday 22:00 through
Saturday 06:00.

**Note on colour:** Modmail colours the "new thread blocked" embed using its own global
`error_color` setting, so `?hours embed color` also updates that value. Everything else
(title, description, footer, thumbnail) is scoped to this plugin only.

### Example setup

```
?hours set all 09:00 17:00
?hours closed sat
?hours closed sun
?hours timezone Europe/London
?hours logchannel #bot-logs
?hours embed title We're closed right now!
?hours embed description Thanks for your message! Our team is offline until **{next_open}** (times shown in {timezone}). We'll get back to you as soon as we're open.
?hours embed color #E74C3C
```

---

## Installation

### Option A — Install from a GitHub repository (recommended)

Modmail plugins are installed straight from a public GitHub repo, so you'll host this small folder
in your own repository (a fork, or a brand-new repo — either works).

1. **Create a repository** on GitHub (e.g. `yourname/modmail-plugins`), or use an existing one.
2. **Copy this plugin's folder** into that repository so the layout looks like this:
   ```
   your-repo/
   └── openhours/
       ├── openhours.py
       └── requirements.txt
   ```
   The folder name (`openhours`) must match the plugin name — don't rename it.
3. **Commit and push** the folder to your repo's default branch (usually `main` or `master`).
4. In your Discord server, in any channel the bot can see, run:
   ```
   ?plugin add yourname/modmail-plugins/openhours
   ```
   Replace `yourname/modmail-plugins` with your actual `<github_user>/<repo>`. If your default
   branch isn't `master`, add it explicitly: `?plugin add yourname/modmail-plugins/openhours@main`.
5. Modmail will download, install dependencies, and load the plugin automatically. You should see
   a confirmation message. Run `?plugin loaded` to double check it's active.
6. Set up your schedule (see the example above), then send yourself a test DM from another account
   during "closed" hours to confirm the embed appears, and check your log channel for the
   open/closed transition message.

To update later, just push changes to the same repo and run:
```
?plugin update openhours
```

### Option B — Local install (self-hosted bots only)

If you're running Modmail yourself (VPS, Docker, etc.) and don't want to publish it to GitHub:

1. In your Modmail bot's directory, go to the `plugins` folder and create (if it doesn't exist)
   an `@local` folder.
2. Copy the `openhours` folder into it, so you have:
   ```
   plugins/@local/openhours/openhours.py
   plugins/@local/openhours/requirements.txt
   ```
3. Install the one dependency manually (or let your process manager's requirements install pick
   it up): `pip install tzdata`
4. In Discord, run:
   ```
   ?plugin add local/openhours
   ```
5. Restart the bot if it doesn't load immediately, then confirm with `?plugin loaded`.

---

## Permissions

Configuration commands require **Administrator**-level Modmail permission; status/preview commands
only require **Supporter**-level. Adjust these with Modmail's normal `?permissions` commands if you
want to change who can run what, e.g.:
```
?permissions add level SUPPORTER hours status
```

## Notes / things worth knowing

- The schedule check runs every 45 seconds, so a transition may be up to that long after the exact
  scheduled time — fine for real-world "office hours" use, not meant for split-second precision.
- If a staff member manually sets `dm_disabled` to `ALL_THREADS` (blocking *all* DMs, not just new
  ones) via Modmail's core config, this plugin will **not** override that — it only manages the
  `NONE` ↔ `NEW_THREADS` states, so a full manual lockdown always takes priority.
- Turn off automatic `dm_disabled` management entirely with `?hours managedm off` if you just want
  the schedule + logging, and want to control blocking yourself.
- All settings are stored in your bot's database (via Modmail's plugin database partition), so they
  survive restarts and redeploys.
