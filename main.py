import discord
import aiohttp
import asyncio
import urllib.parse
import json
import os
import re
import random
import time
from collections import deque, Counter
from datetime import datetime, timezone, timedelta
from io import BytesIO
from dotenv import load_dotenv
import imagehash
from PIL import Image

load_dotenv()

TOKEN = os.getenv("TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
CONFIG_FILE = "config.json"
API_URL = "https://doitenroi.win/api/bypass?url="
REFERENCE_DIR = "reference_images"
HASH_THRESHOLD = 10
PREFIX = "."

if not TOKEN:
    raise ValueError("Missing TOKEN in .env")

last_deleted = {}


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {}


def save_config(data):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)


def load_reference_hashes():
    hashes = []
    if not os.path.exists(REFERENCE_DIR):
        os.makedirs(REFERENCE_DIR)
        return hashes
    for filename in os.listdir(REFERENCE_DIR):
        if filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            path = os.path.join(REFERENCE_DIR, filename)
            try:
                img = Image.open(path)
                hashes.append(imagehash.phash(img))
            except Exception as e:
                print(f"Failed to load reference image {filename}: {e}")
    print(f"Loaded {len(hashes)} reference image(s)")
    return hashes


def parse_duration(s: str) -> int:
    total = 0
    matches = re.findall(r'(\d+)([smhd])', s.lower())
    for val, unit in matches:
        val = int(val)
        if unit == 's':
            total += val
        elif unit == 'm':
            total += val * 60
        elif unit == 'h':
            total += val * 3600
        elif unit == 'd':
            total += val * 86400
    return total


config = load_config()
reference_hashes = load_reference_hashes()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(bot)


async def check_scam_image(attachment: discord.Attachment) -> bool:
    if not attachment.content_type or not attachment.content_type.startswith("image/"):
        return False
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(attachment.url) as res:
                data = await res.read()
        img = Image.open(BytesIO(data))
        img_hash = imagehash.phash(img)
        for ref_hash in reference_hashes:
            if abs(img_hash - ref_hash) <= HASH_THRESHOLD:
                return True
    except Exception as e:
        print(f"Image check error: {e}")
    return False


async def delete_after(msg: discord.Message, seconds: int):
    await asyncio.sleep(seconds)
    try:
        await msg.delete()
    except Exception:
        pass


async def end_giveaway(msg_id: str, gw: dict):
    channel = bot.get_channel(gw["channel_id"])
    if not channel:
        return
    try:
        message = await channel.fetch_message(int(msg_id))
    except Exception:
        return
    entries = gw["entries"]
    winner_count = gw["winner_count"]
    if not entries:
        winners = []
        winner_text = "No entries — no winner."
    else:
        picked = random.sample(entries, min(winner_count, len(entries)))
        winners = [str(u) for u in picked]
        winner_text = " ".join(f"<@{uid}>" for uid in picked)
    gw["ended"] = True
    gw["winners"] = winners
    save_config(config)
    await message.edit(view=GiveawayEndedView(gw["prize"], winner_count, len(entries)))
    await channel.send(
        f"🎉 Giveaway ended! **{gw['prize']}**\nWinner(s): {winner_text}",
        reference=message
    )
    await log_bot(
        channel.guild,
        "🎉 Giveaway Ended",
        f"**Prize:** {gw['prize']}\n**Channel:** {channel.mention}\n**Entries:** {len(entries)}\n**Winner(s):** {winner_text}",
        discord.Color.gold()
    )


async def giveaway_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = int(datetime.now(timezone.utc).timestamp())
        giveaways = config.get("giveaways", {})
        for msg_id, gw in list(giveaways.items()):
            if gw.get("ended"):
                continue
            if now >= gw["end_timestamp"]:
                await end_giveaway(msg_id, gw)
        await asyncio.sleep(15)


async def do_bypass(url: str, reply_msg):
    try:
        encoded = urllib.parse.quote(url, safe="")
        timeout_cfg = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
            async with session.get(API_URL + encoded) as res:
                data = await res.json(content_type=None)
                status = data.get("status")
                if status == "success":
                    await reply_msg.edit(view=SuccessView(data.get("result")))
                elif status in ("failed", "error"):
                    await reply_msg.edit(view=FailedView(data.get("message", "Failed to bypass")))
                else:
                    await reply_msg.edit(view=FailedView("Invalid API response."))
    except asyncio.TimeoutError:
        await reply_msg.edit(view=FailedView("API request timed out."))
    except Exception as e:
        print("Error:", e)
        await reply_msg.edit(view=FailedView(str(e)))


class CopyButton(discord.ui.Button):
    def __init__(self, result):
        super().__init__(label="Mobile Copy", style=discord.ButtonStyle.secondary)
        self.result = result

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(self.result, ephemeral=True)


class VerifyButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Verify",
            style=discord.ButtonStyle.success,
            emoji=discord.PartialEmoji(name="1000044834", id=1553216869094400020),
            custom_id="verify_button"
        )

    async def callback(self, interaction: discord.Interaction):
        role_id = config.get("verify_role_id")
        if not role_id:
            await interaction.response.send_message(view=VerifyEphemeralView("error", "Verification role not configured."), ephemeral=True)
            return
        role = interaction.guild.get_role(role_id)
        if not role:
            await interaction.response.send_message(view=VerifyEphemeralView("error", "Verification role not found."), ephemeral=True)
            return
        if role in interaction.user.roles:
            await interaction.response.send_message(view=VerifyEphemeralView("already", "You are already verified."), ephemeral=True)
            return
        await interaction.user.add_roles(role)
        await interaction.response.send_message(view=VerifyEphemeralView("success", "You now have access to the server. Welcome!"), ephemeral=True)
        log_channel_id = config.get("verify_log_id")
        if log_channel_id:
            log_channel = interaction.guild.get_channel(log_channel_id)
            if log_channel:
                now = datetime.now(timezone.utc)
                created = interaction.user.created_at
                account_age = (now - created).days
                embed = discord.Embed(title="Member Verified", color=discord.Color.green(), timestamp=now)
                embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
                embed.set_thumbnail(url=interaction.user.display_avatar.url)
                embed.add_field(name="User", value=interaction.user.mention, inline=True)
                embed.add_field(name="ID", value=f"`{interaction.user.id}`", inline=True)
                embed.add_field(name="Role Given", value=role.mention, inline=True)
                embed.add_field(name="Account Age", value=f"`{account_age}` days", inline=True)
                embed.add_field(name="Created", value=f"<t:{int(created.timestamp())}:R>", inline=True)
                await log_channel.send(embed=embed)


class GiveawayEnterButton(discord.ui.Button):
    def __init__(self, count: int = 0):
        super().__init__(label=f"🎉 Enter ({count})", style=discord.ButtonStyle.primary, custom_id="giveaway_enter")

    async def callback(self, interaction: discord.Interaction):
        msg_id = str(interaction.message.id)
        giveaways = config.get("giveaways", {})
        if msg_id not in giveaways:
            await interaction.response.send_message("This giveaway no longer exists.", ephemeral=True)
            return
        gw = giveaways[msg_id]
        if gw.get("ended"):
            await interaction.response.send_message("This giveaway has already ended.", ephemeral=True)
            return
        uid = str(interaction.user.id)
        if uid in gw["entries"]:
            gw["entries"].remove(uid)
            save_config(config)
            count = len(gw["entries"])
            await interaction.message.edit(view=GiveawayActiveView(gw["prize"], gw["winner_count"], gw["end_timestamp"], count))
            await interaction.response.send_message("You left the giveaway.", ephemeral=True)
        else:
            gw["entries"].append(uid)
            save_config(config)
            count = len(gw["entries"])
            await interaction.message.edit(view=GiveawayActiveView(gw["prize"], gw["winner_count"], gw["end_timestamp"], count))
            await interaction.response.send_message("You entered the giveaway! 🎉", ephemeral=True)


class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(VerifyButton())


class GiveawayPersistentView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(GiveawayEnterButton())


HELP_PAGES = [
    (
        "Utility",
        "`.ping`\n"
        "`.say <message>`\n"
        "`.embed`\n"
        "`.afk [reason]`\n"
        "`.snipe`\n"
        "`.avatar [@user]`\n"
        "`.bypass <link>`\n"
        "`.steal <emoji>`\n"
        "`.help`"
    ),
    (
        "Moderation",
        "**Moderation**\n"
        "`.kick @user [reason]`\n"
        "`.ban @user [reason]`\n"
        "`.unban <userID>`\n"
        "`.timeout @user <minutes> [reason]`\n"
        "`.untimeout @user`\n"
        "`.purge <amount>`\n\n"
        "**Warnings**\n"
        "`.warn @user [reason]`\n"
        "`.warns [@user]`\n"
        "`.clearwarns @user`\n\n"
        "**Channels**\n"
        "`.lock [#channel]`\n"
        "`.unlock [#channel]`\n"
        "`.slowmode <seconds> [#channel]`"
    ),
    (
        "Economy",
        "`.balance [@user]` `.daily` `.weekly` `.work` `.beg` `.crime`\n"
        "`.rob @user` `.fish` `.mine` `.coinflip <bet> <h/t>` `.scatter <bet>`\n"
        "`.deposit <amt|all>` `.withdraw <amt|all>` `.give @user <amt>`\n"
        "`.shop` `.buy <item> [qty]` `.sell <item> [qty]` `.inventory`\n"
        "`.leaderboard`\n\n"
        "**Admin**\n"
        "`.addcoins` `.removecoins` `.setcoins` `.resetcoins`"
    ),
    (
        "Security & Logs",
        "**Security (Admin)**\n"
        "`.security` `.security setup [#logchannel]` `.panic <on|off>`\n"
        "`.lockdown` `.unlockdown`\n"
        "`.antiraid` `.antinuke` `.antispam` `.antilink` `.antimention` `<on|off>`\n"
        "`.whitelist <add|remove|list> [@user]` (owner only)\n"
        "Fine-tune with `/security <module>`\n\n"
        "**Logs (Admin)**\n"
        "`.logs` `.setlog <category|all> #channel` `.dellog <category|all>`\n"
        "Categories: `mod` `message` `member` `voice` `server` `bot` `security`"
    ),
    (
        "Giveaway, Ticket & Auto Responder",
        "**Giveaways**\n"
        "`.gstart <duration> <winners> <prize>`\n"
        "`.gend <message_id>` `.greroll <message_id>`\n\n"
        "**Tickets**\n"
        "`/ticket setup` `/ticket add` `/ticket remove` `/ticket rename` `/ticket close`\n\n"
        "**Auto Responder**\n"
        "`/autoresponder add|edit|remove|list|clear`\n"
        "`.ar add <trigger> | <reply>` `.ar add <trigger>` `.ar list`\n"
        "Variables: `{user}` `{username}` `{server}` `{channel}` `{membercount}`"
    ),
]


class HelpNavButton(discord.ui.Button):
    def __init__(self, label: str, target: int, view_ref, disabled: bool = False):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, disabled=disabled)
        self.target = target
        self.view_ref = view_ref

    async def callback(self, interaction: discord.Interaction):
        owner = self.view_ref.author_id
        if owner and interaction.user.id != owner:
            await interaction.response.send_message("Only the person who ran this command can use these buttons.", ephemeral=True)
            return
        await interaction.response.edit_message(view=HelpView(owner, self.target))


class HelpView(discord.ui.LayoutView):
    def __init__(self, author_id: int = None, page: int = 0):
        super().__init__(timeout=300)
        self.author_id = author_id
        self.page = max(0, min(page, len(HELP_PAGES) - 1))
        total = len(HELP_PAGES)
        name, body = HELP_PAGES[self.page]
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(f"# Help — {name}"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(body),
                discord.ui.Separator(),
                discord.ui.TextDisplay(f"-# Prefix: `.` | Also available as `/command` | Page {self.page + 1}/{total}"),
            )
        )
        indicator = discord.ui.Button(label=f"{self.page + 1} / {total}", style=discord.ButtonStyle.primary, disabled=True)
        self.add_item(
            discord.ui.ActionRow(
                HelpNavButton("<< Back", self.page - 1, self, disabled=self.page == 0),
                indicator,
                HelpNavButton("Next >>", self.page + 1, self, disabled=self.page >= total - 1),
            )
        )


class AfkSetView(discord.ui.LayoutView):
    def __init__(self, member, reason: str):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# 😴 You are now AFK"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"**User:** {member.mention}\n"
                    f"**Reason:** {reason}"
                ),
                accent_color=discord.Color.yellow()
            )
        )


class AfkReturnView(discord.ui.LayoutView):
    def __init__(self, member):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(f"# 👋 Welcome back, {member.display_name}!"),
                discord.ui.Separator(),
                discord.ui.TextDisplay("Your AFK status has been removed."),
                accent_color=discord.Color.green()
            )
        )


class AfkMentionView(discord.ui.LayoutView):
    def __init__(self, member, reason: str, timestamp: int):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(f"# 😴 {member.display_name} is AFK"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"**Reason:** {reason}\n"
                    f"**Since:** <t:{timestamp}:R>"
                ),
                accent_color=discord.Color.yellow()
            )
        )


class SnipeView(discord.ui.LayoutView):
    def __init__(self, data: dict):
        super().__init__(timeout=None)
        content = data["content"] if data.get("content") else "(no text content)"
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# 🔍 Sniped Message"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"**Author:** {data['author_name']}\n"
                    f"**Sent:** <t:{data['timestamp']}:R>\n\n"
                    f"{content}"
                ),
                accent_color=discord.Color.blurple()
            )
        )


class SnipeEmptyView(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# 🔍 Snipe"),
                discord.ui.Separator(),
                discord.ui.TextDisplay("No recently deleted messages in this channel."),
                accent_color=discord.Color.red()
            )
        )


class VerifyEphemeralView(discord.ui.LayoutView):
    def __init__(self, state: str, message: str):
        super().__init__(timeout=None)
        if state == "success":
            self.add_item(
                discord.ui.Container(
                    discord.ui.TextDisplay("# ✅ You have been verified!"),
                    discord.ui.Separator(),
                    discord.ui.TextDisplay(message),
                    accent_color=discord.Color.green()
                )
            )
        else:
            self.add_item(
                discord.ui.Container(
                    discord.ui.TextDisplay(f"# {message}"),
                    accent_color=discord.Color.red()
                )
            )


class VerifyLayoutView(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# Verify to access this server"),
                discord.ui.Separator(),
                discord.ui.TextDisplay("Click the button below to verify and gain access to the server."),
                discord.ui.ActionRow(VerifyButton())
            )
        )


class UserKickedView(discord.ui.LayoutView):
    def __init__(self, user: discord.Member):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# User Kicked"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"{user.mention} has been kicked\n"
                    f"Reason: MrBeast scam auto detected"
                ),
                accent_color=discord.Color.red()
            )
        )


class ProcessingView(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# Processing..."),
                discord.ui.Separator(),
                discord.ui.TextDisplay("Please wait while I bypass the link."),
                accent_color=discord.Color.yellow()
            )
        )


class SuccessView(discord.ui.LayoutView):
    def __init__(self, result):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# Success!"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(f"```\n{result}\n```"),
                discord.ui.Separator(),
                discord.ui.ActionRow(CopyButton(result)),
                accent_color=discord.Color.green()
            )
        )


class FailedView(discord.ui.LayoutView):
    def __init__(self, message):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# Failed!"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(message),
                accent_color=discord.Color.red()
            )
        )


class PingView(discord.ui.LayoutView):
    def __init__(self, latency):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# Pong!"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(f"Latency: **{latency}ms**"),
                accent_color=discord.Color.blurple()
            )
        )


class SetupView(discord.ui.LayoutView):
    def __init__(self, channel: discord.TextChannel):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# Auto Bypass Setup"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"Auto bypass channel set to {channel.mention}\n"
                    f"Links sent in that channel will be bypassed automatically."
                ),
                accent_color=discord.Color.green()
            )
        )


class SayView(discord.ui.LayoutView):
    def __init__(self, channel: discord.TextChannel, content: str):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# Message Sent"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(f"Sent to {channel.mention}:\n> {content}"),
                accent_color=discord.Color.green()
            )
        )


class KickResultView(discord.ui.LayoutView):
    def __init__(self, member: discord.Member, reason: str):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# Member Kicked"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"**User:** {member.mention} (`{member.id}`)\n"
                    f"**Reason:** {reason}"
                ),
                accent_color=discord.Color.orange()
            )
        )


class BanResultView(discord.ui.LayoutView):
    def __init__(self, member, reason: str):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# Member Banned"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"**User:** {member.mention} (`{member.id}`)\n"
                    f"**Reason:** {reason}"
                ),
                accent_color=discord.Color.red()
            )
        )


class UnbanResultView(discord.ui.LayoutView):
    def __init__(self, user_id: int):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# Member Unbanned"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(f"**User ID:** `{user_id}` has been unbanned."),
                accent_color=discord.Color.green()
            )
        )


class PurgeResultView(discord.ui.LayoutView):
    def __init__(self, count: int, channel: discord.TextChannel):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# Messages Purged"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(f"Deleted **{count}** message(s) from {channel.mention}"),
                accent_color=discord.Color.blurple()
            )
        )


class TimeoutResultView(discord.ui.LayoutView):
    def __init__(self, member: discord.Member, minutes: int, reason: str):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# Member Timed Out"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"**User:** {member.mention} (`{member.id}`)\n"
                    f"**Duration:** {minutes} minute(s)\n"
                    f"**Reason:** {reason}"
                ),
                accent_color=discord.Color.yellow()
            )
        )


class UntimeoutResultView(discord.ui.LayoutView):
    def __init__(self, member: discord.Member):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# Timeout Removed"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"**User:** {member.mention} (`{member.id}`) timeout has been removed."
                ),
                accent_color=discord.Color.green()
            )
        )


class WarnAddedView(discord.ui.LayoutView):
    def __init__(self, member: discord.Member, reason: str, total: int):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# Warning Issued"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"**User:** {member.mention} (`{member.id}`)\n"
                    f"**Reason:** {reason}\n"
                    f"**Total Warns:** {total}"
                ),
                accent_color=discord.Color.yellow()
            )
        )


class WarnsListView(discord.ui.LayoutView):
    def __init__(self, member: discord.Member, warns: list):
        super().__init__(timeout=None)
        if not warns:
            body = "No warnings on record."
        else:
            lines = []
            for i, w in enumerate(warns, 1):
                lines.append(f"**{i}.** {w['reason']} — <t:{w['timestamp']}:R> by <@{w['moderator_id']}>")
            body = "\n".join(lines)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(f"# Warnings for {member.display_name}"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(body),
                accent_color=discord.Color.orange()
            )
        )


class WarnsCleared(discord.ui.LayoutView):
    def __init__(self, member: discord.Member):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# Warnings Cleared"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(f"All warnings for {member.mention} have been removed."),
                accent_color=discord.Color.green()
            )
        )


class SlowmodeView(discord.ui.LayoutView):
    def __init__(self, channel: discord.TextChannel, seconds: int):
        super().__init__(timeout=None)
        msg = f"Slowmode disabled in {channel.mention}" if seconds == 0 else f"Slowmode set to **{seconds}s** in {channel.mention}"
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# Slowmode Updated"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(msg),
                accent_color=discord.Color.blurple()
            )
        )


class LockView(discord.ui.LayoutView):
    def __init__(self, channel: discord.TextChannel, locked: bool):
        super().__init__(timeout=None)
        title = "# Channel Locked" if locked else "# Channel Unlocked"
        msg = f"{channel.mention} has been {'locked' if locked else 'unlocked'}."
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(title),
                discord.ui.Separator(),
                discord.ui.TextDisplay(msg),
                accent_color=discord.Color.red() if locked else discord.Color.green()
            )
        )


class GiveawayActiveView(discord.ui.LayoutView):
    def __init__(self, prize: str, winner_count: int, end_timestamp: int, entry_count: int = 0):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# 🎉 GIVEAWAY"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"**Prize:** {prize}\n"
                    f"**Winners:** {winner_count}\n"
                    f"**Ends:** <t:{end_timestamp}:R>\n"
                    f"**Entries:** {entry_count}"
                ),
                discord.ui.Separator(),
                discord.ui.ActionRow(GiveawayEnterButton(entry_count)),
                accent_color=discord.Color.gold()
            )
        )


class GiveawayEndedView(discord.ui.LayoutView):
    def __init__(self, prize: str, winner_count: int, entry_count: int):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# 🎉 GIVEAWAY ENDED"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"**Prize:** {prize}\n"
                    f"**Winners:** {winner_count}\n"
                    f"**Total Entries:** {entry_count}"
                ),
                accent_color=discord.Color.dark_grey()
            )
        )


class GiveawayStartedView(discord.ui.LayoutView):
    def __init__(self, channel: discord.TextChannel, prize: str, end_ts: int):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# Giveaway Started"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"**Channel:** {channel.mention}\n"
                    f"**Prize:** {prize}\n"
                    f"**Ends:** <t:{end_ts}:R>"
                ),
                accent_color=discord.Color.gold()
            )
        )


class GiveawayRerollView(discord.ui.LayoutView):
    def __init__(self, winners: list):
        super().__init__(timeout=None)
        winner_text = " ".join(f"<@{uid}>" for uid in winners)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# Giveaway Rerolled"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(f"New winner(s): {winner_text}"),
                accent_color=discord.Color.gold()
            )
        )


class InfoView(discord.ui.LayoutView):
    def __init__(self, title: str, body: str, color: discord.Color = discord.Color.blurple()):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(f"# {title}"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(body),
                accent_color=color
            )
        )


EMOJI_RE = re.compile(r"<(a?):(\w{2,32}):(\d+)>")


async def steal_emoji(guild: discord.Guild, text: str, reason: str) -> discord.ui.LayoutView:
    match = EMOJI_RE.search(text or "")
    if not match:
        return InfoView("Failed!", "No custom emoji found.", discord.Color.red())
    animated, name, eid = match.groups()
    ext = "gif" if animated else "png"
    url = f"https://cdn.discordapp.com/emojis/{eid}.{ext}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as res:
                if res.status != 200:
                    return InfoView("Failed!", "Could not download that emoji.", discord.Color.red())
                data = await res.read()
        emoji = await guild.create_custom_emoji(name=name, image=data, reason=reason)
        return InfoView("Emoji Stolen", f"Added {emoji} as `:{emoji.name}:`", discord.Color.green())
    except discord.Forbidden:
        return InfoView("Failed!", "I don't have permission to manage emojis.", discord.Color.red())
    except discord.HTTPException as e:
        return InfoView("Failed!", str(e), discord.Color.red())


SCATTER_SYMBOLS = ["🍒", "🍋", "🔔", "💎", "7️⃣"]
SCATTER_WEIGHTS = [30, 25, 20, 15, 10]
SCATTER_PAYOUT = {"🍒": 3, "🍋": 4, "🔔": 6, "💎": 10, "7️⃣": 25}
STARTING_COINS = 1000
COIN = "🪙"

SHOP = {
    "padlock": {"name": "Padlock", "emoji": "🔒", "price": 1500, "desc": "Blocks the next robbery against you (single use)."},
    "clover": {"name": "Lucky Clover", "emoji": "🍀", "price": 2000, "desc": "+15% success on your next crime/rob (single use)."},
    "laptop": {"name": "Laptop", "emoji": "💻", "price": 8000, "desc": "+50% earnings from work (permanent)."},
    "fishing_rod": {"name": "Fishing Rod", "emoji": "🎣", "price": 2500, "desc": "Lets you use fish (permanent)."},
    "pickaxe": {"name": "Pickaxe", "emoji": "⛏️", "price": 3500, "desc": "Lets you use mine (permanent)."},
}
PERMANENT = {"laptop", "fishing_rod", "pickaxe"}

COOLDOWNS = {
    "daily": 86400,
    "weekly": 604800,
    "work": 1800,
    "beg": 60,
    "crime": 900,
    "rob": 1800,
    "fish": 120,
    "mine": 180,
}

JOBS = [
    ("coded a Discord bot", 200, 450),
    ("delivered food around the city", 150, 350),
    ("fixed someone's PC", 250, 500),
    ("streamed on Twitch", 100, 600),
    ("did freelance design work", 200, 400),
    ("tutored a student online", 180, 380),
]

BEG_PEOPLE = ["Mang Tomas", "Aling Nena", "a kind stranger", "a jeepney driver", "a random tourist"]

FISH_TABLE = [
    ("an old boot", 0, 20, 25),
    ("a Tilapia", 80, 180, 35),
    ("a Bangus", 150, 300, 22),
    ("a Tuna", 300, 600, 12),
    ("a treasure chest", 1000, 2000, 6),
]

ORE_TABLE = [
    ("some plain rocks", 0, 30, 30),
    ("Coal", 80, 200, 30),
    ("Iron", 200, 400, 22),
    ("Gold", 500, 900, 12),
    ("a Diamond", 1500, 2500, 6),
]

ECO_ALIASES = {
    "bal": "balance", "dep": "deposit", "with": "withdraw", "pay": "give",
    "lb": "leaderboard", "top": "leaderboard", "inv": "inventory",
    "cf": "coinflip", "flip": "coinflip",
}

MENTION_RE = re.compile(r"<@!?\d+>")


def get_balance(uid: int) -> int:
    return config.get("coins", {}).get(str(uid), STARTING_COINS)


def set_balance(uid: int, amount: int):
    config.setdefault("coins", {})[str(uid)] = amount
    save_config(config)


def scatter_result(uid: int, bet: int) -> discord.ui.LayoutView:
    balance = get_balance(uid)
    if bet <= 0:
        return FailedView("Bet must be at least 1.")
    if bet > balance:
        return FailedView(f"Not enough coins. Balance: **{balance}**")
    reels = random.choices(SCATTER_SYMBOLS, weights=SCATTER_WEIGHTS, k=3)
    top = max(reels.count(s) for s in reels)
    if top == 3:
        net = bet * (SCATTER_PAYOUT[reels[0]] - 1)
        title, color = "🎰 Scatter — WIN", discord.Color.green()
    elif top == 2:
        net = 0
        title, color = "🎰 Scatter — PUSH", discord.Color.blurple()
    else:
        net = -bet
        title, color = "🎰 Scatter — LOSE", discord.Color.red()
    new_balance = balance + net
    set_balance(uid, new_balance)
    body = (
        f"## {' | '.join(reels)}\n"
        f"**Bet:** {bet}\n"
        f"**Net:** {net:+}\n"
        f"**Balance:** {new_balance}"
    )
    return InfoView(title, body, color)


def err(msg: str) -> discord.ui.LayoutView:
    return InfoView("Failed!", msg, discord.Color.red())


def ok(title: str, body: str) -> discord.ui.LayoutView:
    return InfoView(title, body, discord.Color.green())


def fmt_time(s: int) -> str:
    d, r = divmod(int(s), 86400)
    h, r = divmod(r, 3600)
    m, sec = divmod(r, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if sec or not parts:
        parts.append(f"{sec}s")
    return " ".join(parts)


def parse_amount(s: str, maximum: int):
    s = s.lower().replace(",", "").strip()
    if s == "all":
        return maximum
    if s == "half":
        return maximum // 2
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([km]?)", s)
    if not m:
        return None
    mult = {"": 1, "k": 1000, "m": 1000000}[m.group(2)]
    return int(float(m.group(1)) * mult)


def wallet(uid) -> int:
    return config.get("coins", {}).get(str(uid), STARTING_COINS)


def bank(uid) -> int:
    return config.get("bank", {}).get(str(uid), 0)


def set_wallet(uid, amt):
    config.setdefault("coins", {})[str(uid)] = max(0, int(amt))


def set_bank(uid, amt):
    config.setdefault("bank", {})[str(uid)] = max(0, int(amt))


def inv(uid) -> dict:
    return config.setdefault("inventory", {}).setdefault(str(uid), {})


def has_item(uid, item) -> bool:
    return inv(uid).get(item, 0) > 0


def add_item(uid, item, n=1):
    d = inv(uid)
    d[item] = d.get(item, 0) + n


def take_item(uid, item, n=1):
    d = inv(uid)
    d[item] = d.get(item, 0) - n
    if d[item] <= 0:
        d.pop(item, None)


def cooldown_left(uid, name) -> int:
    last = config.get("cooldowns", {}).get(str(uid), {}).get(name, 0)
    return max(0, int(last + COOLDOWNS[name] - time.time()))


def start_cooldown(uid, name):
    config.setdefault("cooldowns", {}).setdefault(str(uid), {})[name] = time.time()


def cd_view(name, left):
    ready = int(time.time()) + left
    return InfoView(
        "Slow down!",
        f"`{name}` is on cooldown. Try again <t:{ready}:R> (**{fmt_time(left)}**).",
        discord.Color.orange()
    )


def transfer(from_id, to_id, amt):
    set_wallet(from_id, wallet(from_id) - amt)
    set_wallet(to_id, wallet(to_id) + amt)


def balance_view(member):
    w, b = wallet(member.id), bank(member.id)
    return InfoView(
        f"💰 {member.display_name}'s Balance",
        f"**Wallet:** {w:,} {COIN}\n**Bank:** {b:,} {COIN}\n**Net Worth:** {w + b:,} {COIN}"
    )


def daily_view(user):
    left = cooldown_left(user.id, "daily")
    if left:
        return cd_view("daily", left)
    now = int(time.time())
    st = config.setdefault("streaks", {}).get(str(user.id), {"count": 0, "last": 0})
    st["count"] = st["count"] + 1 if now - st["last"] <= 172800 else 1
    st["last"] = now
    config["streaks"][str(user.id)] = st
    reward = 500 + min(st["count"], 30) * 50
    set_wallet(user.id, wallet(user.id) + reward)
    start_cooldown(user.id, "daily")
    save_config(config)
    return ok(
        "🎁 Daily Reward",
        f"You received **{reward:,}** {COIN}\n**Streak:** {st['count']} day(s) 🔥\n**Wallet:** {wallet(user.id):,} {COIN}"
    )


def weekly_view(user):
    left = cooldown_left(user.id, "weekly")
    if left:
        return cd_view("weekly", left)
    reward = 5000
    set_wallet(user.id, wallet(user.id) + reward)
    start_cooldown(user.id, "weekly")
    save_config(config)
    return ok("📅 Weekly Reward", f"You received **{reward:,}** {COIN}\n**Wallet:** {wallet(user.id):,} {COIN}")


def work_view(user):
    left = cooldown_left(user.id, "work")
    if left:
        return cd_view("work", left)
    job, lo, hi = random.choice(JOBS)
    pay = random.randint(lo, hi)
    bonus = ""
    if has_item(user.id, "laptop"):
        pay = int(pay * 1.5)
        bonus = "\n💻 Laptop bonus: +50%"
    set_wallet(user.id, wallet(user.id) + pay)
    start_cooldown(user.id, "work")
    save_config(config)
    return ok("💼 Work", f"You {job} and earned **{pay:,}** {COIN}{bonus}")


def beg_view(user):
    left = cooldown_left(user.id, "beg")
    if left:
        return cd_view("beg", left)
    start_cooldown(user.id, "beg")
    who = random.choice(BEG_PEOPLE)
    if random.random() < 0.65:
        amt = random.randint(20, 120)
        set_wallet(user.id, wallet(user.id) + amt)
        save_config(config)
        return ok("🙏 Beg", f"{who} gave you **{amt:,}** {COIN}")
    save_config(config)
    return InfoView("🙏 Beg", f"{who} ignored you. Nothing this time.", discord.Color.orange())


def crime_view(user):
    left = cooldown_left(user.id, "crime")
    if left:
        return cd_view("crime", left)
    chance = 0.50
    note = ""
    if has_item(user.id, "clover"):
        take_item(user.id, "clover")
        chance += 0.15
        note = "\n🍀 Lucky Clover used (+15%)"
    start_cooldown(user.id, "crime")
    if random.random() < chance:
        amt = random.randint(300, 900)
        set_wallet(user.id, wallet(user.id) + amt)
        save_config(config)
        return ok("🦹 Crime — SUCCESS", f"You pulled it off and got **{amt:,}** {COIN}{note}")
    fine = min(wallet(user.id), random.randint(150, 500))
    set_wallet(user.id, wallet(user.id) - fine)
    save_config(config)
    return InfoView("🚔 Crime — CAUGHT", f"You got caught and paid a fine of **{fine:,}** {COIN}{note}", discord.Color.red())


def rob_view(robber, victim):
    if victim.bot or victim.id == robber.id:
        return err("You can't rob that user.")
    left = cooldown_left(robber.id, "rob")
    if left:
        return cd_view("rob", left)
    if wallet(robber.id) < 200:
        return err("You need at least **200** coins in your wallet to attempt a robbery.")
    if wallet(victim.id) < 200:
        return err(f"{victim.display_name} doesn't have enough in their wallet to be worth robbing.")
    start_cooldown(robber.id, "rob")
    if has_item(victim.id, "padlock"):
        take_item(victim.id, "padlock")
        fine = min(wallet(robber.id), 300)
        transfer(robber.id, victim.id, fine)
        save_config(config)
        return InfoView(
            "🔒 Robbery Blocked",
            f"{victim.mention} had a Padlock! You paid **{fine:,}** {COIN} in damages.",
            discord.Color.red()
        )
    chance = 0.40
    note = ""
    if has_item(robber.id, "clover"):
        take_item(robber.id, "clover")
        chance += 0.15
        note = "\n🍀 Lucky Clover used (+15%)"
    if random.random() < chance:
        amt = max(1, int(wallet(victim.id) * random.uniform(0.10, 0.30)))
        transfer(victim.id, robber.id, amt)
        save_config(config)
        return ok("🥷 Robbery — SUCCESS", f"You stole **{amt:,}** {COIN} from {victim.mention}!{note}")
    fine = max(1, int(wallet(robber.id) * random.uniform(0.10, 0.20)))
    transfer(robber.id, victim.id, fine)
    save_config(config)
    return InfoView(
        "🚔 Robbery — FAILED",
        f"You got caught and paid **{fine:,}** {COIN} to {victim.mention}.{note}",
        discord.Color.red()
    )


def deposit_view(user, raw):
    amt = parse_amount(raw, wallet(user.id))
    if amt is None or amt <= 0:
        return err("Enter a valid amount (number, `all`, `half`, or `5k`).")
    if amt > wallet(user.id):
        return err(f"You only have **{wallet(user.id):,}** {COIN} in your wallet.")
    set_wallet(user.id, wallet(user.id) - amt)
    set_bank(user.id, bank(user.id) + amt)
    save_config(config)
    return ok("🏦 Deposited", f"Deposited **{amt:,}** {COIN}\n**Wallet:** {wallet(user.id):,}\n**Bank:** {bank(user.id):,}")


def withdraw_view(user, raw):
    amt = parse_amount(raw, bank(user.id))
    if amt is None or amt <= 0:
        return err("Enter a valid amount (number, `all`, `half`, or `5k`).")
    if amt > bank(user.id):
        return err(f"You only have **{bank(user.id):,}** {COIN} in your bank.")
    set_bank(user.id, bank(user.id) - amt)
    set_wallet(user.id, wallet(user.id) + amt)
    save_config(config)
    return ok("🏦 Withdrawn", f"Withdrew **{amt:,}** {COIN}\n**Wallet:** {wallet(user.id):,}\n**Bank:** {bank(user.id):,}")


def give_view(sender, receiver, raw):
    if receiver.bot or receiver.id == sender.id:
        return err("You can't give coins to that user.")
    amt = parse_amount(raw, wallet(sender.id))
    if amt is None or amt <= 0:
        return err("Enter a valid amount.")
    if amt > wallet(sender.id):
        return err(f"You only have **{wallet(sender.id):,}** {COIN} in your wallet.")
    transfer(sender.id, receiver.id, amt)
    save_config(config)
    return ok("💸 Transfer Complete", f"{sender.mention} gave **{amt:,}** {COIN} to {receiver.mention}")


def leaderboard_view(guild):
    ids = set(config.get("coins", {})) | set(config.get("bank", {}))
    rows = []
    for i in ids:
        if guild and guild.get_member(int(i)) is None:
            continue
        rows.append((wallet(i) + bank(i), i))
    rows.sort(reverse=True)
    rows = rows[:10]
    if not rows:
        return InfoView("🏆 Leaderboard", "No one is on the board yet.")
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for n, (total, i) in enumerate(rows):
        rank = medals[n] if n < 3 else f"**{n + 1}.**"
        lines.append(f"{rank} <@{i}> — **{total:,}** {COIN}")
    return InfoView("🏆 Richest Members", "\n".join(lines), discord.Color.gold())


def shop_view():
    lines = []
    for k, v in SHOP.items():
        lines.append(f"{v['emoji']} **{v['name']}** — {v['price']:,} {COIN}\n{v['desc']}\n`id: {k}`")
    lines.append("Buy with `.buy <item> [qty]` or `/buy`")
    return InfoView("🛒 Shop", "\n\n".join(lines), discord.Color.gold())


def resolve_item(text):
    t = text.lower().strip().replace("-", "_").replace(" ", "_")
    for k, v in SHOP.items():
        if t == k or t == v["name"].lower().replace(" ", "_"):
            return k
    return None


def buy_view(user, key, qty):
    if key not in SHOP:
        return err("Unknown item. Check `.shop` for the list.")
    item = SHOP[key]
    if key in PERMANENT:
        if has_item(user.id, key):
            return err(f"You already own a {item['name']}.")
        qty = 1
    qty = max(1, min(qty, 100))
    cost = item["price"] * qty
    if cost > wallet(user.id):
        return err(f"You need **{cost:,}** {COIN} but only have **{wallet(user.id):,}** in your wallet.")
    set_wallet(user.id, wallet(user.id) - cost)
    add_item(user.id, key, qty)
    save_config(config)
    return ok("🛒 Purchase Complete", f"Bought **{qty}× {item['emoji']} {item['name']}** for **{cost:,}** {COIN}")


def sell_view(user, key, qty):
    if key not in SHOP:
        return err("Unknown item.")
    item = SHOP[key]
    qty = max(1, qty)
    owned = inv(user.id).get(key, 0)
    if owned < qty:
        return err(f"You only own **{owned}** of {item['name']}.")
    gain = (item["price"] // 2) * qty
    take_item(user.id, key, qty)
    set_wallet(user.id, wallet(user.id) + gain)
    save_config(config)
    return ok("💱 Sold", f"Sold **{qty}× {item['emoji']} {item['name']}** for **{gain:,}** {COIN} (50% value)")


def inventory_view(user):
    items = inv(user.id)
    if not items:
        return InfoView(f"🎒 {user.display_name}'s Inventory", "Your inventory is empty. Visit the `.shop`!")
    lines = [f"{SHOP[k]['emoji']} **{SHOP[k]['name']}** × {n}" for k, n in items.items() if k in SHOP]
    return InfoView(f"🎒 {user.display_name}'s Inventory", "\n".join(lines) or "Empty.")


def weighted_reward(table):
    name, lo, hi, _ = random.choices(table, weights=[t[3] for t in table], k=1)[0]
    return name, random.randint(lo, hi)


def fish_view(user):
    if not has_item(user.id, "fishing_rod"):
        return err("You need a 🎣 Fishing Rod. Buy one with `.buy fishing_rod`.")
    left = cooldown_left(user.id, "fish")
    if left:
        return cd_view("fish", left)
    name, amt = weighted_reward(FISH_TABLE)
    set_wallet(user.id, wallet(user.id) + amt)
    start_cooldown(user.id, "fish")
    save_config(config)
    return ok("🎣 Fishing", f"You caught {name} worth **{amt:,}** {COIN}")


def mine_view(user):
    if not has_item(user.id, "pickaxe"):
        return err("You need a ⛏️ Pickaxe. Buy one with `.buy pickaxe`.")
    left = cooldown_left(user.id, "mine")
    if left:
        return cd_view("mine", left)
    name, amt = weighted_reward(ORE_TABLE)
    set_wallet(user.id, wallet(user.id) + amt)
    start_cooldown(user.id, "mine")
    save_config(config)
    return ok("⛏️ Mining", f"You mined {name} worth **{amt:,}** {COIN}")


def coinflip_view(user, raw_bet, side):
    side = {"h": "heads", "head": "heads", "heads": "heads", "t": "tails", "tail": "tails", "tails": "tails"}.get(side.lower())
    if not side:
        return err("Pick `heads` or `tails`.")
    bet = parse_amount(raw_bet, wallet(user.id))
    if bet is None or bet <= 0:
        return err("Enter a valid bet.")
    if bet > wallet(user.id):
        return err(f"Not enough coins. Wallet: **{wallet(user.id):,}**")
    result = random.choice(["heads", "tails"])
    if result == side:
        set_wallet(user.id, wallet(user.id) + bet)
        save_config(config)
        return ok("🪙 Coinflip — WIN", f"It landed on **{result}**! You won **{bet:,}** {COIN}\n**Wallet:** {wallet(user.id):,}")
    set_wallet(user.id, wallet(user.id) - bet)
    save_config(config)
    return InfoView("🪙 Coinflip — LOSE", f"It landed on **{result}**. You lost **{bet:,}** {COIN}\n**Wallet:** {wallet(user.id):,}", discord.Color.red())


def eco_admin_view(action, member, amount, moderator=None):
    uid = member.id
    if action == "add":
        set_wallet(uid, wallet(uid) + amount)
        text = f"Added **{amount:,}** {COIN} to {member.mention}"
    elif action == "remove":
        set_wallet(uid, wallet(uid) - amount)
        text = f"Removed **{amount:,}** {COIN} from {member.mention}"
    elif action == "set":
        set_wallet(uid, amount)
        text = f"Set {member.mention}'s wallet to **{amount:,}** {COIN}"
    else:
        set_wallet(uid, STARTING_COINS)
        set_bank(uid, 0)
        config.get("inventory", {}).pop(str(uid), None)
        config.get("cooldowns", {}).pop(str(uid), None)
        config.get("streaks", {}).pop(str(uid), None)
        text = f"Reset {member.mention}'s economy data."
    save_config(config)
    if moderator is not None and getattr(member, "guild", None) is not None:
        asyncio.create_task(log_bot(
            member.guild,
            "🛠️ Economy Admin",
            f"**Moderator:** {fmt_ref(moderator)}\n**Target:** {fmt_ref(member)}\n{text}",
            discord.Color.orange()
        ))
    return ok("🛠️ Economy Admin", text + f"\n**Wallet now:** {wallet(uid):,} {COIN}")


SIMPLE_ECONOMY = {
    "daily": ("Claim your daily reward", lambda u, g: daily_view(u)),
    "weekly": ("Claim your weekly reward", lambda u, g: weekly_view(u)),
    "work": ("Work to earn coins", lambda u, g: work_view(u)),
    "beg": ("Beg for some coins", lambda u, g: beg_view(u)),
    "crime": ("Commit a crime (risky!)", lambda u, g: crime_view(u)),
    "fish": ("Go fishing (needs a Fishing Rod)", lambda u, g: fish_view(u)),
    "mine": ("Go mining (needs a Pickaxe)", lambda u, g: mine_view(u)),
    "shop": ("View the shop", lambda u, g: shop_view()),
    "inventory": ("View your inventory", lambda u, g: inventory_view(u)),
    "leaderboard": ("See the richest members", lambda u, g: leaderboard_view(g)),
}


async def economy_prefix(message, cmd, args) -> bool:
    cmd = ECO_ALIASES.get(cmd, cmd)
    author = message.author
    send = message.channel.send
    rest = [a for a in args[1:] if not MENTION_RE.fullmatch(a)]
    target = message.mentions[0] if message.mentions else None

    if cmd in SIMPLE_ECONOMY:
        await send(view=SIMPLE_ECONOMY[cmd][1](author, message.guild))
    elif cmd == "balance":
        await send(view=balance_view(target or author))
    elif cmd == "rob":
        if not target:
            await send(view=err("Usage: `.rob @user`"))
        else:
            await send(view=rob_view(author, target))
    elif cmd == "deposit":
        if not rest:
            await send(view=err("Usage: `.deposit <amount|all>`"))
        else:
            await send(view=deposit_view(author, rest[0]))
    elif cmd == "withdraw":
        if not rest:
            await send(view=err("Usage: `.withdraw <amount|all>`"))
        else:
            await send(view=withdraw_view(author, rest[0]))
    elif cmd == "give":
        if not target or not rest:
            await send(view=err("Usage: `.give @user <amount>`"))
        else:
            await send(view=give_view(author, target, rest[0]))
    elif cmd in ("buy", "sell"):
        qty = 1
        name_parts = rest
        if name_parts and name_parts[-1].isdigit():
            qty = int(name_parts[-1])
            name_parts = name_parts[:-1]
        key = resolve_item(" ".join(name_parts)) if name_parts else None
        if not key:
            await send(view=err(f"Usage: `.{cmd} <item> [qty]` — see `.shop` for item names."))
        elif cmd == "buy":
            await send(view=buy_view(author, key, qty))
        else:
            await send(view=sell_view(author, key, qty))
    elif cmd == "coinflip":
        if len(rest) < 2:
            await send(view=err("Usage: `.coinflip <bet> <heads|tails>`"))
        else:
            await send(view=coinflip_view(author, rest[0], rest[1]))
    elif cmd in ("addcoins", "removecoins", "setcoins", "resetcoins"):
        if not author.guild_permissions.administrator:
            return True
        if not target:
            usage = f"Usage: `.{cmd} @user`" if cmd == "resetcoins" else f"Usage: `.{cmd} @user <amount>`"
            await send(view=err(usage))
            return True
        action = cmd.replace("coins", "")
        if action == "reset":
            await send(view=eco_admin_view("reset", target, 0, author))
            return True
        amount = parse_amount(rest[0], 0) if rest else None
        if amount is None or amount < 0 or (action != "set" and amount == 0):
            await send(view=err("Enter a valid amount."))
        else:
            await send(view=eco_admin_view(action, target, amount, author))
    else:
        return False
    return True


LOG_CATEGORIES = {
    "mod": "🛡️ Moderation",
    "message": "💬 Messages",
    "member": "👤 Members",
    "voice": "🔊 Voice",
    "server": "🏠 Server",
    "bot": "🤖 Bot & Economy",
    "security": "🚨 Security",
}

ACTION_STYLE = {
    "Kick": ("👢", discord.Color.orange()),
    "Ban": ("🔨", discord.Color.red()),
    "Unban": ("🔓", discord.Color.green()),
    "Timeout": ("⏳", discord.Color.yellow()),
    "Untimeout": ("⌛", discord.Color.green()),
    "Warn": ("⚠️", discord.Color.yellow()),
    "Clear Warns": ("🧹", discord.Color.green()),
    "Purge": ("🗑️", discord.Color.blurple()),
    "Lock": ("🔒", discord.Color.red()),
    "Unlock": ("🔓", discord.Color.green()),
    "Slowmode": ("🐢", discord.Color.blurple()),
    "Auto Kick": ("🤖", discord.Color.red()),
}

recent_actions = {}
NO_MENTIONS = discord.AllowedMentions.none()
CHANNEL_MENTION_RE = re.compile(r"<#\d+>")


class LogView(discord.ui.LayoutView):
    def __init__(self, title: str, body: str, color: discord.Color = discord.Color.blurple()):
        super().__init__(timeout=None)
        ts = int(time.time())
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(f"# {title}"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(body),
                discord.ui.Separator(),
                discord.ui.TextDisplay(f"-# <t:{ts}:F> • <t:{ts}:R>"),
                accent_color=color
            )
        )


def clip(text, limit=900) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit - 1] + "…"


def quote(text, limit=900) -> str:
    lines = clip(text, limit).splitlines()
    return "\n".join("> " + line for line in lines) if lines else "> "


def fmt_ref(obj) -> str:
    if obj is None:
        return "Unknown"
    mention = getattr(obj, "mention", None) or getattr(obj, "name", "Unknown")
    return f"{mention} (`{obj.id}`)"


def mark_action(kind: str, target_id: int):
    now = time.time()
    for key in [k for k, t in recent_actions.items() if now - t > 60]:
        recent_actions.pop(key, None)
    recent_actions[(kind, target_id)] = now


def peek_action(kind: str, target_id: int) -> bool:
    t = recent_actions.get((kind, target_id))
    return t is not None and time.time() - t < 20


def consume_action(kind: str, target_id: int) -> bool:
    t = recent_actions.pop((kind, target_id), None)
    return t is not None and time.time() - t < 20


async def find_audit(guild, action, target_id, within=15):
    try:
        async for entry in guild.audit_logs(limit=8, action=action):
            target = entry.target
            if target is not None and getattr(target, "id", None) == target_id:
                if (discord.utils.utcnow() - entry.created_at).total_seconds() <= within:
                    return entry
    except Exception:
        pass
    return None


async def send_log(guild, category: str, view: discord.ui.LayoutView):
    if guild is None or guild.id != GUILD_ID:
        return
    channel_id = config.get("log_channels", {}).get(category)
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        return
    try:
        await channel.send(view=view, allowed_mentions=NO_MENTIONS)
    except Exception as e:
        print(f"Log send failed ({category}): {e}")


async def log_mod(guild, action: str, moderator, target=None, reason=None, extra=None):
    icon, color = ACTION_STYLE.get(action, ("🛡️", discord.Color.blurple()))
    lines = [f"**Moderator:** {fmt_ref(moderator)}"]
    if target is not None:
        lines.append(f"**Target:** {fmt_ref(target)}")
    if reason:
        lines.append(f"**Reason:** {clip(reason, 500)}")
    if extra:
        lines.append(extra)
    await send_log(guild, "mod", LogView(f"{icon} {action}", "\n".join(lines), color))


async def log_bot(guild, title: str, body: str, color: discord.Color = discord.Color.blurple()):
    await send_log(guild, "bot", LogView(title, body, color))


async def log_message_delete(message):
    body = (
        f"**Author:** {fmt_ref(message.author)}\n"
        f"**Channel:** {message.channel.mention}\n"
        f"**Sent:** <t:{int(message.created_at.timestamp())}:R>"
    )
    if message.content:
        body += f"\n\n**Content**\n{quote(message.content, 1000)}"
    if message.attachments:
        files = "\n".join(f"• {a.filename}" for a in message.attachments[:10])
        body += f"\n\n**Attachments**\n{clip(files, 500)}"
    await send_log(message.guild, "message", LogView("🗑️ Message Deleted", body, discord.Color.red()))


def logs_status_view(guild) -> discord.ui.LayoutView:
    cfg = config.get("log_channels", {})
    lines = []
    for key, label in LOG_CATEGORIES.items():
        cid = cfg.get(key)
        ch = guild.get_channel(cid) if cid else None
        lines.append(f"{label} (`{key}`) — {ch.mention if ch else '`Off`'}")
    lines.append("\nSet with `/logs set` or `.setlog <category|all> #channel`")
    return InfoView("📋 Log Channels", "\n".join(lines))


async def apply_log_channel(category: str, channel) -> discord.ui.LayoutView:
    if category != "all" and category not in LOG_CATEGORIES:
        keys = ", ".join(f"`{k}`" for k in LOG_CATEGORIES)
        return err(f"Unknown category. Use: {keys}, `all`")
    label = "all" if category == "all" else LOG_CATEGORIES[category]
    try:
        await channel.send(
            view=LogView("✅ Log Channel Set", f"This channel will now receive **{label}** logs.", discord.Color.green()),
            allowed_mentions=NO_MENTIONS
        )
    except Exception:
        return err(f"I can't send messages in {channel.mention}.")
    targets = list(LOG_CATEGORIES) if category == "all" else [category]
    cfg = config.setdefault("log_channels", {})
    for key in targets:
        cfg[key] = channel.id
    save_config(config)
    return ok("📋 Log Channel Updated", f"**{label}** logs → {channel.mention}")


def disable_log_channel(category: str) -> discord.ui.LayoutView:
    if category != "all" and category not in LOG_CATEGORIES:
        keys = ", ".join(f"`{k}`" for k in LOG_CATEGORIES)
        return err(f"Unknown category. Use: {keys}, `all`")
    targets = list(LOG_CATEGORIES) if category == "all" else [category]
    cfg = config.setdefault("log_channels", {})
    for key in targets:
        cfg.pop(key, None)
    save_config(config)
    label = "all" if category == "all" else LOG_CATEGORIES[category]
    return ok("📋 Logging Disabled", f"**{label}** logs are now off.")


async def logs_prefix(message, cmd, args) -> bool:
    if cmd not in ("logs", "setlog", "dellog"):
        return False
    if not message.author.guild_permissions.administrator:
        return True
    send = message.channel.send
    category = next((a.lower() for a in args[1:] if not CHANNEL_MENTION_RE.fullmatch(a)), None)
    if cmd == "logs":
        await send(view=logs_status_view(message.guild))
    elif cmd == "setlog":
        if not category or not message.channel_mentions:
            await send(view=err("Usage: `.setlog <category|all> #channel`"))
        else:
            await send(view=await apply_log_channel(category, message.channel_mentions[0]))
    else:
        if not category:
            await send(view=err("Usage: `.dellog <category|all>`"))
        else:
            await send(view=disable_log_channel(category))
    return True


SEC_DEFAULTS = {
    "antiraid": {"enabled": False, "threshold": 5, "window": 10, "duration": 300, "action": "kick", "min_age_days": 0, "panic": False},
    "antinuke": {"enabled": False, "limit": 3, "member_limit": 5, "window": 30, "action": "strip", "recover": True, "guard_perms": True},
    "antispam": {"enabled": False, "messages": 6, "window": 5, "timeout_minutes": 10},
    "antilink": {"enabled": False, "invites": True, "links": False, "timeout_minutes": 0},
    "antimention": {"enabled": False, "limit": 5, "timeout_minutes": 10},
}

SEC_LABELS = {
    "antiraid": ("🚧", "Anti-Raid"),
    "antinuke": ("☢️", "Anti-Nuke"),
    "antispam": ("💬", "Anti-Spam"),
    "antilink": ("🔗", "Anti-Link"),
    "antimention": ("📣", "Anti-Mass-Mention"),
}

SEC_RANGES = {
    "threshold": (2, 50),
    "window": (2, 300),
    "duration": (30, 3600),
    "min_age_days": (0, 365),
    "limit": (1, 20),
    "member_limit": (1, 50),
    "messages": (3, 30),
    "timeout_minutes": (0, 40320),
}

MODULE_PERMS = {
    "antiraid": {"kick_members": "Kick Members", "ban_members": "Ban Members", "moderate_members": "Timeout Members"},
    "antinuke": {"view_audit_log": "View Audit Log", "ban_members": "Ban Members", "kick_members": "Kick Members",
                 "manage_roles": "Manage Roles", "manage_channels": "Manage Channels", "manage_webhooks": "Manage Webhooks"},
    "antispam": {"manage_messages": "Manage Messages", "moderate_members": "Timeout Members"},
    "antilink": {"manage_messages": "Manage Messages"},
    "antimention": {"manage_messages": "Manage Messages", "moderate_members": "Timeout Members"},
}

DANGEROUS_PERMS = ("administrator", "manage_guild", "manage_roles", "manage_channels", "ban_members", "kick_members", "manage_webhooks")

NUKE_GROUPS = {
    "channel_create": "structure", "channel_delete": "structure",
    "role_create": "structure", "role_delete": "structure",
    "webhook_create": "structure", "ban": "members", "kick": "members",
}

NUKE_LABELS = {
    "channel_create": "channel(s) created", "channel_delete": "channel(s) deleted",
    "role_create": "role(s) created", "role_delete": "role(s) deleted",
    "webhook_create": "webhook(s) created", "ban": "member(s) banned", "kick": "member(s) kicked",
}

INVITE_RE = re.compile(r"(?:discord\.gg|discord(?:app)?\.com/invite|dsc\.gg)/[\w-]+", re.I)
LINK_RE = re.compile(r"https?://\S+|www\.\S+", re.I)

raid_state = {"joins": deque(), "active_until": 0.0, "count": 0}
spam_tracker = {}
nuke_records = {}
nuke_punished = {}
seen_audit = set()


def sec_cfg(name: str) -> dict:
    store = config.setdefault("security", {})
    cfg = store.setdefault(name, {})
    for key, value in SEC_DEFAULTS[name].items():
        cfg.setdefault(key, value)
    return cfg


def whitelist_ids() -> list:
    return config.setdefault("security", {}).setdefault("whitelist", [])


def is_trusted(guild, user_id: int) -> bool:
    if bot.user is not None and user_id == bot.user.id:
        return True
    if user_id == guild.owner_id:
        return True
    return user_id in whitelist_ids()


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def missing_perms(guild, perms: dict) -> str:
    me = guild.me.guild_permissions if guild.me else None
    if me is None:
        return ""
    missing = [label for attr, label in perms.items() if not getattr(me, attr)]
    if not missing:
        return ""
    return "\n⚠️ Bot is missing: " + ", ".join(missing)


def describe_module(name: str) -> str:
    cfg = sec_cfg(name)
    if name == "antiraid":
        text = f"{cfg['threshold']} joins in {cfg['window']}s → raid mode for {fmt_time(cfg['duration'])}, action: **{cfg['action']}**"
        if cfg["min_age_days"]:
            text += f"\nAccounts younger than **{cfg['min_age_days']}** day(s) are actioned"
        if cfg["panic"]:
            text += "\n🚨 **Panic mode is ON**"
        return text
    if name == "antinuke":
        return (
            f"Limit: **{cfg['limit']}** channel/role/webhook actions or **{cfg['member_limit']}** bans/kicks per {cfg['window']}s\n"
            f"Punishment: **{cfg['action']}** • Recovery: **{'on' if cfg['recover'] else 'off'}** • "
            f"Permission guard: **{'on' if cfg['guard_perms'] else 'off'}**"
        )
    if name == "antispam":
        return f"{cfg['messages']} messages in {cfg['window']}s → timeout **{cfg['timeout_minutes']}** minute(s)"
    if name == "antilink":
        return f"Invites: **{'blocked' if cfg['invites'] else 'allowed'}** • Links: **{'blocked' if cfg['links'] else 'allowed'}**"
    return f"{cfg['limit']}+ mentions in one message → timeout **{cfg['timeout_minutes']}** minute(s)"


def security_status_view(guild) -> discord.ui.LayoutView:
    lines = []
    for name, (emoji, label) in SEC_LABELS.items():
        cfg = sec_cfg(name)
        state = "✅ On" if cfg["enabled"] else "❌ Off"
        lines.append(f"{emoji} **{label}** — {state}\n{describe_module(name)}")
    ids = whitelist_ids()
    wl = " ".join(f"<@{i}>" for i in ids) if ids else "none"
    lines.append(f"🤝 **Whitelist:** {clip(wl, 500)}\n-# Server owner and this bot are always trusted")
    needed = {}
    for perms in MODULE_PERMS.values():
        needed.update(perms)
    note = missing_perms(guild, needed)
    if note:
        lines.append(note.strip())
    return InfoView("🛡️ Security Status", "\n\n".join(lines))


def update_module(guild, name: str, enabled: bool, **opts) -> discord.ui.LayoutView:
    cfg = sec_cfg(name)
    cfg["enabled"] = enabled
    for key, value in opts.items():
        if value is None:
            continue
        if key in SEC_RANGES:
            value = clamp(int(value), *SEC_RANGES[key])
        cfg[key] = value
    save_config(config)
    emoji, label = SEC_LABELS[name]
    note = missing_perms(guild, MODULE_PERMS[name]) if enabled else ""
    return ok(f"{emoji} {label}", f"{label} is now **{'enabled' if enabled else 'disabled'}**.\n{describe_module(name)}{note}")


def panic_view(enabled: bool) -> discord.ui.LayoutView:
    cfg = sec_cfg("antiraid")
    cfg["panic"] = enabled
    if not enabled:
        raid_state["active_until"] = 0.0
        raid_state["joins"].clear()
    save_config(config)
    if enabled:
        return InfoView("🚨 Panic Mode ON", f"Every new member will be **{cfg['action']}ed** until you turn it off with `/security panic` or `.panic off`.", discord.Color.red())
    return ok("✅ Panic Mode OFF", "New members can join normally again.")


def whitelist_view(guild, action: str, member) -> discord.ui.LayoutView:
    ids = whitelist_ids()
    if action == "list":
        text = "\n".join(f"<@{i}> (`{i}`)" for i in ids) if ids else "No whitelisted users."
        return InfoView("🤝 Security Whitelist", text + "\n\n-# Server owner and this bot are always trusted")
    if member is None:
        return err("Mention a member.")
    if action == "add":
        if member.id not in ids:
            ids.append(member.id)
            save_config(config)
        return ok("🤝 Whitelist Updated", f"{member.mention} is now trusted by the security system.")
    if member.id in ids:
        ids.remove(member.id)
        save_config(config)
    return ok("🤝 Whitelist Updated", f"{member.mention} removed from the whitelist.")


async def send_security(guild, title: str, body: str, color: discord.Color = discord.Color.red(), ping: bool = True):
    if guild is None or guild.id != GUILD_ID:
        return
    cfg = config.get("log_channels", {})
    channel_id = cfg.get("security") or cfg.get("mod")
    channel = guild.get_channel(channel_id) if channel_id else None
    if channel is None:
        return
    mentions = NO_MENTIONS
    text = body
    if ping and guild.owner_id:
        mentions = discord.AllowedMentions(everyone=False, roles=False, users=[discord.Object(id=guild.owner_id)])
        text = f"{body}\n\n<@{guild.owner_id}>"
    try:
        await channel.send(view=LogView(title, text, color), allowed_mentions=mentions)
    except Exception as e:
        print(f"Security alert failed: {e}")


def raid_active() -> bool:
    return sec_cfg("antiraid")["panic"] or time.time() < raid_state["active_until"]


async def raid_punish(member, reason: str) -> bool:
    cfg = sec_cfg("antiraid")
    action = cfg["action"]
    try:
        if action == "ban":
            mark_action("ban", member.id)
            await member.ban(reason=f"AntiRaid: {reason}")
        elif action == "timeout":
            mark_action("timeout", member.id)
            await member.timeout(discord.utils.utcnow() + timedelta(hours=1), reason=f"AntiRaid: {reason}")
        else:
            mark_action("kick", member.id)
            await member.kick(reason=f"AntiRaid: {reason}")
    except Exception as e:
        print(f"Raid punish failed: {e}")
        return False
    raid_state["count"] += 1
    return True


async def antiraid_on_join(member):
    cfg = sec_cfg("antiraid")
    guild = member.guild
    if guild.id != GUILD_ID or member.bot or not (cfg["enabled"] or cfg["panic"]):
        return
    if is_trusted(guild, member.id):
        return
    now = time.time()
    joins = raid_state["joins"]
    joins.append((now, member.id))
    while joins and now - joins[0][0] > cfg["window"]:
        joins.popleft()
    if cfg["enabled"] and len(joins) >= cfg["threshold"] and not raid_active():
        raid_state["active_until"] = now + cfg["duration"]
        raid_state["count"] = 0
        ids = [mid for _, mid in joins]
        joins.clear()
        await send_security(
            guild,
            "🚨 Raid Detected",
            f"**{len(ids)}** members joined within **{cfg['window']}s**.\n"
            f"**Raid mode:** active for {fmt_time(cfg['duration'])}\n**Action:** {cfg['action']}"
        )
        for mid in ids:
            target = guild.get_member(mid)
            if target:
                await raid_punish(target, "Raid detected")
        await send_security(guild, "🛡️ Raid Response", f"Actioned **{raid_state['count']}** member(s) with **{cfg['action']}**.", discord.Color.orange(), ping=False)
        return
    reason = None
    if raid_active():
        reason = "Panic mode" if cfg["panic"] else "Raid mode active"
    elif cfg["enabled"] and cfg["min_age_days"]:
        age = (discord.utils.utcnow() - member.created_at).days
        if age < cfg["min_age_days"]:
            reason = f"Account is {age} day(s) old (minimum {cfg['min_age_days']})"
    if reason and await raid_punish(member, reason):
        total = raid_state["count"]
        if total <= 10 or total % 10 == 0:
            await send_security(
                guild,
                "🚧 Anti-Raid",
                f"**User:** {fmt_ref(member)}\n**Reason:** {reason}\n**Action:** {cfg['action']}",
                discord.Color.orange(),
                ping=False
            )


async def find_audit_retry(guild, action, target_id):
    entry = await find_audit(guild, action, target_id, within=10)
    if entry is None:
        await asyncio.sleep(1.5)
        entry = await find_audit(guild, action, target_id, within=12)
    return entry


def snapshot_channel(channel) -> dict:
    return {
        "name": channel.name,
        "type": channel.type,
        "category_id": getattr(channel, "category_id", None),
        "overwrites": dict(channel.overwrites),
        "topic": getattr(channel, "topic", None),
        "nsfw": getattr(channel, "nsfw", False),
        "slowmode": getattr(channel, "slowmode_delay", 0),
        "bitrate": getattr(channel, "bitrate", 64000),
        "user_limit": getattr(channel, "user_limit", 0),
    }


def snapshot_role(role) -> dict:
    try:
        member_ids = [m.id for m in role.members][:50]
    except Exception:
        member_ids = []
    return {
        "name": role.name,
        "colour": role.colour,
        "permissions": role.permissions,
        "hoist": role.hoist,
        "mentionable": role.mentionable,
        "members": member_ids,
    }


async def recreate_channel(guild, snap: dict):
    category = guild.get_channel(snap["category_id"]) if snap.get("category_id") else None
    overwrites = {t: o for t, o in snap["overwrites"].items() if guild.get_role(t.id) or guild.get_member(t.id)}
    reason = "AntiNuke: restoring deleted channel"
    if snap["type"] == discord.ChannelType.category:
        return await guild.create_category(snap["name"], overwrites=overwrites, reason=reason)
    if snap["type"] in (discord.ChannelType.voice, discord.ChannelType.stage_voice):
        return await guild.create_voice_channel(
            snap["name"], category=category, overwrites=overwrites,
            bitrate=min(snap["bitrate"], guild.bitrate_limit), user_limit=snap["user_limit"], reason=reason
        )
    return await guild.create_text_channel(
        snap["name"], category=category, overwrites=overwrites, topic=snap.get("topic"),
        nsfw=snap.get("nsfw", False), slowmode_delay=snap.get("slowmode", 0), reason=reason
    )


async def recreate_role(guild, snap: dict):
    role = await guild.create_role(
        name=snap["name"], colour=snap["colour"], permissions=snap["permissions"],
        hoist=snap["hoist"], mentionable=snap["mentionable"], reason="AntiNuke: restoring deleted role"
    )
    for member_id in snap.get("members", []):
        target = guild.get_member(member_id)
        if target:
            try:
                await target.add_roles(role, reason="AntiNuke: restoring role members")
            except Exception:
                pass
    return role


async def nuke_recover(guild, records: list) -> str:
    restored = removed = failed = 0
    for _, kind, data in records:
        try:
            if kind == "channel_delete" and data:
                await recreate_channel(guild, data)
                restored += 1
            elif kind == "role_delete" and data:
                await recreate_role(guild, data)
                restored += 1
            elif kind == "ban" and data:
                await guild.unban(discord.Object(id=data), reason="AntiNuke: reverting mass ban")
                restored += 1
            elif kind == "channel_create" and data:
                channel = guild.get_channel(data)
                if channel:
                    await channel.delete(reason="AntiNuke: removing channel created during attack")
                    removed += 1
            elif kind == "role_create" and data:
                role = guild.get_role(data)
                if role:
                    await role.delete(reason="AntiNuke: removing role created during attack")
                    removed += 1
            elif kind == "webhook_create" and data:
                for webhook in await guild.webhooks():
                    if webhook.id == data:
                        await webhook.delete(reason="AntiNuke: removing webhook created during attack")
                        removed += 1
        except Exception:
            failed += 1
    if not (restored or removed or failed):
        return "nothing to restore"
    return f"restored {restored}, removed {removed}, failed {failed}"


async def apply_punish(guild, actor_id: int, action: str, reason: str) -> str:
    member = guild.get_member(actor_id)
    order = [action] + [a for a in ("ban", "kick", "strip") if a != action]
    for method in order:
        try:
            if method == "ban":
                mark_action("ban", actor_id)
                await guild.ban(discord.Object(id=actor_id), reason=reason)
                return "banned"
            if method == "kick":
                if member is None:
                    continue
                mark_action("kick", actor_id)
                await member.kick(reason=reason)
                return "kicked"
            if member is None:
                continue
            roles = [r for r in member.roles if not r.is_default() and not r.managed and r < guild.me.top_role]
            if not roles:
                return "no manageable roles to strip"
            await member.remove_roles(*roles, reason=reason)
            return f"stripped {len(roles)} role(s)"
        except Exception:
            continue
    return "failed (bot role is too low?)"


async def nuke_punish(guild, actor_id: int, records: list, title: str, extra: str = ""):
    cfg = sec_cfg("antinuke")
    nuke_punished[actor_id] = time.time()
    actor = guild.get_member(actor_id)
    counts = Counter(kind for _, kind, _ in records)
    summary = "\n".join(f"• {n} {NUKE_LABELS[k]}" for k, n in counts.items())
    outcome = await apply_punish(guild, actor_id, cfg["action"], f"AntiNuke: {title}")
    recovered = ""
    if cfg["recover"] and records:
        recovered = f"\n**Recovery:** {await nuke_recover(guild, records)}"
    who = fmt_ref(actor) if actor else f"<@{actor_id}> (`{actor_id}`)"
    body = f"**Offender:** {who}\n**Reason:** {title}"
    if summary:
        body += f"\n{summary}"
    if extra:
        body += f"\n{extra}"
    body += f"\n**Punishment:** {outcome}{recovered}"
    await send_security(guild, "☢️ Anti-Nuke Triggered", body)


async def nuke_record(guild, kind: str, entry, data=None):
    cfg = sec_cfg("antinuke")
    if entry.user is None:
        return
    actor_id = entry.user.id
    if is_trusted(guild, actor_id):
        return
    now = time.time()
    if now - nuke_punished.get(actor_id, 0) < 60:
        if cfg["recover"]:
            await nuke_recover(guild, [(now, kind, data)])
        return
    group = NUKE_GROUPS[kind]
    dq = nuke_records.setdefault((actor_id, group), deque())
    dq.append((now, kind, data))
    while dq and now - dq[0][0] > cfg["window"]:
        dq.popleft()
    limit = cfg["limit"] if group == "structure" else cfg["member_limit"]
    if len(dq) >= limit:
        records = list(dq)
        dq.clear()
        await nuke_punish(guild, actor_id, records, f"Mass action: {len(records)} in {cfg['window']}s")


async def nuke_track(guild, kind: str, audit_action, target_id: int, data=None):
    if not sec_cfg("antinuke")["enabled"] or guild.id != GUILD_ID:
        return
    entry = await find_audit_retry(guild, audit_action, target_id)
    if entry is not None:
        await nuke_record(guild, kind, entry, data)


def has_dangerous(perms) -> bool:
    return any(getattr(perms, p) for p in DANGEROUS_PERMS)


async def antinuke_role_update_guard(before, after):
    cfg = sec_cfg("antinuke")
    guild = after.guild
    if not cfg["enabled"] or not cfg["guard_perms"] or guild.id != GUILD_ID or after.managed:
        return
    gained = [p for p in DANGEROUS_PERMS if getattr(after.permissions, p) and not getattr(before.permissions, p)]
    if not gained:
        return
    entry = await find_audit_retry(guild, discord.AuditLogAction.role_update, after.id)
    if entry is None or entry.user is None or is_trusted(guild, entry.user.id):
        return
    reverted = "reverted"
    try:
        await after.edit(permissions=before.permissions, reason="AntiNuke: unauthorized dangerous permission grant")
    except Exception:
        reverted = "could not revert"
    names = ", ".join(p.replace("_", " ") for p in gained)
    await nuke_punish(guild, entry.user.id, [], "Unauthorized dangerous permission grant", f"**Role:** {after.mention}\n**Permissions:** {names} ({reverted})")


async def antinuke_member_role_guard(member, added: list):
    cfg = sec_cfg("antinuke")
    guild = member.guild
    if not cfg["enabled"] or not cfg["guard_perms"] or guild.id != GUILD_ID:
        return
    risky = [r for r in added if not r.managed and has_dangerous(r.permissions)]
    if not risky:
        return
    entry = await find_audit_retry(guild, discord.AuditLogAction.member_role_update, member.id)
    if entry is None or entry.user is None or is_trusted(guild, entry.user.id):
        return
    removed = "removed"
    try:
        await member.remove_roles(*risky, reason="AntiNuke: unauthorized dangerous role grant")
    except Exception:
        removed = "could not remove"
    names = " ".join(r.mention for r in risky)
    await nuke_punish(guild, entry.user.id, [], "Unauthorized dangerous role grant", f"**Target:** {fmt_ref(member)}\n**Roles:** {names} ({removed})")


async def antinuke_bot_add(member):
    cfg = sec_cfg("antinuke")
    guild = member.guild
    if not member.bot or not cfg["enabled"] or guild.id != GUILD_ID:
        return
    entry = await find_audit_retry(guild, discord.AuditLogAction.bot_add, member.id)
    if entry is None or entry.user is None or is_trusted(guild, entry.user.id):
        return
    kicked = "kicked"
    try:
        mark_action("kick", member.id)
        await member.kick(reason="AntiNuke: bot added by an untrusted user")
    except Exception:
        kicked = "could not kick"
    await nuke_punish(guild, entry.user.id, [], "Added a bot without permission", f"**Bot:** {fmt_ref(member)} ({kicked})")


def automod_exempt(member) -> bool:
    perms = member.guild_permissions
    if perms.administrator or perms.manage_messages:
        return True
    return is_trusted(member.guild, member.id)


async def automod_punish(message, rule: str, detail: str, minutes: int, purge_recent: bool = False):
    guild = message.guild
    member = message.author
    try:
        await message.delete()
    except Exception:
        pass
    if purge_recent:
        try:
            mark_action("purge", message.channel.id)
            await message.channel.purge(
                limit=25,
                check=lambda m: m.author.id == member.id,
                after=discord.utils.utcnow() - timedelta(seconds=60)
            )
        except Exception:
            pass
    timed = ""
    if minutes > 0:
        try:
            mark_action("timeout", member.id)
            await member.timeout(discord.utils.utcnow() + timedelta(minutes=minutes), reason=f"AutoMod: {rule}")
            timed = f"\n**Timeout:** {minutes} minute(s)"
        except Exception:
            timed = "\n**Timeout:** failed (check bot permissions)"
    try:
        sent = await message.channel.send(view=InfoView(f"🛡️ AutoMod — {rule}", f"{member.mention}, {detail}", discord.Color.orange()))
        asyncio.create_task(delete_after(sent, 6))
    except Exception:
        pass
    await send_security(
        guild,
        f"🛡️ AutoMod — {rule}",
        f"**User:** {fmt_ref(member)}\n**Channel:** {message.channel.mention}\n**Detail:** {detail}{timed}\n\n**Content**\n{quote(message.content or '(empty)', 600)}",
        discord.Color.orange(),
        ping=False
    )


async def automod_check(message) -> bool:
    if not isinstance(message.author, discord.Member) or message.guild.id != GUILD_ID:
        return False
    if automod_exempt(message.author):
        return False
    content = message.content or ""
    link_cfg = sec_cfg("antilink")
    if link_cfg["enabled"]:
        if link_cfg["invites"] and INVITE_RE.search(content):
            await automod_punish(message, "Invite Link", "invite links are not allowed here.", link_cfg["timeout_minutes"])
            return True
        if link_cfg["links"] and LINK_RE.search(content):
            await automod_punish(message, "Link", "links are not allowed here.", link_cfg["timeout_minutes"])
            return True
    mention_cfg = sec_cfg("antimention")
    if mention_cfg["enabled"]:
        count = len({m.id for m in message.mentions}) + len(message.role_mentions)
        if message.mention_everyone and not message.author.guild_permissions.mention_everyone:
            count += mention_cfg["limit"]
        if count >= mention_cfg["limit"]:
            await automod_punish(message, "Mass Mention", f"mass mentions are not allowed ({count} mentions).", mention_cfg["timeout_minutes"])
            return True
    spam_cfg = sec_cfg("antispam")
    if spam_cfg["enabled"]:
        now = time.time()
        dq = spam_tracker.setdefault(message.author.id, deque())
        dq.append(now)
        while dq and now - dq[0] > spam_cfg["window"]:
            dq.popleft()
        if len(dq) >= spam_cfg["messages"]:
            dq.clear()
            await automod_punish(message, "Spam", f"slow down — {spam_cfg['messages']}+ messages in {spam_cfg['window']}s.", spam_cfg["timeout_minutes"], purge_recent=True)
            return True
    return False


async def security_prefix(message, cmd, args) -> bool:
    modules = ("antiraid", "antinuke", "antispam", "antilink", "antimention")
    if cmd not in modules + ("security", "panic", "whitelist", "lockdown", "unlockdown"):
        return False
    author = message.author
    if not author.guild_permissions.administrator:
        return True
    send = message.channel.send
    sub = args[1].lower() if len(args) > 1 else ""
    on_words = ("on", "enable", "true", "yes")
    off_words = ("off", "disable", "false", "no")
    if cmd == "security":
        if sub == "setup":
            await send(view=security_setup_view(message.guild))
            channel = message.channel_mentions[0] if message.channel_mentions else None
            if channel is not None:
                await send(view=await apply_log_channel("security", channel))
        else:
            await send(view=security_status_view(message.guild))
    elif cmd == "lockdown":
        await send(view=ok("🔒 Lockdown Started", "Locking every text channel for @everyone..."))
        count = await do_lockdown(message.guild, f"Lockdown by {author}")
        await send_security(message.guild, "🔒 Server Lockdown", f"**By:** {fmt_ref(author)}\n**Channels locked:** {count}", discord.Color.red(), ping=False)
        await send(view=ok("🔒 Lockdown Active", f"Locked **{count}** channel(s). Use `.unlockdown` to restore."))
    elif cmd == "unlockdown":
        await send(view=ok("🔓 Unlocking", "Restoring channel permissions..."))
        count = await undo_lockdown(message.guild)
        await send_security(message.guild, "🔓 Lockdown Ended", f"**By:** {fmt_ref(author)}\n**Channels restored:** {count}", discord.Color.green(), ping=False)
        await send(view=ok("🔓 Lockdown Ended", f"Restored **{count}** channel(s)."))
    elif cmd == "panic":
        if sub in on_words:
            await send(view=panic_view(True))
        elif sub in off_words:
            await send(view=panic_view(False))
        else:
            await send(view=err("Usage: `.panic <on|off>`"))
    elif cmd == "whitelist":
        if author.id != message.guild.owner_id:
            await send(view=err("Only the server owner can manage the whitelist."))
        elif sub not in ("add", "remove", "list"):
            await send(view=err("Usage: `.whitelist <add|remove|list> [@user]`"))
        else:
            target = message.mentions[0] if message.mentions else None
            await send(view=whitelist_view(message.guild, sub, target))
    else:
        if sub in on_words:
            await send(view=update_module(message.guild, cmd, True))
        elif sub in off_words:
            await send(view=update_module(message.guild, cmd, False))
        else:
            await send(view=err(f"Usage: `.{cmd} <on|off>` — fine-tune with `/security {cmd}`"))
    return True


async def do_lockdown(guild, reason: str) -> int:
    saved = config.setdefault("security", {}).setdefault("lockdown_channels", {})
    default = guild.default_role
    count = 0
    for ch in guild.text_channels:
        ow = ch.overwrites_for(default)
        key = str(ch.id)
        if key not in saved:
            saved[key] = ow.send_messages
        if ow.send_messages is False:
            continue
        ow.send_messages = False
        try:
            await ch.set_permissions(default, overwrite=ow, reason=reason)
            count += 1
        except Exception as e:
            print(f"Lockdown failed for {ch.id}: {e}")
        await asyncio.sleep(0.4)
    save_config(config)
    return count


async def undo_lockdown(guild) -> int:
    saved = config.setdefault("security", {}).setdefault("lockdown_channels", {})
    default = guild.default_role
    count = 0
    for key, original in list(saved.items()):
        ch = guild.get_channel(int(key))
        if ch is None:
            continue
        ow = ch.overwrites_for(default)
        ow.send_messages = original
        try:
            if ow.is_empty():
                await ch.set_permissions(default, overwrite=None, reason="Lockdown ended")
            else:
                await ch.set_permissions(default, overwrite=ow, reason="Lockdown ended")
            count += 1
        except Exception as e:
            print(f"Unlock failed for {key}: {e}")
        await asyncio.sleep(0.4)
    config["security"]["lockdown_channels"] = {}
    save_config(config)
    return count


def security_setup_view(guild) -> discord.ui.LayoutView:
    for name in SEC_DEFAULTS:
        sec_cfg(name)["enabled"] = True
    save_config(config)
    return security_status_view(guild)


GUILD_OBJ = discord.Object(id=GUILD_ID)


@tree.command(name="help", description="Show all commands", guild=discord.Object(id=GUILD_ID))
async def help_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(view=HelpView(interaction.user.id))


@tree.command(name="ping", description="Check bot latency", guild=discord.Object(id=GUILD_ID))
@discord.app_commands.default_permissions(administrator=True)
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(view=PingView(latency))


@tree.command(name="bypass", description="Bypass a link", guild=discord.Object(id=GUILD_ID))
@discord.app_commands.describe(link="The link to bypass")
async def bypass(interaction: discord.Interaction, link: str):
    await interaction.response.send_message(view=ProcessingView())
    msg = await interaction.original_response()
    await do_bypass(link, msg)


@tree.command(name="autobypass", description="Setup auto bypass channel", guild=discord.Object(id=GUILD_ID))
@discord.app_commands.describe(channel="Channel to watch for links")
@discord.app_commands.default_permissions(administrator=True)
async def autobypass(interaction: discord.Interaction, channel: discord.TextChannel):
    config["channel_id"] = channel.id
    save_config(config)
    await interaction.response.send_message(view=SetupView(channel))


@tree.command(name="say", description="Send a message as the bot", guild=discord.Object(id=GUILD_ID))
@discord.app_commands.describe(message="Message content")
@discord.app_commands.default_permissions(administrator=True)
async def say(interaction: discord.Interaction, message: str):
    try:
        await interaction.channel.send(message)
        await interaction.response.send_message(view=SayView(interaction.channel, message), ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(view=FailedView(str(e)), ephemeral=True)


@tree.command(name="kick", description="Kick a member", guild=discord.Object(id=GUILD_ID))
@discord.app_commands.describe(member="Member to kick", reason="Reason for the kick")
@discord.app_commands.default_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    try:
        mark_action("kick", member.id)
        await member.kick(reason=reason)
        await interaction.response.send_message(view=KickResultView(member, reason))
        await log_mod(interaction.guild, "Kick", interaction.user, member, reason)
    except discord.Forbidden:
        await interaction.response.send_message(view=FailedView("I don't have permission to kick that member."), ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(view=FailedView(str(e)), ephemeral=True)


@tree.command(name="ban", description="Ban a member", guild=discord.Object(id=GUILD_ID))
@discord.app_commands.describe(member="Member to ban", reason="Reason for the ban", delete_days="Days of messages to delete (0-7)")
@discord.app_commands.default_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided", delete_days: int = 0):
    delete_days = max(0, min(7, delete_days))
    try:
        mark_action("ban", member.id)
        await member.ban(reason=reason, delete_message_days=delete_days)
        await interaction.response.send_message(view=BanResultView(member, reason))
        await log_mod(interaction.guild, "Ban", interaction.user, member, reason, f"**Message History Deleted:** {delete_days} day(s)")
    except discord.Forbidden:
        await interaction.response.send_message(view=FailedView("I don't have permission to ban that member."), ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(view=FailedView(str(e)), ephemeral=True)


@tree.command(name="unban", description="Unban a user by ID", guild=discord.Object(id=GUILD_ID))
@discord.app_commands.describe(user_id="The ID of the user to unban")
@discord.app_commands.default_permissions(ban_members=True)
async def unban(interaction: discord.Interaction, user_id: str):
    try:
        uid = int(user_id)
        user = await bot.fetch_user(uid)
        mark_action("unban", uid)
        await interaction.guild.unban(user)
        await interaction.response.send_message(view=UnbanResultView(uid))
        await log_mod(interaction.guild, "Unban", interaction.user, user)
    except discord.NotFound:
        await interaction.response.send_message(view=FailedView("User not found or not banned."), ephemeral=True)
    except ValueError:
        await interaction.response.send_message(view=FailedView("Invalid user ID."), ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(view=FailedView(str(e)), ephemeral=True)


@tree.command(name="purge", description="Delete multiple messages", guild=discord.Object(id=GUILD_ID))
@discord.app_commands.describe(amount="Number of messages to delete (1-100)")
@discord.app_commands.default_permissions(manage_messages=True)
async def purge(interaction: discord.Interaction, amount: int):
    amount = max(1, min(100, amount))
    try:
        await interaction.response.defer(ephemeral=True)
        mark_action("purge", interaction.channel.id)
        deleted = await interaction.channel.purge(limit=amount)
        msg = await interaction.followup.send(view=PurgeResultView(len(deleted), interaction.channel), ephemeral=False)
        await log_mod(interaction.guild, "Purge", interaction.user, interaction.channel, extra=f"**Deleted:** {len(deleted)} message(s)")
        asyncio.create_task(delete_after(msg, 5))
    except discord.Forbidden:
        await interaction.followup.send(view=FailedView("I don't have permission to delete messages here."), ephemeral=True)
    except Exception as e:
        await interaction.followup.send(view=FailedView(str(e)), ephemeral=True)


@tree.command(name="timeout", description="Timeout a member", guild=discord.Object(id=GUILD_ID))
@discord.app_commands.describe(member="Member to timeout", minutes="Duration in minutes", reason="Reason for the timeout")
@discord.app_commands.default_permissions(moderate_members=True)
async def timeout_cmd(interaction: discord.Interaction, member: discord.Member, minutes: int, reason: str = "No reason provided"):
    minutes = max(1, min(40320, minutes))
    try:
        until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        mark_action("timeout", member.id)
        await member.timeout(until, reason=reason)
        await interaction.response.send_message(view=TimeoutResultView(member, minutes, reason))
        await log_mod(interaction.guild, "Timeout", interaction.user, member, reason, f"**Duration:** {minutes} minute(s)\n**Until:** <t:{int(until.timestamp())}:F>")
    except discord.Forbidden:
        await interaction.response.send_message(view=FailedView("I don't have permission to timeout that member."), ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(view=FailedView(str(e)), ephemeral=True)


@tree.command(name="untimeout", description="Remove timeout from a member", guild=discord.Object(id=GUILD_ID))
@discord.app_commands.describe(member="Member to remove timeout from")
@discord.app_commands.default_permissions(moderate_members=True)
async def untimeout(interaction: discord.Interaction, member: discord.Member):
    try:
        mark_action("untimeout", member.id)
        await member.timeout(None)
        await interaction.response.send_message(view=UntimeoutResultView(member))
        await log_mod(interaction.guild, "Untimeout", interaction.user, member)
    except discord.Forbidden:
        await interaction.response.send_message(view=FailedView("I don't have permission to remove that timeout."), ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(view=FailedView(str(e)), ephemeral=True)


@tree.command(name="warn", description="Warn a member", guild=discord.Object(id=GUILD_ID))
@discord.app_commands.describe(member="Member to warn", reason="Reason for the warning")
@discord.app_commands.default_permissions(moderate_members=True)
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    if "warns" not in config:
        config["warns"] = {}
    uid = str(member.id)
    if uid not in config["warns"]:
        config["warns"][uid] = []
    config["warns"][uid].append({
        "reason": reason,
        "timestamp": int(datetime.now(timezone.utc).timestamp()),
        "moderator_id": interaction.user.id
    })
    save_config(config)
    total = len(config["warns"][uid])
    await interaction.response.send_message(view=WarnAddedView(member, reason, total))
    await log_mod(interaction.guild, "Warn", interaction.user, member, reason, f"**Total Warns:** {total}")


@tree.command(name="warns", description="View warnings for a member", guild=discord.Object(id=GUILD_ID))
@discord.app_commands.describe(member="Member to check warns for")
@discord.app_commands.default_permissions(moderate_members=True)
async def warns(interaction: discord.Interaction, member: discord.Member):
    warns_data = config.get("warns", {}).get(str(member.id), [])
    await interaction.response.send_message(view=WarnsListView(member, warns_data))


@tree.command(name="clearwarns", description="Clear all warnings for a member", guild=discord.Object(id=GUILD_ID))
@discord.app_commands.describe(member="Member to clear warns for")
@discord.app_commands.default_permissions(administrator=True)
async def clearwarns(interaction: discord.Interaction, member: discord.Member):
    if "warns" in config and str(member.id) in config["warns"]:
        config["warns"][str(member.id)] = []
        save_config(config)
    await interaction.response.send_message(view=WarnsCleared(member))
    await log_mod(interaction.guild, "Clear Warns", interaction.user, member)


@tree.command(name="slowmode", description="Set slowmode in a channel", guild=discord.Object(id=GUILD_ID))
@discord.app_commands.describe(seconds="Slowmode delay in seconds (0 to disable)", channel="Channel to set slowmode in (defaults to current)")
@discord.app_commands.default_permissions(manage_channels=True)
async def slowmode(interaction: discord.Interaction, seconds: int, channel: discord.TextChannel = None):
    target = channel or interaction.channel
    seconds = max(0, min(21600, seconds))
    try:
        await target.edit(slowmode_delay=seconds)
        await interaction.response.send_message(view=SlowmodeView(target, seconds))
        await log_mod(interaction.guild, "Slowmode", interaction.user, target, extra=f"**Delay:** {seconds}s")
    except discord.Forbidden:
        await interaction.response.send_message(view=FailedView("I don't have permission to edit that channel."), ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(view=FailedView(str(e)), ephemeral=True)


@tree.command(name="lock", description="Lock a channel", guild=discord.Object(id=GUILD_ID))
@discord.app_commands.describe(channel="Channel to lock (defaults to current)")
@discord.app_commands.default_permissions(manage_channels=True)
async def lock(interaction: discord.Interaction, channel: discord.TextChannel = None):
    target = channel or interaction.channel
    overwrite = target.overwrites_for(interaction.guild.default_role)
    overwrite.send_messages = False
    try:
        await target.set_permissions(interaction.guild.default_role, overwrite=overwrite)
        await interaction.response.send_message(view=LockView(target, True))
        await log_mod(interaction.guild, "Lock", interaction.user, target)
    except discord.Forbidden:
        await interaction.response.send_message(view=FailedView("I don't have permission to lock that channel."), ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(view=FailedView(str(e)), ephemeral=True)


@tree.command(name="unlock", description="Unlock a channel", guild=discord.Object(id=GUILD_ID))
@discord.app_commands.describe(channel="Channel to unlock (defaults to current)")
@discord.app_commands.default_permissions(manage_channels=True)
async def unlock(interaction: discord.Interaction, channel: discord.TextChannel = None):
    target = channel or interaction.channel
    overwrite = target.overwrites_for(interaction.guild.default_role)
    overwrite.send_messages = True
    try:
        await target.set_permissions(interaction.guild.default_role, overwrite=overwrite)
        await interaction.response.send_message(view=LockView(target, False))
        await log_mod(interaction.guild, "Unlock", interaction.user, target)
    except discord.Forbidden:
        await interaction.response.send_message(view=FailedView("I don't have permission to unlock that channel."), ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(view=FailedView(str(e)), ephemeral=True)


@tree.command(name="afk", description="Set your AFK status", guild=discord.Object(id=GUILD_ID))
@discord.app_commands.describe(reason="Reason for AFK")
async def afk_cmd(interaction: discord.Interaction, reason: str = "AFK"):
    if "afk" not in config:
        config["afk"] = {}
    config["afk"][str(interaction.user.id)] = {
        "reason": reason,
        "timestamp": int(datetime.now(timezone.utc).timestamp())
    }
    save_config(config)
    await interaction.response.send_message(view=AfkSetView(interaction.user, reason))


@tree.command(name="snipe", description="View the last deleted message in this channel", guild=discord.Object(id=GUILD_ID))
async def snipe_cmd(interaction: discord.Interaction):
    data = last_deleted.get(interaction.channel.id)
    if not data:
        await interaction.response.send_message(view=SnipeEmptyView())
    else:
        await interaction.response.send_message(view=SnipeView(data))


@tree.command(name="avatar", description="View a user's avatar", guild=discord.Object(id=GUILD_ID))
@discord.app_commands.describe(member="Member to view avatar of")
async def avatar_cmd(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    embed = discord.Embed(title=f"{target.display_name}'s Avatar", color=discord.Color.blurple())
    embed.set_image(url=str(target.display_avatar.url))
    await interaction.response.send_message(embed=embed)


@tree.command(name="steal", description="Steal a custom emoji into this server", guild=discord.Object(id=GUILD_ID))
@discord.app_commands.describe(emoji="The custom emoji to steal")
@discord.app_commands.default_permissions(manage_emojis_and_stickers=True)
async def steal_cmd(interaction: discord.Interaction, emoji: str):
    await interaction.response.defer(ephemeral=True)
    view = await steal_emoji(interaction.guild, emoji, f"Stolen by {interaction.user}")
    await interaction.followup.send(view=view, ephemeral=True)


@tree.command(name="scatter", description="Play scatter slots with virtual coins", guild=discord.Object(id=GUILD_ID))
@discord.app_commands.describe(bet="Coins to bet")
async def scatter_cmd(interaction: discord.Interaction, bet: int):
    await interaction.response.send_message(view=scatter_result(interaction.user.id, bet))


def register_simple_economy(name, desc, fn):
    async def cb(interaction: discord.Interaction):
        await interaction.response.send_message(view=fn(interaction.user, interaction.guild))
    tree.command(name=name, description=desc, guild=GUILD_OBJ)(cb)


for _name, (_desc, _fn) in SIMPLE_ECONOMY.items():
    register_simple_economy(_name, _desc, _fn)

ITEM_CHOICES = [discord.app_commands.Choice(name=v["name"], value=k) for k, v in SHOP.items()]


@tree.command(name="balance", description="Check your (or someone's) balance", guild=GUILD_OBJ)
@discord.app_commands.describe(member="Member to check")
async def balance_cmd(interaction: discord.Interaction, member: discord.Member = None):
    await interaction.response.send_message(view=balance_view(member or interaction.user))


@tree.command(name="rob", description="Try to rob another member's wallet", guild=GUILD_OBJ)
@discord.app_commands.describe(member="Who to rob")
async def rob_cmd(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.send_message(view=rob_view(interaction.user, member))


@tree.command(name="deposit", description="Deposit coins into your bank", guild=GUILD_OBJ)
@discord.app_commands.describe(amount="Amount, or all / half / 5k")
async def deposit_cmd(interaction: discord.Interaction, amount: str):
    await interaction.response.send_message(view=deposit_view(interaction.user, amount))


@tree.command(name="withdraw", description="Withdraw coins from your bank", guild=GUILD_OBJ)
@discord.app_commands.describe(amount="Amount, or all / half / 5k")
async def withdraw_cmd(interaction: discord.Interaction, amount: str):
    await interaction.response.send_message(view=withdraw_view(interaction.user, amount))


@tree.command(name="give", description="Give coins to another member", guild=GUILD_OBJ)
@discord.app_commands.describe(member="Who to give coins to", amount="Amount, or all / half / 5k")
async def give_cmd(interaction: discord.Interaction, member: discord.Member, amount: str):
    await interaction.response.send_message(view=give_view(interaction.user, member, amount))


@tree.command(name="buy", description="Buy an item from the shop", guild=GUILD_OBJ)
@discord.app_commands.describe(item="Item to buy", amount="How many (default 1)")
@discord.app_commands.choices(item=ITEM_CHOICES)
async def buy_cmd(interaction: discord.Interaction, item: discord.app_commands.Choice[str], amount: int = 1):
    await interaction.response.send_message(view=buy_view(interaction.user, item.value, amount))


@tree.command(name="sell", description="Sell an item for 50% of its price", guild=GUILD_OBJ)
@discord.app_commands.describe(item="Item to sell", amount="How many (default 1)")
@discord.app_commands.choices(item=ITEM_CHOICES)
async def sell_cmd(interaction: discord.Interaction, item: discord.app_commands.Choice[str], amount: int = 1):
    await interaction.response.send_message(view=sell_view(interaction.user, item.value, amount))


@tree.command(name="coinflip", description="Flip a coin and double your bet", guild=GUILD_OBJ)
@discord.app_commands.describe(bet="Amount, or all / half / 5k", side="Heads or tails")
@discord.app_commands.choices(side=[
    discord.app_commands.Choice(name="Heads", value="heads"),
    discord.app_commands.Choice(name="Tails", value="tails")
])
async def coinflip_cmd(interaction: discord.Interaction, bet: str, side: discord.app_commands.Choice[str]):
    await interaction.response.send_message(view=coinflip_view(interaction.user, bet, side.value))


@tree.command(name="addcoins", description="[Admin] Add coins to a member", guild=GUILD_OBJ)
@discord.app_commands.default_permissions(administrator=True)
async def addcoins_cmd(interaction: discord.Interaction, member: discord.Member, amount: int):
    if amount <= 0:
        await interaction.response.send_message(view=err("Amount must be positive."), ephemeral=True)
        return
    await interaction.response.send_message(view=eco_admin_view("add", member, amount, interaction.user))


@tree.command(name="removecoins", description="[Admin] Remove coins from a member", guild=GUILD_OBJ)
@discord.app_commands.default_permissions(administrator=True)
async def removecoins_cmd(interaction: discord.Interaction, member: discord.Member, amount: int):
    if amount <= 0:
        await interaction.response.send_message(view=err("Amount must be positive."), ephemeral=True)
        return
    await interaction.response.send_message(view=eco_admin_view("remove", member, amount, interaction.user))


@tree.command(name="setcoins", description="[Admin] Set a member's wallet", guild=GUILD_OBJ)
@discord.app_commands.default_permissions(administrator=True)
async def setcoins_cmd(interaction: discord.Interaction, member: discord.Member, amount: int):
    if amount < 0:
        await interaction.response.send_message(view=err("Amount can't be negative."), ephemeral=True)
        return
    await interaction.response.send_message(view=eco_admin_view("set", member, amount, interaction.user))


@tree.command(name="resetcoins", description="[Admin] Reset a member's economy data", guild=GUILD_OBJ)
@discord.app_commands.default_permissions(administrator=True)
async def resetcoins_cmd(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.send_message(view=eco_admin_view("reset", member, 0, interaction.user))


verify_group = discord.app_commands.Group(
    name="verify",
    description="Verification commands",
    guild_ids=[GUILD_ID],
    default_permissions=discord.Permissions(administrator=True)
)


@verify_group.command(name="setup", description="Setup verification")
@discord.app_commands.describe(
    role="Role to assign when a user verifies",
    channel="Channel where the verify message will be sent",
    log="Channel where verifications will be logged"
)
async def verify_setup(interaction: discord.Interaction, role: discord.Role, channel: discord.TextChannel, log: discord.TextChannel):
    config["verify_role_id"] = role.id
    config["verify_log_id"] = log.id
    save_config(config)
    await channel.send(view=VerifyLayoutView())
    await interaction.response.send_message(
        f"Verification setup complete.\nVerify channel: {channel.mention}\nRole: {role.mention}\nLog: {log.mention}",
        ephemeral=True
    )


tree.add_command(verify_group)

giveaway_group = discord.app_commands.Group(
    name="giveaway",
    description="Giveaway commands",
    guild_ids=[GUILD_ID],
    default_permissions=discord.Permissions(administrator=True)
)


@giveaway_group.command(name="start", description="Start a giveaway")
@discord.app_commands.describe(channel="Channel for the giveaway", duration="Duration e.g. 1h, 30m, 2d, 1h30m", prize="What you're giving away", winners="Number of winners")
async def giveaway_start(interaction: discord.Interaction, channel: discord.TextChannel, duration: str, prize: str, winners: int = 1):
    seconds = parse_duration(duration)
    if seconds <= 0:
        await interaction.response.send_message(view=FailedView("Invalid duration. Use format like `1h`, `30m`, `2d`, `1h30m`."), ephemeral=True)
        return
    end_ts = int((datetime.now(timezone.utc) + timedelta(seconds=seconds)).timestamp())
    msg = await channel.send(view=GiveawayActiveView(prize, winners, end_ts, 0))
    if "giveaways" not in config:
        config["giveaways"] = {}
    config["giveaways"][str(msg.id)] = {
        "channel_id": channel.id,
        "prize": prize,
        "winner_count": winners,
        "end_timestamp": end_ts,
        "entries": [],
        "ended": False,
        "winners": []
    }
    save_config(config)
    await interaction.response.send_message(view=GiveawayStartedView(channel, prize, end_ts), ephemeral=True)
    await log_bot(
        interaction.guild,
        "🎉 Giveaway Started",
        f"**By:** {fmt_ref(interaction.user)}\n**Channel:** {channel.mention}\n**Prize:** {prize}\n**Winners:** {winners}\n**Ends:** <t:{end_ts}:R>",
        discord.Color.gold()
    )


@giveaway_group.command(name="end", description="Force end a giveaway early")
@discord.app_commands.describe(message_id="Message ID of the giveaway to end")
async def giveaway_end(interaction: discord.Interaction, message_id: str):
    giveaways = config.get("giveaways", {})
    if message_id not in giveaways:
        await interaction.response.send_message(view=FailedView("Giveaway not found."), ephemeral=True)
        return
    gw = giveaways[message_id]
    if gw.get("ended"):
        await interaction.response.send_message(view=FailedView("Giveaway already ended."), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await end_giveaway(message_id, gw)
    await interaction.followup.send("Giveaway ended.", ephemeral=True)


@giveaway_group.command(name="reroll", description="Reroll the winner of an ended giveaway")
@discord.app_commands.describe(message_id="Message ID of the ended giveaway")
async def giveaway_reroll(interaction: discord.Interaction, message_id: str):
    giveaways = config.get("giveaways", {})
    if message_id not in giveaways:
        await interaction.response.send_message(view=FailedView("Giveaway not found."), ephemeral=True)
        return
    gw = giveaways[message_id]
    if not gw.get("ended"):
        await interaction.response.send_message(view=FailedView("Giveaway hasn't ended yet."), ephemeral=True)
        return
    entries = gw["entries"]
    if not entries:
        await interaction.response.send_message(view=FailedView("No entries to reroll from."), ephemeral=True)
        return
    picked = random.sample(entries, min(gw["winner_count"], len(entries)))
    gw["winners"] = picked
    save_config(config)
    channel = bot.get_channel(gw["channel_id"])
    winner_text = " ".join(f"<@{uid}>" for uid in picked)
    if channel:
        try:
            original = await channel.fetch_message(int(message_id))
            await channel.send(f"🎉 Giveaway rerolled! **{gw['prize']}**\nNew winner(s): {winner_text}", reference=original)
        except Exception:
            pass
    await interaction.response.send_message(view=GiveawayRerollView(picked), ephemeral=True)
    await log_bot(
        interaction.guild,
        "🎉 Giveaway Rerolled",
        f"**By:** {fmt_ref(interaction.user)}\n**Prize:** {gw['prize']}\n**New winner(s):** {winner_text}",
        discord.Color.gold()
    )


tree.add_command(giveaway_group)

logs_group = discord.app_commands.Group(
    name="logs",
    description="Log system commands",
    guild_ids=[GUILD_ID],
    default_permissions=discord.Permissions(administrator=True)
)

LOG_CATEGORY_CHOICES = [discord.app_commands.Choice(name=label, value=key) for key, label in LOG_CATEGORIES.items()]


@logs_group.command(name="set", description="Set the channel for a log category")
@discord.app_commands.describe(category="Which logs to send", channel="Channel that receives the logs")
@discord.app_commands.choices(category=LOG_CATEGORY_CHOICES)
async def logs_set(interaction: discord.Interaction, category: discord.app_commands.Choice[str], channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    view = await apply_log_channel(category.value, channel)
    await interaction.followup.send(view=view, ephemeral=True)


@logs_group.command(name="all", description="Send every log category to one channel")
@discord.app_commands.describe(channel="Channel that receives all logs")
async def logs_all(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    view = await apply_log_channel("all", channel)
    await interaction.followup.send(view=view, ephemeral=True)


@logs_group.command(name="disable", description="Turn off a log category")
@discord.app_commands.describe(category="Which logs to turn off")
@discord.app_commands.choices(category=LOG_CATEGORY_CHOICES + [discord.app_commands.Choice(name="All", value="all")])
async def logs_disable(interaction: discord.Interaction, category: discord.app_commands.Choice[str]):
    await interaction.response.send_message(view=disable_log_channel(category.value), ephemeral=True)


@logs_group.command(name="status", description="Show where each log category is sent")
async def logs_status(interaction: discord.Interaction):
    await interaction.response.send_message(view=logs_status_view(interaction.guild), ephemeral=True)


tree.add_command(logs_group)

security_group = discord.app_commands.Group(
    name="security",
    description="Security system (anti-raid, anti-nuke, automod)",
    guild_ids=[GUILD_ID],
    default_permissions=discord.Permissions(administrator=True)
)

SEC_ACTION_RAID = [
    discord.app_commands.Choice(name="Kick", value="kick"),
    discord.app_commands.Choice(name="Ban", value="ban"),
    discord.app_commands.Choice(name="Timeout (1h)", value="timeout"),
]
SEC_ACTION_NUKE = [
    discord.app_commands.Choice(name="Strip roles", value="strip"),
    discord.app_commands.Choice(name="Kick", value="kick"),
    discord.app_commands.Choice(name="Ban", value="ban"),
]


async def deny_unless_admin(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return False
    await interaction.response.send_message(view=err("Administrator permission required."), ephemeral=True)
    return True


@security_group.command(name="status", description="Show the security system status")
async def sec_status(interaction: discord.Interaction):
    if await deny_unless_admin(interaction):
        return
    await interaction.response.send_message(view=security_status_view(interaction.guild), ephemeral=True)


@security_group.command(name="antiraid", description="Configure anti-raid")
@discord.app_commands.describe(
    enabled="Turn anti-raid on or off",
    threshold="Joins within the window that trigger raid mode",
    seconds="Join-detection window in seconds",
    action="What to do with raiders",
    min_account_age="Action accounts younger than this many days (0 = off)",
    duration="How long raid mode lasts in seconds"
)
@discord.app_commands.choices(action=SEC_ACTION_RAID)
async def sec_antiraid(
    interaction: discord.Interaction,
    enabled: bool,
    threshold: int = None,
    seconds: int = None,
    action: discord.app_commands.Choice[str] = None,
    min_account_age: int = None,
    duration: int = None
):
    if await deny_unless_admin(interaction):
        return
    view = update_module(
        interaction.guild, "antiraid", enabled,
        threshold=threshold, window=seconds, action=action.value if action else None,
        min_age_days=min_account_age, duration=duration
    )
    await interaction.response.send_message(view=view, ephemeral=True)


@security_group.command(name="panic", description="Instantly action every new member until turned off")
@discord.app_commands.describe(enabled="Panic mode on or off")
async def sec_panic(interaction: discord.Interaction, enabled: bool):
    if await deny_unless_admin(interaction):
        return
    await interaction.response.send_message(view=panic_view(enabled), ephemeral=True)


@security_group.command(name="antinuke", description="Configure anti-nuke")
@discord.app_commands.describe(
    enabled="Turn anti-nuke on or off",
    limit="Channel/role/webhook actions allowed per window",
    member_limit="Bans/kicks allowed per window",
    seconds="Detection window in seconds",
    action="Punishment for the offender",
    recover="Restore deleted channels/roles and undo the attack",
    guard_perms="Revert dangerous permission grants by untrusted users"
)
@discord.app_commands.choices(action=SEC_ACTION_NUKE)
async def sec_antinuke(
    interaction: discord.Interaction,
    enabled: bool,
    limit: int = None,
    member_limit: int = None,
    seconds: int = None,
    action: discord.app_commands.Choice[str] = None,
    recover: bool = None,
    guard_perms: bool = None
):
    if await deny_unless_admin(interaction):
        return
    view = update_module(
        interaction.guild, "antinuke", enabled,
        limit=limit, member_limit=member_limit, window=seconds,
        action=action.value if action else None, recover=recover, guard_perms=guard_perms
    )
    await interaction.response.send_message(view=view, ephemeral=True)


@security_group.command(name="antispam", description="Configure anti-spam")
@discord.app_commands.describe(
    enabled="Turn anti-spam on or off",
    messages="Messages allowed within the window",
    seconds="Window in seconds",
    timeout_minutes="Timeout length in minutes (0 = delete only)"
)
async def sec_antispam(interaction: discord.Interaction, enabled: bool, messages: int = None, seconds: int = None, timeout_minutes: int = None):
    if await deny_unless_admin(interaction):
        return
    view = update_module(interaction.guild, "antispam", enabled, messages=messages, window=seconds, timeout_minutes=timeout_minutes)
    await interaction.response.send_message(view=view, ephemeral=True)


@security_group.command(name="antilink", description="Configure anti-link")
@discord.app_commands.describe(
    enabled="Turn anti-link on or off",
    invites="Block Discord invite links",
    links="Block all links",
    timeout_minutes="Timeout length in minutes (0 = delete only)"
)
async def sec_antilink(interaction: discord.Interaction, enabled: bool, invites: bool = None, links: bool = None, timeout_minutes: int = None):
    if await deny_unless_admin(interaction):
        return
    view = update_module(interaction.guild, "antilink", enabled, invites=invites, links=links, timeout_minutes=timeout_minutes)
    await interaction.response.send_message(view=view, ephemeral=True)


@security_group.command(name="antimention", description="Configure anti-mass-mention")
@discord.app_commands.describe(
    enabled="Turn anti-mass-mention on or off",
    limit="Mentions in one message that trigger it",
    timeout_minutes="Timeout length in minutes (0 = delete only)"
)
async def sec_antimention(interaction: discord.Interaction, enabled: bool, limit: int = None, timeout_minutes: int = None):
    if await deny_unless_admin(interaction):
        return
    view = update_module(interaction.guild, "antimention", enabled, limit=limit, timeout_minutes=timeout_minutes)
    await interaction.response.send_message(view=view, ephemeral=True)


@security_group.command(name="whitelist", description="Manage trusted users (server owner only)")
@discord.app_commands.describe(action="Add, remove or list", member="Member to add or remove")
@discord.app_commands.choices(action=[
    discord.app_commands.Choice(name="Add", value="add"),
    discord.app_commands.Choice(name="Remove", value="remove"),
    discord.app_commands.Choice(name="List", value="list"),
])
async def sec_whitelist(interaction: discord.Interaction, action: discord.app_commands.Choice[str], member: discord.Member = None):
    if interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message(view=err("Only the server owner can manage the whitelist."), ephemeral=True)
        return
    await interaction.response.send_message(view=whitelist_view(interaction.guild, action.value, member), ephemeral=True)


@security_group.command(name="setup", description="Turn on every security module with default settings")
@discord.app_commands.describe(log_channel="Channel for security alerts (optional)")
async def sec_setup(interaction: discord.Interaction, log_channel: discord.TextChannel = None):
    if await deny_unless_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(view=security_setup_view(interaction.guild), ephemeral=True)
    if log_channel is not None:
        await interaction.followup.send(view=await apply_log_channel("security", log_channel), ephemeral=True)


@security_group.command(name="lockdown", description="Lock every text channel for @everyone")
async def sec_lockdown(interaction: discord.Interaction):
    if await deny_unless_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    count = await do_lockdown(interaction.guild, f"Lockdown by {interaction.user}")
    await send_security(interaction.guild, "🔒 Server Lockdown", f"**By:** {fmt_ref(interaction.user)}\n**Channels locked:** {count}", discord.Color.red(), ping=False)
    await interaction.followup.send(view=ok("🔒 Lockdown Active", f"Locked **{count}** channel(s). Use `/security unlockdown` to restore."), ephemeral=True)


@security_group.command(name="unlockdown", description="Restore channel permissions after a lockdown")
async def sec_unlockdown(interaction: discord.Interaction):
    if await deny_unless_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    count = await undo_lockdown(interaction.guild)
    await send_security(interaction.guild, "🔓 Lockdown Ended", f"**By:** {fmt_ref(interaction.user)}\n**Channels restored:** {count}", discord.Color.green(), ping=False)
    await interaction.followup.send(view=ok("🔓 Lockdown Ended", f"Restored **{count}** channel(s)."), ephemeral=True)


tree.add_command(security_group)


@bot.event
async def on_message_delete(message):
    if message.author.bot:
        return
    if message.content:
        last_deleted[message.channel.id] = {
            "content": message.content,
            "author_name": str(message.author),
            "author_id": message.author.id,
            "timestamp": int(message.created_at.timestamp())
        }
    if message.guild and not message.content.lower().startswith((f"{PREFIX}purge", f"{PREFIX}say")):
        await log_message_delete(message)


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.guild and message.mentions:
        afk_data = config.get("afk", {})
        for mentioned in message.mentions:
            uid = str(mentioned.id)
            if uid in afk_data:
                afk = afk_data[uid]
                sent = await message.channel.send(
                    view=AfkMentionView(mentioned, afk["reason"], afk["timestamp"])
                )
                asyncio.create_task(delete_after(sent, 10))

    if message.guild:
        afk_data = config.get("afk", {})
        uid = str(message.author.id)
        content_lower = message.content.lower().strip()
        is_afk_cmd = content_lower == f"{PREFIX}afk" or content_lower.startswith(f"{PREFIX}afk ")
        if uid in afk_data and not is_afk_cmd:
            del config["afk"][uid]
            save_config(config)
            sent = await message.channel.send(view=AfkReturnView(message.author))
            asyncio.create_task(delete_after(sent, 5))

    if message.attachments:
        for attachment in message.attachments:
            if await check_scam_image(attachment):
                await message.delete()
                try:
                    mark_action("kick", message.author.id)
                    await message.author.kick(reason="MrBeast scam auto detected")
                    await log_mod(message.guild, "Auto Kick", bot.user, message.author, "MrBeast scam auto detected", f"**Channel:** {message.channel.mention}")
                except Exception as e:
                    print(f"Kick failed: {e}")
                sent = await message.channel.send(view=UserKickedView(message.author))
                asyncio.create_task(delete_after(sent, 60))
                return

    if message.guild and await automod_check(message):
        return

    if not message.content.startswith(PREFIX):
        if message.guild and await ar_check(message):
            return
        bypass_channel_id = config.get("channel_id")
        if (
            bypass_channel_id
            and message.guild
            and message.guild.id == GUILD_ID
            and message.channel.id == bypass_channel_id
        ):
            urls = [w for w in message.content.split() if w.startswith("http://") or w.startswith("https://")]
            for url in urls:
                msg = await message.reply(view=ProcessingView())
                await do_bypass(url, msg)
        return

    if not message.guild or message.guild.id != GUILD_ID:
        return

    args = message.content[len(PREFIX):].split()
    if not args:
        return
    cmd = args[0].lower()

    if await security_prefix(message, cmd, args):
        return

    if await logs_prefix(message, cmd, args):
        return

    if await economy_prefix(message, cmd, args):
        return

    if cmd == "help":
        await message.channel.send(view=HelpView(message.author.id))

    elif cmd == "ping":
        latency = round(bot.latency * 1000)
        await message.channel.send(view=PingView(latency))

    elif cmd == "bypass":
        if len(args) < 2:
            await message.channel.send(view=FailedView("Usage: `.bypass <link>`"))
            return
        url = args[1]
        msg = await message.channel.send(view=ProcessingView())
        await do_bypass(url, msg)

    elif cmd == "say":
        if not message.author.guild_permissions.administrator:
            return
        content = message.content[len(PREFIX) + len("say"):].strip()
        if not content:
            await message.channel.send(view=FailedView("Usage: `.say <message>`"))
            return
        try:
            await message.delete()
        except Exception:
            pass
        await message.channel.send(content)

    elif cmd == "embed":
        if not message.author.guild_permissions.manage_messages:
            return
        await message.channel.send(view=EmbedBuilderView(message.author.id))

    elif cmd in ("autoresponder", "ar"):
        if not message.author.guild_permissions.manage_guild:
            return
        await ar_prefix(message)

    elif cmd == "kick":
        if not message.author.guild_permissions.kick_members:
            return
        if not message.mentions:
            await message.channel.send(view=FailedView("Usage: `.kick @user [reason]`"))
            return
        member = message.mentions[0]
        reason = " ".join(args[2:]) if len(args) > 2 else "No reason provided"
        try:
            mark_action("kick", member.id)
            await member.kick(reason=reason)
            await message.channel.send(view=KickResultView(member, reason))
            await log_mod(message.guild, "Kick", message.author, member, reason)
        except discord.Forbidden:
            await message.channel.send(view=FailedView("I don't have permission to kick that member."))
        except Exception as e:
            await message.channel.send(view=FailedView(str(e)))

    elif cmd == "ban":
        if not message.author.guild_permissions.ban_members:
            return
        if not message.mentions:
            await message.channel.send(view=FailedView("Usage: `.ban @user [reason]`"))
            return
        member = message.mentions[0]
        reason = " ".join(args[2:]) if len(args) > 2 else "No reason provided"
        try:
            mark_action("ban", member.id)
            await member.ban(reason=reason, delete_message_days=0)
            await message.channel.send(view=BanResultView(member, reason))
            await log_mod(message.guild, "Ban", message.author, member, reason)
        except discord.Forbidden:
            await message.channel.send(view=FailedView("I don't have permission to ban that member."))
        except Exception as e:
            await message.channel.send(view=FailedView(str(e)))

    elif cmd == "unban":
        if not message.author.guild_permissions.ban_members:
            return
        if len(args) < 2:
            await message.channel.send(view=FailedView("Usage: `.unban <userID>`"))
            return
        try:
            uid = int(args[1])
            user = await bot.fetch_user(uid)
            mark_action("unban", uid)
            await message.guild.unban(user)
            await message.channel.send(view=UnbanResultView(uid))
            await log_mod(message.guild, "Unban", message.author, user)
        except discord.NotFound:
            await message.channel.send(view=FailedView("User not found or not banned."))
        except ValueError:
            await message.channel.send(view=FailedView("Invalid user ID."))
        except Exception as e:
            await message.channel.send(view=FailedView(str(e)))

    elif cmd == "purge":
        if not message.author.guild_permissions.manage_messages:
            return
        if len(args) < 2:
            await message.channel.send(view=FailedView("Usage: `.purge <amount>`"))
            return
        try:
            amount = max(1, min(100, int(args[1])))
            await message.delete()
            mark_action("purge", message.channel.id)
            deleted = await message.channel.purge(limit=amount)
            msg = await message.channel.send(view=PurgeResultView(len(deleted), message.channel))
            await log_mod(message.guild, "Purge", message.author, message.channel, extra=f"**Deleted:** {len(deleted)} message(s)")
            asyncio.create_task(delete_after(msg, 5))
        except Exception as e:
            await message.channel.send(view=FailedView(str(e)))

    elif cmd == "timeout":
        if not message.author.guild_permissions.moderate_members:
            return
        if not message.mentions or len(args) < 3:
            await message.channel.send(view=FailedView("Usage: `.timeout @user <minutes> [reason]`"))
            return
        member = message.mentions[0]
        try:
            minutes = max(1, min(40320, int(args[2])))
            reason = " ".join(args[3:]) if len(args) > 3 else "No reason provided"
            until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
            mark_action("timeout", member.id)
            await member.timeout(until, reason=reason)
            await message.channel.send(view=TimeoutResultView(member, minutes, reason))
            await log_mod(message.guild, "Timeout", message.author, member, reason, f"**Duration:** {minutes} minute(s)\n**Until:** <t:{int(until.timestamp())}:F>")
        except discord.Forbidden:
            await message.channel.send(view=FailedView("I don't have permission to timeout that member."))
        except Exception as e:
            await message.channel.send(view=FailedView(str(e)))

    elif cmd == "untimeout":
        if not message.author.guild_permissions.moderate_members:
            return
        if not message.mentions:
            await message.channel.send(view=FailedView("Usage: `.untimeout @user`"))
            return
        member = message.mentions[0]
        try:
            mark_action("untimeout", member.id)
            await member.timeout(None)
            await message.channel.send(view=UntimeoutResultView(member))
            await log_mod(message.guild, "Untimeout", message.author, member)
        except discord.Forbidden:
            await message.channel.send(view=FailedView("I don't have permission to remove that timeout."))
        except Exception as e:
            await message.channel.send(view=FailedView(str(e)))

    elif cmd == "warn":
        if not message.author.guild_permissions.moderate_members:
            return
        if not message.mentions:
            await message.channel.send(view=FailedView("Usage: `.warn @user [reason]`"))
            return
        member = message.mentions[0]
        reason = " ".join(args[2:]) if len(args) > 2 else "No reason provided"
        if "warns" not in config:
            config["warns"] = {}
        uid = str(member.id)
        if uid not in config["warns"]:
            config["warns"][uid] = []
        config["warns"][uid].append({
            "reason": reason,
            "timestamp": int(datetime.now(timezone.utc).timestamp()),
            "moderator_id": message.author.id
        })
        save_config(config)
        total = len(config["warns"][uid])
        await message.channel.send(view=WarnAddedView(member, reason, total))
        await log_mod(message.guild, "Warn", message.author, member, reason, f"**Total Warns:** {total}")

    elif cmd == "warns":
        if not message.author.guild_permissions.moderate_members:
            return
        member = message.mentions[0] if message.mentions else message.author
        warns_data = config.get("warns", {}).get(str(member.id), [])
        await message.channel.send(view=WarnsListView(member, warns_data))

    elif cmd == "clearwarns":
        if not message.author.guild_permissions.administrator:
            return
        if not message.mentions:
            await message.channel.send(view=FailedView("Usage: `.clearwarns @user`"))
            return
        member = message.mentions[0]
        if "warns" in config and str(member.id) in config["warns"]:
            config["warns"][str(member.id)] = []
            save_config(config)
        await message.channel.send(view=WarnsCleared(member))
        await log_mod(message.guild, "Clear Warns", message.author, member)

    elif cmd == "slowmode":
        if not message.author.guild_permissions.manage_channels:
            return
        if len(args) < 2:
            await message.channel.send(view=FailedView("Usage: `.slowmode <seconds> [#channel]`"))
            return
        target = message.channel_mentions[0] if message.channel_mentions else message.channel
        try:
            seconds = max(0, min(21600, int(args[1])))
            await target.edit(slowmode_delay=seconds)
            await message.channel.send(view=SlowmodeView(target, seconds))
            await log_mod(message.guild, "Slowmode", message.author, target, extra=f"**Delay:** {seconds}s")
        except discord.Forbidden:
            await message.channel.send(view=FailedView("I don't have permission to edit that channel."))
        except Exception as e:
            await message.channel.send(view=FailedView(str(e)))

    elif cmd == "lock":
        if not message.author.guild_permissions.manage_channels:
            return
        target = message.channel_mentions[0] if message.channel_mentions else message.channel
        overwrite = target.overwrites_for(message.guild.default_role)
        overwrite.send_messages = False
        try:
            await target.set_permissions(message.guild.default_role, overwrite=overwrite)
            await message.channel.send(view=LockView(target, True))
            await log_mod(message.guild, "Lock", message.author, target)
        except discord.Forbidden:
            await message.channel.send(view=FailedView("I don't have permission to lock that channel."))
        except Exception as e:
            await message.channel.send(view=FailedView(str(e)))

    elif cmd == "unlock":
        if not message.author.guild_permissions.manage_channels:
            return
        target = message.channel_mentions[0] if message.channel_mentions else message.channel
        overwrite = target.overwrites_for(message.guild.default_role)
        overwrite.send_messages = True
        try:
            await target.set_permissions(message.guild.default_role, overwrite=overwrite)
            await message.channel.send(view=LockView(target, False))
            await log_mod(message.guild, "Unlock", message.author, target)
        except discord.Forbidden:
            await message.channel.send(view=FailedView("I don't have permission to unlock that channel."))
        except Exception as e:
            await message.channel.send(view=FailedView(str(e)))

    elif cmd == "afk":
        reason = " ".join(args[1:]) if len(args) > 1 else "AFK"
        if "afk" not in config:
            config["afk"] = {}
        config["afk"][str(message.author.id)] = {
            "reason": reason,
            "timestamp": int(datetime.now(timezone.utc).timestamp())
        }
        save_config(config)
        await message.channel.send(view=AfkSetView(message.author, reason))

    elif cmd == "snipe":
        data = last_deleted.get(message.channel.id)
        if not data:
            await message.channel.send(view=SnipeEmptyView())
        else:
            await message.channel.send(view=SnipeView(data))

    elif cmd == "avatar":
        member = message.mentions[0] if message.mentions else message.author
        embed = discord.Embed(title=f"{member.display_name}'s Avatar", color=discord.Color.blurple())
        embed.set_image(url=str(member.display_avatar.url))
        await message.channel.send(embed=embed)

    elif cmd == "gstart":
        if not message.author.guild_permissions.administrator:
            return
        if len(args) < 4:
            await message.channel.send(view=FailedView("Usage: `.gstart <duration> <winners> <prize>`"))
            return
        duration_str = args[1]
        try:
            winner_count = int(args[2])
        except ValueError:
            await message.channel.send(view=FailedView("Winners must be a number."))
            return
        prize = " ".join(args[3:])
        seconds = parse_duration(duration_str)
        if seconds <= 0:
            await message.channel.send(view=FailedView("Invalid duration. Example: `1h`, `30m`, `2d`"))
            return
        end_ts = int((datetime.now(timezone.utc) + timedelta(seconds=seconds)).timestamp())
        msg = await message.channel.send(view=GiveawayActiveView(prize, winner_count, end_ts, 0))
        if "giveaways" not in config:
            config["giveaways"] = {}
        config["giveaways"][str(msg.id)] = {
            "channel_id": message.channel.id,
            "prize": prize,
            "winner_count": winner_count,
            "end_timestamp": end_ts,
            "entries": [],
            "ended": False,
            "winners": []
        }
        save_config(config)
        await log_bot(
            message.guild,
            "🎉 Giveaway Started",
            f"**By:** {fmt_ref(message.author)}\n**Channel:** {message.channel.mention}\n**Prize:** {prize}\n**Winners:** {winner_count}\n**Ends:** <t:{end_ts}:R>",
            discord.Color.gold()
        )

    elif cmd == "gend":
        if not message.author.guild_permissions.administrator:
            return
        if len(args) < 2:
            await message.channel.send(view=FailedView("Usage: `.gend <message_id>`"))
            return
        msg_id = args[1]
        giveaways = config.get("giveaways", {})
        if msg_id not in giveaways:
            await message.channel.send(view=FailedView("Giveaway not found."))
            return
        gw = giveaways[msg_id]
        if gw.get("ended"):
            await message.channel.send(view=FailedView("Giveaway already ended."))
            return
        await end_giveaway(msg_id, gw)

    elif cmd == "greroll":
        if not message.author.guild_permissions.administrator:
            return
        if len(args) < 2:
            await message.channel.send(view=FailedView("Usage: `.greroll <message_id>`"))
            return
        msg_id = args[1]
        giveaways = config.get("giveaways", {})
        if msg_id not in giveaways:
            await message.channel.send(view=FailedView("Giveaway not found."))
            return
        gw = giveaways[msg_id]
        if not gw.get("ended"):
            await message.channel.send(view=FailedView("Giveaway hasn't ended yet."))
            return
        entries = gw["entries"]
        if not entries:
            await message.channel.send(view=FailedView("No entries to reroll from."))
            return
        picked = random.sample(entries, min(gw["winner_count"], len(entries)))
        gw["winners"] = picked
        save_config(config)
        channel = bot.get_channel(gw["channel_id"])
        winner_text = " ".join(f"<@{uid}>" for uid in picked)
        if channel:
            try:
                original = await channel.fetch_message(int(msg_id))
                await channel.send(f"🎉 Giveaway rerolled! **{gw['prize']}**\nNew winner(s): {winner_text}", reference=original)
            except Exception:
                pass
        await message.channel.send(view=GiveawayRerollView(picked))
        await log_bot(
            message.guild,
            "🎉 Giveaway Rerolled",
            f"**By:** {fmt_ref(message.author)}\n**Prize:** {gw['prize']}\n**New winner(s):** {winner_text}",
            discord.Color.gold()
        )

    elif cmd == "steal":
        if not message.author.guild_permissions.manage_emojis_and_stickers:
            return
        text = " ".join(args[1:])
        if not text and message.reference:
            text = getattr(message.reference.resolved, "content", "")
        await message.channel.send(view=await steal_emoji(message.guild, text, f"Stolen by {message.author}"))

    elif cmd == "scatter":
        if len(args) < 2 or not args[1].isdigit():
            await message.channel.send(view=FailedView("Usage: `.scatter <bet>`"))
            return
        await message.channel.send(view=scatter_result(message.author.id, int(args[1])))


@bot.event
async def on_message_edit(before, after):
    if after.author.bot or not after.guild or before.content == after.content:
        return
    body = (
        f"**Author:** {fmt_ref(after.author)}\n"
        f"**Channel:** {after.channel.mention}\n"
        f"**Jump:** [Go to message]({after.jump_url})\n\n"
        f"**Before**\n{quote(before.content or '(empty)', 800)}\n\n"
        f"**After**\n{quote(after.content or '(empty)', 800)}"
    )
    await send_log(after.guild, "message", LogView("✏️ Message Edited", body, discord.Color.orange()))


@bot.event
async def on_raw_bulk_message_delete(payload):
    if not payload.guild_id:
        return
    if consume_action("purge", payload.channel_id):
        return
    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return
    body = f"**Channel:** <#{payload.channel_id}>\n**Messages:** {len(payload.message_ids)}"
    await send_log(guild, "message", LogView("🧹 Bulk Delete", body, discord.Color.red()))


@bot.event
async def on_member_join(member):
    if member.guild.id != GUILD_ID:
        return
    asyncio.create_task(antiraid_on_join(member))
    asyncio.create_task(antinuke_bot_add(member))
    age = (discord.utils.utcnow() - member.created_at).days
    body = (
        f"**User:** {fmt_ref(member)}\n"
        f"**Account Created:** <t:{int(member.created_at.timestamp())}:R>\n"
        f"**Account Age:** {age} day(s)\n"
        f"**Member Count:** {member.guild.member_count}"
    )
    if age < 7:
        body += "\n⚠️ **New account**"
    await send_log(member.guild, "member", LogView("📥 Member Joined", body, discord.Color.green()))


@bot.event
async def on_member_remove(member):
    guild = member.guild
    if guild.id != GUILD_ID:
        return
    asyncio.create_task(nuke_track(guild, "kick", discord.AuditLogAction.kick, member.id, None))
    if peek_action("ban", member.id):
        return
    if consume_action("kick", member.id):
        return
    if await find_audit(guild, discord.AuditLogAction.ban, member.id, within=10):
        return
    kick_entry = await find_audit(guild, discord.AuditLogAction.kick, member.id, within=10)
    if kick_entry:
        await log_mod(guild, "Kick", kick_entry.user, member, kick_entry.reason)
        return
    roles = [r.mention for r in member.roles if r != guild.default_role]
    body = f"**User:** {fmt_ref(member)}\n**Member Count:** {guild.member_count}"
    if member.joined_at:
        body += f"\n**Joined:** <t:{int(member.joined_at.timestamp())}:R>"
    if roles:
        body += f"\n**Roles:** {clip(' '.join(roles), 700)}"
    await send_log(guild, "member", LogView("📤 Member Left", body, discord.Color.red()))


@bot.event
async def on_member_ban(guild, user):
    if guild.id != GUILD_ID:
        return
    asyncio.create_task(nuke_track(guild, "ban", discord.AuditLogAction.ban, user.id, user.id))
    if consume_action("ban", user.id):
        return
    entry = await find_audit(guild, discord.AuditLogAction.ban, user.id)
    await log_mod(guild, "Ban", entry.user if entry else None, user, entry.reason if entry else None)


@bot.event
async def on_member_unban(guild, user):
    if guild.id != GUILD_ID:
        return
    if consume_action("unban", user.id):
        return
    entry = await find_audit(guild, discord.AuditLogAction.unban, user.id)
    await log_mod(guild, "Unban", entry.user if entry else None, user, entry.reason if entry else None)


@bot.event
async def on_member_update(before, after):
    guild = after.guild
    if guild.id != GUILD_ID:
        return
    if before.nick != after.nick:
        entry = await find_audit(guild, discord.AuditLogAction.member_update, after.id)
        body = (
            f"**Member:** {fmt_ref(after)}\n"
            f"**Before:** {before.nick or '*none*'}\n"
            f"**After:** {after.nick or '*none*'}"
        )
        if entry and entry.user and entry.user.id != after.id:
            body += f"\n**Changed by:** {fmt_ref(entry.user)}"
        await send_log(guild, "member", LogView("✏️ Nickname Changed", body, discord.Color.blurple()))
    added = [r for r in after.roles if r not in before.roles]
    removed = [r for r in before.roles if r not in after.roles]
    if added:
        asyncio.create_task(antinuke_member_role_guard(after, added))
    if added or removed:
        entry = await find_audit(guild, discord.AuditLogAction.member_role_update, after.id)
        body = f"**Member:** {fmt_ref(after)}"
        if added:
            body += f"\n**Added:** {' '.join(r.mention for r in added)}"
        if removed:
            body += f"\n**Removed:** {' '.join(r.mention for r in removed)}"
        if entry and entry.user:
            body += f"\n**By:** {fmt_ref(entry.user)}"
        color = discord.Color.green() if added and not removed else discord.Color.orange()
        await send_log(guild, "member", LogView("🎭 Roles Updated", body, color))
    if before.timed_out_until != after.timed_out_until:
        kind = "Timeout" if after.timed_out_until else "Untimeout"
        if not consume_action(kind.lower(), after.id):
            entry = await find_audit(guild, discord.AuditLogAction.member_update, after.id)
            extra = None
            if after.timed_out_until:
                extra = f"**Until:** <t:{int(after.timed_out_until.timestamp())}:F>"
            await log_mod(guild, kind, entry.user if entry else None, after, entry.reason if entry else None, extra)


@bot.event
async def on_voice_state_update(member, before, after):
    if member.guild.id != GUILD_ID or member.bot:
        return
    if before.channel == after.channel:
        return
    if before.channel is None:
        title, color = "🔊 Voice Join", discord.Color.green()
        body = f"{fmt_ref(member)} joined {after.channel.mention}"
    elif after.channel is None:
        title, color = "🔇 Voice Leave", discord.Color.red()
        body = f"{fmt_ref(member)} left {before.channel.mention}"
    else:
        title, color = "🔀 Voice Move", discord.Color.blurple()
        body = f"{fmt_ref(member)} moved from {before.channel.mention} to {after.channel.mention}"
    await send_log(member.guild, "voice", LogView(title, body, color))


@bot.event
async def on_guild_channel_create(channel):
    if channel.guild.id != GUILD_ID:
        return
    asyncio.create_task(nuke_track(channel.guild, "channel_create", discord.AuditLogAction.channel_create, channel.id, channel.id))
    entry = await find_audit(channel.guild, discord.AuditLogAction.channel_create, channel.id)
    body = f"**Channel:** {channel.mention} (`{channel.id}`)\n**Type:** {str(channel.type).replace('_', ' ').title()}"
    if entry and entry.user:
        body += f"\n**Created by:** {fmt_ref(entry.user)}"
    await send_log(channel.guild, "server", LogView("➕ Channel Created", body, discord.Color.green()))


@bot.event
async def on_guild_channel_delete(channel):
    if channel.guild.id != GUILD_ID:
        return
    await ticket_channel_deleted(channel)
    asyncio.create_task(nuke_track(channel.guild, "channel_delete", discord.AuditLogAction.channel_delete, channel.id, snapshot_channel(channel)))
    entry = await find_audit(channel.guild, discord.AuditLogAction.channel_delete, channel.id)
    body = f"**Channel:** #{channel.name} (`{channel.id}`)\n**Type:** {str(channel.type).replace('_', ' ').title()}"
    if entry and entry.user:
        body += f"\n**Deleted by:** {fmt_ref(entry.user)}"
    await send_log(channel.guild, "server", LogView("➖ Channel Deleted", body, discord.Color.red()))


@bot.event
async def on_guild_channel_update(before, after):
    if after.guild.id != GUILD_ID:
        return
    changes = []
    if before.name != after.name:
        changes.append(f"**Name:** `{before.name}` → `{after.name}`")
    old_topic = getattr(before, "topic", None)
    new_topic = getattr(after, "topic", None)
    if old_topic != new_topic:
        changes.append(f"**Topic:** {clip(old_topic or 'none', 200)} → {clip(new_topic or 'none', 200)}")
    if before.category != after.category:
        old_cat = before.category.name if before.category else "none"
        new_cat = after.category.name if after.category else "none"
        changes.append(f"**Category:** {old_cat} → {new_cat}")
    if getattr(before, "nsfw", None) != getattr(after, "nsfw", None):
        changes.append(f"**NSFW:** {getattr(before, 'nsfw', None)} → {getattr(after, 'nsfw', None)}")
    if not changes:
        return
    entry = await find_audit(after.guild, discord.AuditLogAction.channel_update, after.id)
    body = f"**Channel:** {after.mention} (`{after.id}`)\n" + "\n".join(changes)
    if entry and entry.user:
        body += f"\n**By:** {fmt_ref(entry.user)}"
    await send_log(after.guild, "server", LogView("🛠️ Channel Updated", body, discord.Color.orange()))


@bot.event
async def on_guild_role_create(role):
    if role.guild.id != GUILD_ID:
        return
    asyncio.create_task(nuke_track(role.guild, "role_create", discord.AuditLogAction.role_create, role.id, role.id))
    entry = await find_audit(role.guild, discord.AuditLogAction.role_create, role.id)
    body = f"**Role:** {role.mention} (`{role.id}`)"
    if entry and entry.user:
        body += f"\n**Created by:** {fmt_ref(entry.user)}"
    await send_log(role.guild, "server", LogView("➕ Role Created", body, discord.Color.green()))


@bot.event
async def on_guild_role_delete(role):
    if role.guild.id != GUILD_ID:
        return
    asyncio.create_task(nuke_track(role.guild, "role_delete", discord.AuditLogAction.role_delete, role.id, snapshot_role(role)))
    entry = await find_audit(role.guild, discord.AuditLogAction.role_delete, role.id)
    body = f"**Role:** {role.name} (`{role.id}`)"
    if entry and entry.user:
        body += f"\n**Deleted by:** {fmt_ref(entry.user)}"
    await send_log(role.guild, "server", LogView("➖ Role Deleted", body, discord.Color.red()))


@bot.event
async def on_guild_role_update(before, after):
    if after.guild.id != GUILD_ID:
        return
    asyncio.create_task(antinuke_role_update_guard(before, after))
    changes = []
    if before.name != after.name:
        changes.append(f"**Name:** `{before.name}` → `{after.name}`")
    if before.color != after.color:
        changes.append(f"**Color:** `{before.color}` → `{after.color}`")
    if before.hoist != after.hoist:
        changes.append(f"**Hoisted:** {before.hoist} → {after.hoist}")
    if before.mentionable != after.mentionable:
        changes.append(f"**Mentionable:** {before.mentionable} → {after.mentionable}")
    if before.permissions != after.permissions:
        gained = [n.replace("_", " ") for n, v in after.permissions if v and not getattr(before.permissions, n)]
        lost = [n.replace("_", " ") for n, v in before.permissions if v and not getattr(after.permissions, n)]
        if gained:
            changes.append(f"**Permissions Added:** {clip(', '.join(gained), 400)}")
        if lost:
            changes.append(f"**Permissions Removed:** {clip(', '.join(lost), 400)}")
    if not changes:
        return
    entry = await find_audit(after.guild, discord.AuditLogAction.role_update, after.id)
    body = f"**Role:** {after.mention} (`{after.id}`)\n" + "\n".join(changes)
    if entry and entry.user:
        body += f"\n**By:** {fmt_ref(entry.user)}"
    await send_log(after.guild, "server", LogView("🛠️ Role Updated", body, discord.Color.orange()))


@bot.event
async def on_guild_update(before, after):
    if after.id != GUILD_ID:
        return
    changes = []
    if before.name != after.name:
        changes.append(f"**Name:** `{before.name}` → `{after.name}`")
    if before.icon != after.icon:
        changes.append("**Icon:** changed")
    if before.owner_id != after.owner_id:
        changes.append(f"**Owner:** <@{before.owner_id}> → <@{after.owner_id}>")
    if before.verification_level != after.verification_level:
        changes.append(f"**Verification:** {before.verification_level} → {after.verification_level}")
    if not changes:
        return
    entry = await find_audit(after, discord.AuditLogAction.guild_update, after.id)
    body = "\n".join(changes)
    if entry and entry.user:
        body += f"\n**By:** {fmt_ref(entry.user)}"
    await send_log(after, "server", LogView("🏠 Server Updated", body, discord.Color.orange()))


@bot.event
async def on_invite_create(invite):
    guild = bot.get_guild(invite.guild.id) if invite.guild else None
    if guild is None:
        return
    body = f"**Code:** `{invite.code}`\n**Channel:** {invite.channel.mention}"
    if invite.inviter:
        body += f"\n**Created by:** {fmt_ref(invite.inviter)}"
    body += f"\n**Max Uses:** {invite.max_uses or 'Unlimited'}"
    body += f"\n**Expires:** {'Never' if not invite.max_age else f'in {invite.max_age // 3600}h'}"
    await send_log(guild, "server", LogView("🔗 Invite Created", body, discord.Color.green()))


@bot.event
async def on_invite_delete(invite):
    guild = bot.get_guild(invite.guild.id) if invite.guild else None
    if guild is None:
        return
    body = f"**Code:** `{invite.code}`\n**Channel:** {invite.channel.mention if invite.channel else 'Unknown'}"
    await send_log(guild, "server", LogView("🔗 Invite Deleted", body, discord.Color.red()))


@bot.event
async def on_webhooks_update(channel):
    guild = channel.guild
    if guild.id != GUILD_ID or not sec_cfg("antinuke")["enabled"]:
        return
    await asyncio.sleep(1)
    try:
        async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.webhook_create):
            if entry.id in seen_audit:
                continue
            if (discord.utils.utcnow() - entry.created_at).total_seconds() > 15:
                continue
            seen_audit.add(entry.id)
            if len(seen_audit) > 500:
                seen_audit.clear()
            await nuke_record(guild, "webhook_create", entry, getattr(entry.target, "id", None))
    except Exception as e:
        print(f"Webhook audit failed: {e}")


TICKET_BANNER = "ticket_banner.png"
TICKET_BANNER_NAME = "ticket_banner.png"


def tk_cfg() -> dict:
    t = config.setdefault("ticket", {})
    t.setdefault("counter", 0)
    t.setdefault("open", {})
    return t


def tk_is_staff(member: discord.Member) -> bool:
    perms = member.guild_permissions
    if perms.administrator or perms.manage_channels:
        return True
    rid = tk_cfg().get("staff_role_id")
    return bool(rid and any(r.id == rid for r in member.roles))


def tk_find_open(guild: discord.Guild, user_id: int):
    t = tk_cfg()
    for cid, data in list(t["open"].items()):
        if data["user_id"] != user_id:
            continue
        ch = guild.get_channel(int(cid))
        if ch is None:
            t["open"].pop(cid, None)
            save_config(config)
            continue
        return ch
    return None


def tk_safe_name(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]", "", name.lower().replace(" ", "-"))
    return (cleaned or "user")[:20]


class TicketInfoView(discord.ui.LayoutView):
    def __init__(self, title: str, body: str, color=None):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(f"# {title}"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(body)
            )
        )


def tk_err(msg: str) -> discord.ui.LayoutView:
    return TicketInfoView("Failed!", msg)


def tk_ok(title: str, body: str) -> discord.ui.LayoutView:
    return TicketInfoView(title, body)


class OpenTicketButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Open Ticket",
            style=discord.ButtonStyle.primary,
            custom_id="ticket_open"
        )

    async def callback(self, interaction: discord.Interaction):
        existing = tk_find_open(interaction.guild, interaction.user.id)
        if existing:
            await interaction.response.send_message(
                view=tk_err(f"You already have an open ticket: {existing.mention}"),
                ephemeral=True
            )
            return
        await interaction.response.send_modal(TicketModal())


class TicketClaimButton(discord.ui.Button):
    def __init__(self, claimed_by: int = None):
        super().__init__(
            label="Claimed" if claimed_by else "Claim",
            style=discord.ButtonStyle.success,
            emoji="🙋",
            custom_id="ticket_claim",
            disabled=bool(claimed_by)
        )

    async def callback(self, interaction: discord.Interaction):
        t = tk_cfg()
        data = t["open"].get(str(interaction.channel.id))
        if not data:
            await interaction.response.send_message(view=tk_err("This is not an active ticket."), ephemeral=True)
            return
        if not tk_is_staff(interaction.user):
            await interaction.response.send_message(view=tk_err("Only staff can claim tickets."), ephemeral=True)
            return
        if data.get("claimed_by"):
            await interaction.response.send_message(view=tk_err(f"Already claimed by <@{data['claimed_by']}>."), ephemeral=True)
            return
        data["claimed_by"] = interaction.user.id
        save_config(config)
        await interaction.response.edit_message(view=TicketWelcomeView(data))
        await interaction.channel.send(
            view=tk_ok("🙋 Ticket Claimed", f"{interaction.user.mention} will be assisting <@{data['user_id']}>."),
            allowed_mentions=discord.AllowedMentions.none()
        )


class TicketCloseButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Close",
            style=discord.ButtonStyle.danger,
            emoji="🔒",
            custom_id="ticket_close"
        )

    async def callback(self, interaction: discord.Interaction):
        data = tk_cfg()["open"].get(str(interaction.channel.id))
        if not data:
            await interaction.response.send_message(view=tk_err("This is not an active ticket."), ephemeral=True)
            return
        if not (tk_is_staff(interaction.user) or interaction.user.id == data["user_id"]):
            await interaction.response.send_message(view=tk_err("Only staff or the ticket owner can close this."), ephemeral=True)
            return
        await interaction.response.send_message(
            "Are you sure you want to close this ticket?",
            view=TicketCloseConfirm(),
            ephemeral=True
        )


class TicketCloseConfirm(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="Confirm Close", style=discord.ButtonStyle.danger, emoji="🔒")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="🔒 Closing ticket...", view=None)
        await close_ticket(interaction.channel, interaction.user)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", view=None)


class TicketPanelPersistentView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(OpenTicketButton())


class TicketControlsPersistentView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketClaimButton())
        self.add_item(TicketCloseButton())


class TicketPanelView(discord.ui.LayoutView):
    def __init__(self, with_banner: bool = True):
        super().__init__(timeout=None)
        container = discord.ui.Container()
        if with_banner:
            gallery = discord.ui.MediaGallery()
            gallery.add_item(media=f"attachment://{TICKET_BANNER_NAME}")
            container.add_item(gallery)
        container.add_item(discord.ui.TextDisplay("**Need Help?**\nClick **Open Ticket**"))
        self.add_item(container)
        self.add_item(discord.ui.ActionRow(OpenTicketButton()))


class TicketWelcomeView(discord.ui.LayoutView):
    def __init__(self, data: dict):
        super().__init__(timeout=None)
        created = int(data.get("created", time.time()))
        status = f"🙋 Claimed by <@{data['claimed_by']}>" if data.get("claimed_by") else "🟢 Waiting for staff"
        product = data.get("product") or "Not specified"
        tried = data.get("tried") or "Not specified"
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# 🎫 Support Ticket"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"**Opened by:** <@{data['user_id']}>\n"
                    f"**Created:** <t:{created}:F> (<t:{created}:R>)\n"
                    f"**Status:** {status}"
                ),
                discord.ui.Separator(),
                discord.ui.TextDisplay(f"**Issue / Request**\n{data['issue']}"),
                discord.ui.TextDisplay(f"**Product / Feature**\n{product}"),
                discord.ui.TextDisplay(f"**Already Tried**\n{tried}"),
                discord.ui.Separator(),
                discord.ui.TextDisplay("A staff member will be with you shortly. Please wait patiently."),
                discord.ui.ActionRow(TicketClaimButton(data.get("claimed_by")), TicketCloseButton()),
            )
        )


class TicketModal(discord.ui.Modal, title="Support Ticket"):
    issue = discord.ui.TextInput(
        label="Describe your issue or what you need",
        style=discord.TextStyle.paragraph,
        placeholder="Explain the problem in detail...",
        required=True,
        max_length=1000
    )
    product = discord.ui.TextInput(
        label="Product / feature involved (optional)",
        style=discord.TextStyle.short,
        required=False,
        max_length=100
    )
    tried = discord.ui.TextInput(
        label="What you already tried (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        user = interaction.user
        t = tk_cfg()

        existing = tk_find_open(guild, user.id)
        if existing:
            await interaction.followup.send(view=tk_err(f"You already have an open ticket: {existing.mention}"), ephemeral=True)
            return

        category = guild.get_channel(t["category_id"]) if t.get("category_id") else None
        if not isinstance(category, discord.CategoryChannel):
            category = None
        staff_role = guild.get_role(t["staff_role_id"]) if t.get("staff_role_id") else None

        user_perms = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            attach_files=True, embed_links=True
        )
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: user_perms,
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                manage_channels=True, manage_permissions=True, embed_links=True, attach_files=True
            ),
        }
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                attach_files=True, embed_links=True, manage_messages=True
            )

        t["counter"] += 1
        try:
            channel = await guild.create_text_channel(
                name=f"ticket-{tk_safe_name(user.name)}",
                category=category,
                overwrites=overwrites,
                topic=f"Ticket owner: {user.id}",
                reason=f"Ticket opened by {user}"
            )
        except discord.Forbidden:
            await interaction.followup.send(view=tk_err("I don't have permission to create channels (need Manage Channels)."), ephemeral=True)
            return
        except Exception as e:
            await interaction.followup.send(view=tk_err(f"Could not create the ticket: `{e}`"), ephemeral=True)
            return

        data = {
            "user_id": user.id,
            "issue": self.issue.value,
            "product": self.product.value.strip(),
            "tried": self.tried.value.strip(),
            "claimed_by": None,
            "created": int(time.time()),
            "number": t["counter"]
        }
        t["open"][str(channel.id)] = data
        save_config(config)

        mentions = user.mention + (f" {staff_role.mention}" if staff_role else "")
        try:
            ping = await channel.send(mentions, allowed_mentions=discord.AllowedMentions(users=True, roles=True))
            await ping.delete()
        except Exception:
            pass
        await channel.send(view=TicketWelcomeView(data))
        await interaction.followup.send(view=tk_ok("🎫 Ticket Created", f"Your ticket is ready: {channel.mention}"), ephemeral=True)

        log_ch = guild.get_channel(t["log_id"]) if t.get("log_id") else None
        if log_ch:
            try:
                await log_ch.send(view=TicketInfoView(
                    "🎫 Ticket Opened",
                    f"**User:** {user.mention} (`{user.id}`)\n**Channel:** {channel.mention}\n**Issue:** {data['issue'][:300]}",
                    discord.Color.green()
                ))
            except Exception as e:
                print(f"Ticket log failed: {e}")

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"Ticket modal error: {error}")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(view=tk_err("Something went wrong while creating your ticket."), ephemeral=True)
            else:
                await interaction.response.send_message(view=tk_err("Something went wrong while creating your ticket."), ephemeral=True)
        except Exception:
            pass


async def close_ticket(channel: discord.TextChannel, closer: discord.Member):
    t = tk_cfg()
    data = t["open"].pop(str(channel.id), None)
    if not data:
        return
    save_config(config)
    guild = channel.guild

    lines = [
        f"Ticket #{data.get('number', '?')} - {channel.name}",
        f"Owner ID: {data['user_id']}",
        f"Closed by: {closer} ({closer.id})",
        f"Issue: {data['issue']}",
        "=" * 60,
    ]
    try:
        async for m in channel.history(limit=None, oldest_first=True):
            if m.author.bot and not m.content and not m.attachments:
                continue
            ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
            text = m.content or ""
            for a in m.attachments:
                text += f" [attachment: {a.url}]"
            lines.append(f"[{ts} UTC] {m.author} ({m.author.id}): {text}")
    except Exception as e:
        lines.append(f"(Could not read full history: {e})")
    transcript = "\n".join(lines).encode("utf-8")
    fname = f"transcript-{channel.name}.txt"

    opener = guild.get_member(data["user_id"])
    claimed = f"<@{data['claimed_by']}>" if data.get("claimed_by") else "Nobody"
    log_ch = guild.get_channel(t["log_id"]) if t.get("log_id") else None
    if log_ch:
        try:
            await log_ch.send(
                view=TicketInfoView(
                    "🔒 Ticket Closed",
                    f"**Ticket:** #{data.get('number', '?')} (`{channel.name}`)\n"
                    f"**Owner:** <@{data['user_id']}>\n"
                    f"**Claimed by:** {claimed}\n"
                    f"**Closed by:** {closer.mention}",
                    discord.Color.red()
                ),
                file=discord.File(BytesIO(transcript), filename=fname),
                allowed_mentions=discord.AllowedMentions.none()
            )
        except Exception as e:
            print(f"Ticket close log failed: {e}")

    if opener:
        try:
            await opener.send(
                f"🔒 Your ticket **{channel.name}** in **{guild.name}** was closed. Transcript attached.",
                file=discord.File(BytesIO(transcript), filename=fname)
            )
        except Exception:
            pass

    try:
        await channel.send(view=TicketInfoView("🔒 Ticket Closed", f"Closed by {closer.mention}. Deleting in 5 seconds...", discord.Color.red()))
    except Exception:
        pass
    await asyncio.sleep(5)
    try:
        await channel.delete(reason=f"Ticket closed by {closer}")
    except Exception as e:
        print(f"Ticket delete failed: {e}")


async def ticket_channel_deleted(channel):
    t = tk_cfg()
    if t["open"].pop(str(channel.id), None) is not None:
        save_config(config)


ticket_group = discord.app_commands.Group(
    name="ticket",
    description="Ticket system commands",
    guild_ids=[GUILD_ID],
    default_permissions=discord.Permissions(manage_channels=True)
)


@ticket_group.command(name="setup", description="Send the ticket panel and configure the ticket system")
@discord.app_commands.describe(
    channel="Channel where the ticket panel will be sent",
    category="Category where new tickets are created",
    staff_role="Role that can see and manage tickets",
    log="Channel for ticket logs / transcripts (optional)"
)
@discord.app_commands.default_permissions(administrator=True)
async def ticket_setup(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    category: discord.CategoryChannel,
    staff_role: discord.Role,
    log: discord.TextChannel = None
):
    if not os.path.exists(TICKET_BANNER):
        await interaction.response.send_message(view=tk_err(f"`{TICKET_BANNER}` is missing next to main.py."), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    t = tk_cfg()
    t["category_id"] = category.id
    t["staff_role_id"] = staff_role.id
    t["log_id"] = log.id if log else None
    t["panel_channel_id"] = channel.id
    save_config(config)
    try:
        await channel.send(
            view=TicketPanelView(),
            file=discord.File(TICKET_BANNER, filename=TICKET_BANNER_NAME)
        )
    except discord.Forbidden:
        await interaction.followup.send(view=tk_err(f"I can't send messages in {channel.mention}."), ephemeral=True)
        return
    await interaction.followup.send(
        view=tk_ok(
            "Ticket Setup Complete",
            f"**Panel:** {channel.mention}\n**Category:** {category.mention}\n"
            f"**Staff role:** {staff_role.mention}\n**Logs:** {log.mention if log else 'None'}"
        ),
        ephemeral=True
    )


def tk_ticket_only(interaction: discord.Interaction):
    return tk_cfg()["open"].get(str(interaction.channel.id))


@ticket_group.command(name="add", description="Add a member to this ticket")
@discord.app_commands.describe(member="Member to add")
async def ticket_add(interaction: discord.Interaction, member: discord.Member):
    if not tk_ticket_only(interaction):
        await interaction.response.send_message(view=tk_err("Use this inside a ticket channel."), ephemeral=True)
        return
    if not tk_is_staff(interaction.user):
        await interaction.response.send_message(view=tk_err("Only staff can do that."), ephemeral=True)
        return
    await interaction.channel.set_permissions(
        member, view_channel=True, send_messages=True, read_message_history=True, attach_files=True
    )
    await interaction.response.send_message(view=tk_ok("Member Added", f"{member.mention} was added to this ticket."))


@ticket_group.command(name="remove", description="Remove a member from this ticket")
@discord.app_commands.describe(member="Member to remove")
async def ticket_remove(interaction: discord.Interaction, member: discord.Member):
    data = tk_ticket_only(interaction)
    if not data:
        await interaction.response.send_message(view=tk_err("Use this inside a ticket channel."), ephemeral=True)
        return
    if not tk_is_staff(interaction.user):
        await interaction.response.send_message(view=tk_err("Only staff can do that."), ephemeral=True)
        return
    if member.id == data["user_id"]:
        await interaction.response.send_message(view=tk_err("You can't remove the ticket owner."), ephemeral=True)
        return
    await interaction.channel.set_permissions(member, overwrite=None)
    await interaction.response.send_message(view=tk_ok("Member Removed", f"{member.mention} was removed from this ticket."))


@ticket_group.command(name="rename", description="Rename this ticket channel")
@discord.app_commands.describe(name="New channel name")
async def ticket_rename(interaction: discord.Interaction, name: str):
    if not tk_ticket_only(interaction):
        await interaction.response.send_message(view=tk_err("Use this inside a ticket channel."), ephemeral=True)
        return
    if not tk_is_staff(interaction.user):
        await interaction.response.send_message(view=tk_err("Only staff can do that."), ephemeral=True)
        return
    new = tk_safe_name(name)
    await interaction.response.send_message(view=tk_ok("Ticket Renamed", f"Renamed to `{new}`."))
    await interaction.channel.edit(name=new)


@ticket_group.command(name="close", description="Close this ticket")
async def ticket_close_cmd(interaction: discord.Interaction):
    data = tk_ticket_only(interaction)
    if not data:
        await interaction.response.send_message(view=tk_err("Use this inside a ticket channel."), ephemeral=True)
        return
    if not (tk_is_staff(interaction.user) or interaction.user.id == data["user_id"]):
        await interaction.response.send_message(view=tk_err("Only staff or the ticket owner can close this."), ephemeral=True)
        return
    await interaction.response.send_message(
        "Are you sure you want to close this ticket?",
        view=TicketCloseConfirm(),
        ephemeral=True
    )


tree.add_command(ticket_group, guild=discord.Object(id=GUILD_ID))


EB_COLORS = {
    "none": None,
    "blurple": discord.Colour.blurple(),
    "red": discord.Colour.red(),
    "green": discord.Colour.green(),
    "blue": discord.Colour.blue(),
    "orange": discord.Colour.orange(),
    "dark_grey": discord.Colour.dark_grey(),
    "white": discord.Colour(0xFFFFFF),
}

EB_COLOR_NAMES = {
    "none": "None",
    "blurple": "Blurple",
    "red": "Red",
    "green": "Green",
    "blue": "Blue",
    "orange": "Orange",
    "dark_grey": "Dark Grey",
    "white": "White",
}

EB_MAX_COMPONENTS = 20
EB_MAX_TOTAL = 26
EB_MAX_CHARS = 3600


def eb_label(comp: dict, i: int) -> str:
    t = comp["type"]
    if t == "text":
        p = comp["content"][:45].replace("\n", " ")
        return f"{i + 1}. Text — {p}"
    if t == "separator":
        kind = "line" if comp.get("divider", True) else "spacer"
        return f"{i + 1}. Separator ({comp.get('spacing', 'small')}, {kind})"
    if t == "section":
        p = comp["content"][:40].replace("\n", " ")
        return f"{i + 1}. Section — {p}"
    if t == "media_gallery":
        n = len(comp["urls"])
        return f"{i + 1}. Gallery ({n} image{'s' if n != 1 else ''})"
    if t == "button_row":
        labels = ", ".join(b["label"] for b in comp["buttons"][:3])
        suffix = "..." if len(comp["buttons"]) > 3 else ""
        return f"{i + 1}. Buttons — {labels}{suffix}"
    return f"{i + 1}. Unknown"


def eb_cost(comp: dict) -> int:
    t = comp["type"]
    if t == "section":
        return 3 if comp.get("thumbnail_url") else 1
    if t == "media_gallery":
        return 1 + len(comp["urls"])
    if t == "button_row":
        return 1 + len(comp["buttons"])
    return 1


def eb_chars(comp: dict) -> int:
    t = comp["type"]
    if t in ("text", "section"):
        return len(comp["content"])
    return 0


def eb_fit(data: list):
    if len(data) > EB_MAX_COMPONENTS:
        return f"Maximum of {EB_MAX_COMPONENTS} components reached."
    if 1 + sum(eb_cost(c) for c in data) > EB_MAX_TOTAL:
        return "Discord's component limit would be exceeded. Remove something or use fewer images/buttons."
    if sum(eb_chars(c) for c in data) > EB_MAX_CHARS:
        return f"Too much text. The total is limited to {EB_MAX_CHARS} characters."
    return None


def eb_render_into(container: discord.ui.Container, comp: dict, transform=None) -> None:
    t = comp["type"]
    tf = transform or (lambda s: s)
    if t == "text":
        container.add_item(discord.ui.TextDisplay(tf(comp["content"])))
    elif t == "separator":
        sp = discord.SeparatorSpacing.large if comp.get("spacing") == "large" else discord.SeparatorSpacing.small
        container.add_item(discord.ui.Separator(visible=comp.get("divider", True), spacing=sp))
    elif t == "section":
        url = comp.get("thumbnail_url")
        if url:
            section = discord.ui.Section(accessory=discord.ui.Thumbnail(media=url))
            section.add_item(discord.ui.TextDisplay(tf(comp["content"])))
            container.add_item(section)
        else:
            container.add_item(discord.ui.TextDisplay(tf(comp["content"])))
    elif t == "media_gallery":
        if comp["urls"]:
            gallery = discord.ui.MediaGallery()
            for u in comp["urls"]:
                gallery.add_item(media=u)
            container.add_item(gallery)
    elif t == "button_row":
        if comp["buttons"]:
            row = discord.ui.ActionRow(*[
                discord.ui.Button(label=b["label"], style=discord.ButtonStyle.link, url=b["url"])
                for b in comp["buttons"][:5]
            ])
            container.add_item(row)


def eb_make_view(data: list, color_key: str, transform=None) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_color=EB_COLORS.get(color_key))
    if not data:
        container.add_item(discord.ui.TextDisplay("*(empty container)*"))
    else:
        for comp in data:
            eb_render_into(container, comp, transform)
    view.add_item(container)
    return view


class EbTextModal(discord.ui.Modal):
    def __init__(self, view, index=None):
        super().__init__(title="Edit Text Display" if index is not None else "Add Text Display")
        self._v = view
        self._i = index
        self._field = discord.ui.TextInput(
            label="Content",
            style=discord.TextStyle.paragraph,
            placeholder="Supports markdown: **bold**, ### heading, > quote...",
            required=True,
            max_length=2000,
            default=view._data[index]["content"] if index is not None else None,
        )
        self.add_item(self._field)

    async def on_submit(self, interaction: discord.Interaction):
        await self._v._commit(interaction, {"type": "text", "content": self._field.value}, self._i)


class EbSeparatorModal(discord.ui.Modal):
    def __init__(self, view, index=None):
        super().__init__(title="Edit Separator" if index is not None else "Add Separator")
        self._v = view
        self._i = index
        existing = view._data[index] if index is not None else {}
        self._divider = discord.ui.TextInput(
            label="Show divider line? (yes / no)",
            default="yes" if existing.get("divider", True) else "no",
            max_length=3,
            required=True,
        )
        self._spacing = discord.ui.TextInput(
            label="Spacing (small / large)",
            default=existing.get("spacing", "small"),
            max_length=5,
            required=True,
        )
        self.add_item(self._divider)
        self.add_item(self._spacing)

    async def on_submit(self, interaction: discord.Interaction):
        divider = self._divider.value.strip().lower() not in ("no", "n", "false", "0")
        spacing = "large" if self._spacing.value.strip().lower() == "large" else "small"
        await self._v._commit(interaction, {"type": "separator", "divider": divider, "spacing": spacing}, self._i)


class EbSectionModal(discord.ui.Modal):
    def __init__(self, view, index=None):
        super().__init__(title="Edit Section" if index is not None else "Add Section & Thumbnail")
        self._v = view
        self._i = index
        existing = view._data[index] if index is not None else {}
        self._text = discord.ui.TextInput(
            label="Text Content",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=1000,
            default=existing.get("content"),
        )
        self._url = discord.ui.TextInput(
            label="Thumbnail URL (optional)",
            style=discord.TextStyle.short,
            required=False,
            placeholder="https://example.com/image.png",
            max_length=500,
            default=existing.get("thumbnail_url"),
        )
        self.add_item(self._text)
        self.add_item(self._url)

    async def on_submit(self, interaction: discord.Interaction):
        url = self._url.value.strip() or None
        if url and not url.startswith(("http://", "https://")):
            await interaction.response.send_message("Thumbnail must be a valid http(s) URL.", ephemeral=True)
            return
        await self._v._commit(interaction, {"type": "section", "content": self._text.value, "thumbnail_url": url}, self._i)


class EbGalleryModal(discord.ui.Modal):
    def __init__(self, view, index=None):
        super().__init__(title="Edit Media Gallery" if index is not None else "Add Media Gallery")
        self._v = view
        self._i = index
        existing = view._data[index] if index is not None else {}
        self._urls = discord.ui.TextInput(
            label="Image URLs — one per line, max 10",
            style=discord.TextStyle.paragraph,
            required=True,
            placeholder="https://example.com/image1.png\nhttps://example.com/image2.png",
            max_length=4000,
            default="\n".join(existing.get("urls", [])) or None,
        )
        self.add_item(self._urls)

    async def on_submit(self, interaction: discord.Interaction):
        lines = [ln.strip() for ln in self._urls.value.splitlines() if ln.strip().startswith(("http://", "https://"))]
        if not lines:
            await interaction.response.send_message("No valid image URLs found.", ephemeral=True)
            return
        await self._v._commit(interaction, {"type": "media_gallery", "urls": lines[:10]}, self._i)


class EbButtonRowModal(discord.ui.Modal):
    def __init__(self, view, index=None):
        super().__init__(title="Edit Button Row" if index is not None else "Add Button Row")
        self._v = view
        self._i = index
        existing = view._data[index] if index is not None else {}
        existing_text = "\n".join(f"{b['label']} :: {b['url']}" for b in existing.get("buttons", []))
        self._input = discord.ui.TextInput(
            label="Buttons — one per line: Label :: https://url",
            style=discord.TextStyle.paragraph,
            required=True,
            placeholder="Discord :: https://discord.com\nGitHub :: https://github.com",
            max_length=1000,
            default=existing_text or None,
        )
        self.add_item(self._input)

    async def on_submit(self, interaction: discord.Interaction):
        buttons = []
        for line in self._input.value.splitlines():
            if "::" not in line:
                continue
            label_part, _, url_part = line.partition("::")
            label = label_part.strip()[:80]
            url = url_part.strip()
            if label and url.startswith(("http://", "https://")):
                buttons.append({"label": label, "url": url})
        if not buttons:
            await interaction.response.send_message("No valid buttons found. Use: `Label :: https://url.com`", ephemeral=True)
            return
        await self._v._commit(interaction, {"type": "button_row", "buttons": buttons[:5]}, self._i)


EB_MODALS = {
    "text": EbTextModal,
    "separator": EbSeparatorModal,
    "section": EbSectionModal,
    "media_gallery": EbGalleryModal,
    "button_row": EbButtonRowModal,
}


class EmbedBuilderView(discord.ui.LayoutView):
    def __init__(self, author_id: int, data: list = None, color_key: str = "none", save_cb=None, heading: str = "Container Builder"):
        super().__init__(timeout=600)
        self.author_id = author_id
        self._color_key = color_key if color_key in EB_COLORS else "none"
        self._data = json.loads(json.dumps(data)) if data else []
        self._mode = "normal"
        self._reorder_index = None
        self._save_cb = save_cb
        self._heading = heading
        self._rebuild()

    def _comp_options(self):
        return [
            discord.SelectOption(
                label=eb_label(c, i)[:100],
                value=str(i),
                default=(i == self._reorder_index and self._mode == "reorder"),
            )
            for i, c in enumerate(self._data)
        ]

    def _build_preview(self):
        c = discord.ui.Container(accent_color=EB_COLORS.get(self._color_key))
        if not self._data:
            c.add_item(discord.ui.TextDisplay("-# Your container is empty. Use the controls below to add components."))
        else:
            for comp in self._data:
                eb_render_into(c, comp)
        return c

    def _cancel_row(self):
        row = discord.ui.ActionRow(
            discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="eb_cancel")
        )
        row.children[0].callback = self._cb_cancel
        return row

    def _ctrl_normal(self):
        c = discord.ui.Container(accent_color=None)
        c.add_item(discord.ui.TextDisplay(f"**{self._heading}**"))
        c.add_item(discord.ui.Separator())
        c.add_item(discord.ui.TextDisplay(
            f"-# Color: **{EB_COLOR_NAMES[self._color_key]}**  |  Components: **{len(self._data)} / {EB_MAX_COMPONENTS}**"
        ))
        c.add_item(discord.ui.Separator(visible=False))
        at_max = len(self._data) >= EB_MAX_COMPONENTS
        add_opts = [
            discord.SelectOption(label="Text Display", value="text", description="A block of markdown text"),
            discord.SelectOption(label="Separator", value="separator", description="A divider line or invisible spacer"),
            discord.SelectOption(label="Section & Thumbnail", value="section", description="Text with a thumbnail image on the right"),
            discord.SelectOption(label="Media Gallery", value="media_gallery", description="One or more images in a grid"),
            discord.SelectOption(label="Button Row", value="button_row", description="Up to 5 link buttons in a row"),
        ]
        add_row = discord.ui.ActionRow(discord.ui.Select(
            placeholder="Add a component..." if not at_max else f"Maximum {EB_MAX_COMPONENTS} components reached",
            custom_id="eb_add",
            options=add_opts,
            disabled=at_max,
        ))
        add_row.children[0].callback = self._cb_add
        c.add_item(add_row)
        has = bool(self._data)
        can_reorder = len(self._data) >= 2
        btn_row = discord.ui.ActionRow(
            discord.ui.Button(label="Edit", style=discord.ButtonStyle.secondary, custom_id="eb_edit", disabled=not has),
            discord.ui.Button(label="Remove", style=discord.ButtonStyle.danger, custom_id="eb_remove", disabled=not has),
            discord.ui.Button(label="Color", style=discord.ButtonStyle.secondary, custom_id="eb_color"),
            discord.ui.Button(label="Reorder", style=discord.ButtonStyle.secondary, custom_id="eb_reorder", disabled=not can_reorder),
            discord.ui.Button(
                label="Save" if self._save_cb else "Send",
                style=discord.ButtonStyle.primary,
                custom_id="eb_send"
            ),
        )
        btn_row.children[0].callback = self._cb_edit_btn
        btn_row.children[1].callback = self._cb_remove_btn
        btn_row.children[2].callback = self._cb_color_btn
        btn_row.children[3].callback = self._cb_reorder_btn
        btn_row.children[4].callback = self._cb_save if self._save_cb else self._cb_send_btn
        c.add_item(btn_row)
        return c

    def _ctrl_color(self):
        c = discord.ui.Container(accent_color=None)
        c.add_item(discord.ui.TextDisplay("**Select Accent Color**"))
        c.add_item(discord.ui.Separator())
        c.add_item(discord.ui.TextDisplay("-# Choose the color bar shown on the left side of the container."))
        c.add_item(discord.ui.Separator(visible=False))
        opts = [
            discord.SelectOption(label=name, value=key, default=(key == self._color_key))
            for key, name in EB_COLOR_NAMES.items()
        ]
        sel_row = discord.ui.ActionRow(discord.ui.Select(placeholder="Choose a color...", custom_id="eb_color_sel", options=opts))
        sel_row.children[0].callback = self._cb_color_sel
        c.add_item(sel_row)
        c.add_item(self._cancel_row())
        return c

    def _ctrl_pick(self, title: str, hint: str, placeholder: str, cid: str, cb):
        c = discord.ui.Container(accent_color=None)
        c.add_item(discord.ui.TextDisplay(f"**{title}**"))
        c.add_item(discord.ui.Separator())
        c.add_item(discord.ui.TextDisplay(f"-# {hint}"))
        c.add_item(discord.ui.Separator(visible=False))
        sel_row = discord.ui.ActionRow(discord.ui.Select(placeholder=placeholder, custom_id=cid, options=self._comp_options()))
        sel_row.children[0].callback = cb
        c.add_item(sel_row)
        c.add_item(self._cancel_row())
        return c

    def _ctrl_reorder(self):
        c = discord.ui.Container(accent_color=None)
        c.add_item(discord.ui.TextDisplay("**Reorder Components**"))
        c.add_item(discord.ui.Separator())
        if self._reorder_index is not None:
            label = eb_label(self._data[self._reorder_index], self._reorder_index)
            c.add_item(discord.ui.TextDisplay(f"-# Selected: **{label[:80]}**"))
        else:
            c.add_item(discord.ui.TextDisplay("-# Select a component below, then use Move Up / Move Down."))
        c.add_item(discord.ui.Separator(visible=False))
        sel_row = discord.ui.ActionRow(discord.ui.Select(
            placeholder="Select a component to move...", custom_id="eb_reorder_sel", options=self._comp_options()
        ))
        sel_row.children[0].callback = self._cb_reorder_sel
        c.add_item(sel_row)
        can_up = self._reorder_index is not None and self._reorder_index > 0
        can_down = self._reorder_index is not None and self._reorder_index < len(self._data) - 1
        nav_row = discord.ui.ActionRow(
            discord.ui.Button(label="Move Up", style=discord.ButtonStyle.secondary, custom_id="eb_up", disabled=not can_up),
            discord.ui.Button(label="Move Down", style=discord.ButtonStyle.secondary, custom_id="eb_down", disabled=not can_down),
            discord.ui.Button(label="Done", style=discord.ButtonStyle.primary, custom_id="eb_done"),
        )
        nav_row.children[0].callback = self._cb_move_up
        nav_row.children[1].callback = self._cb_move_down
        nav_row.children[2].callback = self._cb_cancel
        c.add_item(nav_row)
        return c

    def _ctrl_send(self):
        c = discord.ui.Container(accent_color=None)
        c.add_item(discord.ui.TextDisplay("**Send Container**"))
        c.add_item(discord.ui.Separator())
        c.add_item(discord.ui.TextDisplay("-# Select the channel to send your container to."))
        c.add_item(discord.ui.Separator(visible=False))
        sel_row = discord.ui.ActionRow(discord.ui.ChannelSelect(
            placeholder="Select a channel...",
            custom_id="eb_channel_sel",
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        ))
        sel_row.children[0].callback = self._cb_channel_sel
        c.add_item(sel_row)
        c.add_item(self._cancel_row())
        return c

    def _ctrl_done(self, text: str):
        c = discord.ui.Container(accent_color=None)
        c.add_item(discord.ui.TextDisplay("**Done**"))
        c.add_item(discord.ui.Separator())
        c.add_item(discord.ui.TextDisplay(text))
        if not self._save_cb:
            c.add_item(discord.ui.Separator(visible=False))
            reset_row = discord.ui.ActionRow(
                discord.ui.Button(label="Build Another", style=discord.ButtonStyle.secondary, custom_id="eb_reset")
            )
            reset_row.children[0].callback = self._cb_reset
            c.add_item(reset_row)
        return c

    def _rebuild(self, done_text: str = None):
        self.clear_items()
        self.add_item(self._build_preview())
        if self._mode == "normal":
            self.add_item(self._ctrl_normal())
        elif self._mode == "color":
            self.add_item(self._ctrl_color())
        elif self._mode == "edit":
            self.add_item(self._ctrl_pick("Edit a Component", "Select the component you want to edit.", "Select a component to edit...", "eb_edit_sel", self._cb_edit_sel))
        elif self._mode == "remove":
            self.add_item(self._ctrl_pick("Remove a Component", "Select the component you want to remove.", "Select a component to remove...", "eb_remove_sel", self._cb_remove_sel))
        elif self._mode == "reorder":
            self.add_item(self._ctrl_reorder())
        elif self._mode == "send":
            self.add_item(self._ctrl_send())
        elif self._mode == "done":
            self.add_item(self._ctrl_done(done_text or "Done."))

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the person who ran this command can use these controls.", ephemeral=True)
            return False
        return True

    async def _refresh(self, interaction: discord.Interaction, mode: str = None, done_text: str = None):
        if mode:
            self._mode = mode
        self._rebuild(done_text)
        await interaction.response.edit_message(view=self)

    async def _commit(self, interaction: discord.Interaction, comp: dict, index):
        new = list(self._data)
        if index is None:
            new.append(comp)
        else:
            new[index] = comp
        problem = eb_fit(new)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        self._data = new
        await self._refresh(interaction, "normal")

    async def _cb_add(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        await interaction.response.send_modal(EB_MODALS[interaction.data["values"][0]](self))

    async def _cb_edit_btn(self, interaction: discord.Interaction):
        if await self._guard(interaction):
            await self._refresh(interaction, "edit")

    async def _cb_remove_btn(self, interaction: discord.Interaction):
        if await self._guard(interaction):
            await self._refresh(interaction, "remove")

    async def _cb_color_btn(self, interaction: discord.Interaction):
        if await self._guard(interaction):
            await self._refresh(interaction, "color")

    async def _cb_reorder_btn(self, interaction: discord.Interaction):
        if await self._guard(interaction):
            self._reorder_index = None
            await self._refresh(interaction, "reorder")

    async def _cb_send_btn(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        if not self._data:
            await interaction.response.send_message("Add at least one component first.", ephemeral=True)
            return
        await self._refresh(interaction, "send")

    async def _cb_save(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        if not self._data:
            await interaction.response.send_message("Add at least one component first.", ephemeral=True)
            return
        try:
            text = await self._save_cb(interaction, json.loads(json.dumps(self._data)), self._color_key)
        except Exception as e:
            await interaction.response.send_message(f"Failed to save: `{e}`", ephemeral=True)
            return
        self.stop()
        await self._refresh(interaction, "done", text)

    async def _cb_cancel(self, interaction: discord.Interaction):
        if await self._guard(interaction):
            self._reorder_index = None
            await self._refresh(interaction, "normal")

    async def _cb_color_sel(self, interaction: discord.Interaction):
        if await self._guard(interaction):
            self._color_key = interaction.data["values"][0]
            await self._refresh(interaction, "normal")

    async def _cb_edit_sel(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        index = int(interaction.data["values"][0])
        await interaction.response.send_modal(EB_MODALS[self._data[index]["type"]](self, index))

    async def _cb_remove_sel(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self._data.pop(int(interaction.data["values"][0]))
        self._reorder_index = None
        await self._refresh(interaction, "normal")

    async def _cb_reorder_sel(self, interaction: discord.Interaction):
        if await self._guard(interaction):
            self._reorder_index = int(interaction.data["values"][0])
            await self._refresh(interaction)

    async def _cb_move_up(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        i = self._reorder_index
        if i is not None and i > 0:
            self._data[i], self._data[i - 1] = self._data[i - 1], self._data[i]
            self._reorder_index = i - 1
        await self._refresh(interaction)

    async def _cb_move_down(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        i = self._reorder_index
        if i is not None and i < len(self._data) - 1:
            self._data[i], self._data[i + 1] = self._data[i + 1], self._data[i]
            self._reorder_index = i + 1
        await self._refresh(interaction)

    async def _cb_channel_sel(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        channel = interaction.guild.get_channel(int(interaction.data["values"][0]))
        if channel is None:
            await interaction.response.send_message("Could not find that channel.", ephemeral=True)
            return
        try:
            await channel.send(
                view=eb_make_view(self._data, self._color_key),
                allowed_mentions=discord.AllowedMentions(everyone=False, roles=True, users=True)
            )
        except discord.Forbidden:
            await interaction.response.send_message(f"I don't have permission to send messages in {channel.mention}.", ephemeral=True)
            return
        except Exception as exc:
            await interaction.response.send_message(f"Failed to send: {exc}", ephemeral=True)
            return
        await self._refresh(interaction, "done", f"Your container was sent to {channel.mention}.")

    async def _cb_reset(self, interaction: discord.Interaction):
        if await self._guard(interaction):
            self._color_key = "none"
            self._data = []
            self._reorder_index = None
            await self._refresh(interaction, "normal")


@tree.command(name="embed", description="Build and send a Components V2 container", guild=discord.Object(id=GUILD_ID))
@discord.app_commands.default_permissions(manage_messages=True)
async def embed_builder(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message(view=tk_err("You need Manage Messages to use this."), ephemeral=True)
        return
    await interaction.response.send_message(view=EmbedBuilderView(interaction.user.id), ephemeral=True)


AR_MATCHES = {"exact": "Exact message", "starts": "Starts with", "contains": "Contains word"}
AR_MAX = 50
ar_cooldowns = {}


def ar_list() -> list:
    return config.setdefault("autoresponders", [])


def ar_get(trigger: str):
    t = trigger.strip().lower()
    for r in ar_list():
        if r["trigger"] == t:
            return r
    return None


def ar_store(trigger: str, match: str, plain, data, color_key: str) -> None:
    t = trigger.strip().lower()
    items = ar_list()
    items[:] = [r for r in items if r["trigger"] != t]
    items.append({"trigger": t, "match": match, "plain": plain, "data": data or [], "color": color_key})
    save_config(config)


def ar_find(content: str):
    text = content.strip().lower()
    if not text:
        return None
    items = ar_list()
    for r in sorted((r for r in items if r["match"] == "exact"), key=lambda r: -len(r["trigger"])):
        if text == r["trigger"]:
            return r
    for r in sorted((r for r in items if r["match"] == "starts"), key=lambda r: -len(r["trigger"])):
        if text.startswith(r["trigger"]):
            return r
    for r in sorted((r for r in items if r["match"] == "contains"), key=lambda r: -len(r["trigger"])):
        if re.search(rf"(?<!\w){re.escape(r['trigger'])}(?!\w)", text):
            return r
    return None


def ar_vars(text: str, message: discord.Message) -> str:
    return (
        text.replace("{user}", message.author.mention)
        .replace("{username}", message.author.display_name)
        .replace("{server}", message.guild.name)
        .replace("{channel}", message.channel.mention)
        .replace("{membercount}", str(message.guild.member_count))
    )


async def ar_check(message: discord.Message) -> bool:
    if not message.guild or message.guild.id != GUILD_ID or not message.content:
        return False
    r = ar_find(message.content)
    if not r:
        return False
    key = (message.channel.id, r["trigger"])
    now = time.time()
    if now - ar_cooldowns.get(key, 0) < 5:
        return True
    ar_cooldowns[key] = now
    mentions = discord.AllowedMentions(users=True, roles=False, everyone=False)
    try:
        if r.get("plain") is not None:
            await message.reply(ar_vars(r["plain"], message), mention_author=False, allowed_mentions=mentions)
        else:
            view = eb_make_view(r["data"], r.get("color", "none"), lambda s: ar_vars(s, message))
            await message.reply(view=view, mention_author=False, allowed_mentions=mentions)
    except Exception as e:
        print(f"Auto responder failed: {e}")
    return True


def ar_builder(author_id: int, trigger: str, match: str, data=None, color_key="none") -> EmbedBuilderView:
    async def cb(interaction, built, color):
        ar_store(trigger, match, None, built, color)
        return f"Auto responder saved.\n**Trigger:** `{trigger}`\n**Match:** {AR_MATCHES[match]}"
    return EmbedBuilderView(author_id, data, color_key, cb, f"Auto Responder — {trigger}")


def ar_summary() -> str:
    items = ar_list()
    if not items:
        return "No auto responders yet."
    lines = []
    for r in items:
        if r.get("plain") is not None:
            preview = r["plain"][:50].replace("\n", " ")
        else:
            first = next((c for c in r["data"] if c["type"] in ("text", "section")), None)
            preview = f"[container] {first['content'][:40].replace(chr(10), ' ')}" if first else "[container]"
        lines.append(f"`{r['trigger']}` ({AR_MATCHES[r['match']]}) → {preview}")
    text = "\n".join(lines)
    return text[:3500]


ar_group = discord.app_commands.Group(
    name="autoresponder",
    description="Auto responder commands",
    guild_ids=[GUILD_ID],
    default_permissions=discord.Permissions(manage_guild=True)
)


async def ar_autocomplete(interaction: discord.Interaction, current: str):
    return [
        discord.app_commands.Choice(name=r["trigger"][:100], value=r["trigger"])
        for r in ar_list() if current.lower() in r["trigger"]
    ][:25]


def ar_denied(user) -> bool:
    return not user.guild_permissions.manage_guild


@ar_group.command(name="add", description="Add an auto responder")
@discord.app_commands.describe(
    trigger="Word or message that triggers the bot",
    response="Quick text reply. Leave empty to build a container instead",
    match="How the trigger is matched"
)
@discord.app_commands.choices(match=[
    discord.app_commands.Choice(name="Exact message", value="exact"),
    discord.app_commands.Choice(name="Starts with", value="starts"),
    discord.app_commands.Choice(name="Contains word", value="contains"),
])
async def ar_add(interaction: discord.Interaction, trigger: str, response: str = None, match: str = "exact"):
    if ar_denied(interaction.user):
        await interaction.response.send_message(view=tk_err("You need Manage Server to use this."), ephemeral=True)
        return
    trigger = trigger.strip().lower()
    if not trigger or len(trigger) > 100:
        await interaction.response.send_message(view=tk_err("Trigger must be 1-100 characters."), ephemeral=True)
        return
    if not ar_get(trigger) and len(ar_list()) >= AR_MAX:
        await interaction.response.send_message(view=tk_err(f"Maximum of {AR_MAX} auto responders."), ephemeral=True)
        return
    if response:
        ar_store(trigger, match, response, [], "none")
        await interaction.response.send_message(
            view=tk_ok("Auto Responder Saved", f"**Trigger:** `{trigger}`\n**Match:** {AR_MATCHES[match]}\n**Reply:** {response[:300]}"),
            ephemeral=True
        )
        return
    await interaction.response.send_message(view=ar_builder(interaction.user.id, trigger, match), ephemeral=True)


@ar_group.command(name="edit", description="Edit an auto responder in the container builder")
@discord.app_commands.describe(trigger="Trigger to edit")
@discord.app_commands.autocomplete(trigger=ar_autocomplete)
async def ar_edit(interaction: discord.Interaction, trigger: str):
    if ar_denied(interaction.user):
        await interaction.response.send_message(view=tk_err("You need Manage Server to use this."), ephemeral=True)
        return
    r = ar_get(trigger)
    if not r:
        await interaction.response.send_message(view=tk_err("That auto responder doesn't exist."), ephemeral=True)
        return
    data = [{"type": "text", "content": r["plain"]}] if r.get("plain") is not None else r["data"]
    await interaction.response.send_message(
        view=ar_builder(interaction.user.id, r["trigger"], r["match"], data, r.get("color", "none")),
        ephemeral=True
    )


@ar_group.command(name="remove", description="Remove an auto responder")
@discord.app_commands.describe(trigger="Trigger to remove")
@discord.app_commands.autocomplete(trigger=ar_autocomplete)
async def ar_remove(interaction: discord.Interaction, trigger: str):
    if ar_denied(interaction.user):
        await interaction.response.send_message(view=tk_err("You need Manage Server to use this."), ephemeral=True)
        return
    r = ar_get(trigger)
    if not r:
        await interaction.response.send_message(view=tk_err("That auto responder doesn't exist."), ephemeral=True)
        return
    ar_list().remove(r)
    save_config(config)
    await interaction.response.send_message(view=tk_ok("Auto Responder Removed", f"`{r['trigger']}` was removed."), ephemeral=True)


@ar_group.command(name="list", description="List all auto responders")
async def ar_list_cmd(interaction: discord.Interaction):
    if ar_denied(interaction.user):
        await interaction.response.send_message(view=tk_err("You need Manage Server to use this."), ephemeral=True)
        return
    await interaction.response.send_message(view=TicketInfoView(f"Auto Responders ({len(ar_list())})", ar_summary()), ephemeral=True)


@ar_group.command(name="clear", description="Remove every auto responder")
async def ar_clear(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(view=tk_err("You need Administrator to use this."), ephemeral=True)
        return
    ar_list().clear()
    save_config(config)
    await interaction.response.send_message(view=tk_ok("Auto Responders Cleared", "All auto responders were removed."), ephemeral=True)


tree.add_command(ar_group, guild=discord.Object(id=GUILD_ID))


async def ar_prefix(message: discord.Message):
    parts = message.content[len(PREFIX):].split(None, 2)
    sub = parts[1].lower() if len(parts) > 1 else ""
    rest = parts[2].strip() if len(parts) > 2 else ""
    usage = (
        "`.ar add <trigger> | <reply>`\n"
        "`.ar add <trigger>` (opens the container builder)\n"
        "`.ar add contains:<trigger> | <reply>` or `starts:<trigger>`\n"
        "`.ar edit <trigger>` `.ar remove <trigger>` `.ar list`"
    )
    if sub == "add" and rest:
        trig_part, sep, reply = rest.partition("|")
        trig = trig_part.strip().lower()
        match = "exact"
        for key in ("contains", "starts", "exact"):
            if trig.startswith(f"{key}:"):
                match = key
                trig = trig[len(key) + 1:].strip()
                break
        reply = reply.strip()
        if not trig or len(trig) > 100:
            await message.channel.send(view=tk_err("Trigger must be 1-100 characters."))
            return
        if not ar_get(trig) and len(ar_list()) >= AR_MAX:
            await message.channel.send(view=tk_err(f"Maximum of {AR_MAX} auto responders."))
            return
        if sep and reply:
            ar_store(trig, match, reply, [], "none")
            await message.channel.send(view=tk_ok("Auto Responder Saved", f"**Trigger:** `{trig}`\n**Match:** {AR_MATCHES[match]}\n**Reply:** {reply[:300]}"))
            return
        await message.channel.send(view=ar_builder(message.author.id, trig, match))
    elif sub == "edit" and rest:
        r = ar_get(rest)
        if not r:
            await message.channel.send(view=tk_err("That auto responder doesn't exist."))
            return
        data = [{"type": "text", "content": r["plain"]}] if r.get("plain") is not None else r["data"]
        await message.channel.send(view=ar_builder(message.author.id, r["trigger"], r["match"], data, r.get("color", "none")))
    elif sub in ("remove", "delete") and rest:
        r = ar_get(rest)
        if not r:
            await message.channel.send(view=tk_err("That auto responder doesn't exist."))
            return
        ar_list().remove(r)
        save_config(config)
        await message.channel.send(view=tk_ok("Auto Responder Removed", f"`{r['trigger']}` was removed."))
    elif sub == "list":
        await message.channel.send(view=TicketInfoView(f"Auto Responders ({len(ar_list())})", ar_summary()))
    else:
        await message.channel.send(view=TicketInfoView("Auto Responder", usage))


@bot.event
async def on_ready():
    bot.add_view(VerifyView())
    bot.add_view(GiveawayPersistentView())
    bot.add_view(TicketPanelPersistentView())
    bot.add_view(TicketControlsPersistentView())
    asyncio.create_task(giveaway_loop())
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print(f"Logged in as {bot.user}")
    print(f"Reference images loaded: {len(reference_hashes)}")


bot.run(TOKEN)
