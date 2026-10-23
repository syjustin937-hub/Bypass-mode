import discord
import aiohttp
import asyncio
import urllib.parse
import json
import os
import re
import random
import time
import hashlib
import base64
import unicodedata
import ipaddress
import difflib
import secrets
import math
import ast
import operator
import socket
import ssl
import sys
import uuid
import string
import http
import platform
import colorsys
import codecs
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
PREFIX = "!"

if not TOKEN:
    raise ValueError("Missing TOKEN in .env")


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
        f"Giveaway ended! **{gw['prize']}**\nWinner(s): {winner_text}",
        reference=message
    )
    await log_bot(
        channel.guild,
        "Giveaway Ended",
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
        super().__init__(label=f"Enter ({count})", style=discord.ButtonStyle.primary, custom_id="giveaway_enter")

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
            await interaction.response.send_message("You entered the giveaway!", ephemeral=True)


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
        "Information",
        "`.userinfo [@user]` View info about a user\n"
        "`.serverinfo` View info about this server\n"
        "`.roleinfo <role>` View info about a role\n"
        "`.avatar [@user]` Get a user's avatar\n"
        "`.banner [@user]` Get a user's banner\n"
        "`.botinfo` View bot information\n"
        "`.stats` View server and bot stats\n"
        "`.ping` Check bot latency"
    ),
    (
        "Tools",
        "`.afk [reason]` Set your AFK status\n"
        "`.steal <emoji>` Copy a custom emoji\n"
        "`.stealsticker` Copy a sticker (reply to it)"
    ),
    (
        "More",
        "`.say <message>` Send a message as the bot\n"
        "`.embed` Open the container builder\n"
        "`.bypass <link>` Bypass a link\n"
        "`.emojis` List all server emojis\n"
        "`.activity <name>` Start a voice activity\n"
        "`.ascii <text>` Fancy Unicode fonts\n"
        "`.base <num> <from> [to]` Convert number bases\n"
        "`.base32` `.base64` `.binary` Encode or decode\n"
        "`.char <text>` Unicode character info\n"
        "`.cidr <ip/prefix>` Subnet calculator\n"
        "`.counting setup #channel` Counting game\n"
        "`.todo` Your personal todo list\n"
        "`.help` Show this menu"
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
        "Moderation Plus",
        "**Messaging & Roles**\n"
        "`.dm @user <message>` `.roleall <add|remove> @role [humans|bots|everyone]`\n"
        "`.massrole <add|remove> @role @user...` `.giverole @user @role`\n"
        "`.role create|delete|rename|color|hoist|mentionable|add|remove`\n"
        "`.reactionrole add|remove|list` `.stickyroles <on|off|status>`\n"
        "`.staffroles <add|remove|list> [@role]` `.extraowner <add|remove|list> [@user]`\n\n"
        "**Punishments**\n"
        "`.jail setup` `.jail @user [reason]` `.unjail @user`\n"
        "`.softban @user [reason]` `.restrict @user` `.picperms @user [#channel]`\n"
        "`.forcenick @user <nickname|off>` `.honeypot setup #channel <timeout|quarantine|kick|ban|softban>`\n"
        "`.masspurge @user [limit]` `.moveall #voice`\n\n"
        "**Staff Tools**\n"
        "`.modmail <setup|off|status|reply|close|block|unblock>`\n"
        "`.modnote <add|view|remove> @user` `.modstats [days]`\n"
        "`.ignoredchannels <add|remove|list> [#channel]`\n\n"
        "Every command also works as a slash command"
    ),
    (
        "Logs",
        "**Logs (Admin)**\n"
        "`.logs` `.setlog <category|all> #channel` `.dellog <category|all>`\n"
        "Categories: `mod` `message` `member` `voice` `server` `bot`"
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
        "Variables: `{user}` `{username}` `{server}` `{channel}` `{membercount}` `{avatar}`"
    ),
    (
        "Welcome",
        "`/welcome setup #channel [role]`\n"
        "`/welcome design` (container builder)\n"
        "`/welcome test` `/welcome toggle`\n"
        "`/welcome autorole [role]` `/welcome reset` `/welcome status`\n\n"
        "Prefix: `.welcome setup #channel` `.welcome design` `.welcome test`\n"
        "Variables: `{user}` `{username}` `{server}` `{membercount}` `{avatar}`"
    ),
]


HELP_CATEGORIES = [
    ("Moderation", ["Moderation", "Moderation Plus", "Logs"]),
    ("Utility", ["Information", "Tools"]),
    ("Server Setup", ["Welcome", "Giveaway, Ticket & Auto Responder"]),
    ("More", ["More"]),
]


class HelpNavButton(discord.ui.Button):
    def __init__(self, label: str, category, page: int, view_ref, style=discord.ButtonStyle.secondary, disabled: bool = False):
        super().__init__(label=label, style=style, disabled=disabled)
        self.category = category
        self.page = page
        self.view_ref = view_ref

    async def callback(self, interaction: discord.Interaction):
        owner = self.view_ref.author_id
        if owner and interaction.user.id != owner:
            await interaction.response.send_message("Only the person who ran this command can use these buttons.", ephemeral=True)
            return
        await interaction.response.edit_message(view=HelpView(owner, self.category, self.page))


class HelpView(discord.ui.LayoutView):
    def __init__(self, author_id: int = None, category: str = None, page: int = 0):
        super().__init__(timeout=300)
        self.author_id = author_id
        page_names = dict(HELP_CATEGORIES).get(category)
        bodies = dict(HELP_PAGES)
        if page_names is None:
            name = bot.user.name if bot.user else "Help"
            header = discord.ui.TextDisplay(f"# {name}\nPrefix `{PREFIX}` · Browse the categories below")
            container = discord.ui.Container()
            if bot.user:
                section = discord.ui.Section(accessory=discord.ui.Thumbnail(media=bot.user.display_avatar.url))
                section.add_item(header)
                container.add_item(section)
            else:
                container.add_item(header)
            container.add_item(discord.ui.Separator())
            buttons = [HelpNavButton(cat, cat, 0, self) for cat, _ in HELP_CATEGORIES]
            for start in range(0, len(buttons), 3):
                container.add_item(discord.ui.ActionRow(*buttons[start:start + 3]))
            self.add_item(container)
            return
        total = len(page_names)
        self.page = max(0, min(page, total - 1))
        page_name = page_names[self.page]
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(f"# {category}: Page {self.page + 1}/{total}\n-# {page_name}"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(bodies[page_name]),
                discord.ui.Separator(),
                discord.ui.TextDisplay(f"-# Prefix: `{PREFIX}` | Slash commands work too"),
                discord.ui.ActionRow(
                    HelpNavButton("Back", None, 0, self, discord.ButtonStyle.primary),
                    HelpNavButton("Previous", category, self.page - 1, self, disabled=self.page == 0),
                    HelpNavButton("Next", category, self.page + 1, self, disabled=self.page >= total - 1),
                ),
            )
        )


class AfkSetView(discord.ui.LayoutView):
    def __init__(self, member, reason: str):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# You are now AFK"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"**User:** {member.mention}\n"
                    f"**Reason:** {reason}"
                ),
            )
        )


class AfkReturnView(discord.ui.LayoutView):
    def __init__(self, member):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(f"# Welcome back, {member.display_name}!"),
                discord.ui.Separator(),
                discord.ui.TextDisplay("Your AFK status has been removed."),
            )
        )


class AfkMentionView(discord.ui.LayoutView):
    def __init__(self, member, reason: str, timestamp: int):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(f"# {member.display_name} is AFK"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"**Reason:** {reason}\n"
                    f"**Since:** <t:{timestamp}:R>"
                ),
            )
        )


class VerifyEphemeralView(discord.ui.LayoutView):
    def __init__(self, state: str, message: str):
        super().__init__(timeout=None)
        if state == "success":
            self.add_item(
                discord.ui.Container(
                    discord.ui.TextDisplay("# You have been verified!"),
                    discord.ui.Separator(),
                    discord.ui.TextDisplay(message),
                )
            )
        else:
            self.add_item(
                discord.ui.Container(
                    discord.ui.TextDisplay(f"# {message}"),
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
            )
        )


class GiveawayActiveView(discord.ui.LayoutView):
    def __init__(self, prize: str, winner_count: int, end_timestamp: int, entry_count: int = 0):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# GIVEAWAY"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"**Prize:** {prize}\n"
                    f"**Winners:** {winner_count}\n"
                    f"**Ends:** <t:{end_timestamp}:R>\n"
                    f"**Entries:** {entry_count}"
                ),
                discord.ui.Separator(),
                discord.ui.ActionRow(GiveawayEnterButton(entry_count)),
            )
        )


class GiveawayEndedView(discord.ui.LayoutView):
    def __init__(self, prize: str, winner_count: int, entry_count: int):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# GIVEAWAY ENDED"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"**Prize:** {prize}\n"
                    f"**Winners:** {winner_count}\n"
                    f"**Total Entries:** {entry_count}"
                ),
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


def err(msg: str) -> discord.ui.LayoutView:
    return InfoView("Failed!", msg, discord.Color.red())


def ok(title: str, body: str) -> discord.ui.LayoutView:
    return InfoView(title, body, discord.Color.green())


LOG_CATEGORIES = {
    "mod": "Moderation",
    "message": "Messages",
    "member": "Members",
    "voice": "Voice",
    "server": "Server",
    "bot": "Bot",
}

ACTION_STYLE = {
    "Kick": ("", discord.Color.orange()),
    "Ban": ("", discord.Color.red()),
    "Unban": ("", discord.Color.green()),
    "Timeout": ("", discord.Color.yellow()),
    "Untimeout": ("", discord.Color.green()),
    "Warn": ("", discord.Color.yellow()),
    "Clear Warns": ("", discord.Color.green()),
    "Purge": ("", discord.Color.blurple()),
    "Lock": ("", discord.Color.red()),
    "Unlock": ("", discord.Color.green()),
    "Slowmode": ("", discord.Color.blurple()),
    "Auto Kick": ("", discord.Color.red()),
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
    mx_record(action, moderator)
    icon, color = ACTION_STYLE.get(action, ("", discord.Color.blurple()))
    lines = [f"**Moderator:** {fmt_ref(moderator)}"]
    if target is not None:
        lines.append(f"**Target:** {fmt_ref(target)}")
    if reason:
        lines.append(f"**Reason:** {clip(reason, 500)}")
    if extra:
        lines.append(extra)
    await send_log(guild, "mod", LogView(action, "\n".join(lines), color))


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
    await send_log(message.guild, "message", LogView("Message Deleted", body, discord.Color.red()))


def logs_status_view(guild) -> discord.ui.LayoutView:
    cfg = config.get("log_channels", {})
    lines = []
    for key, label in LOG_CATEGORIES.items():
        cid = cfg.get(key)
        ch = guild.get_channel(cid) if cid else None
        lines.append(f"{label} (`{key}`) — {ch.mention if ch else '`Off`'}")
    lines.append("\nSet with `/logs set` or `.setlog <category|all> #channel`")
    return InfoView("Log Channels", "\n".join(lines))


async def apply_log_channel(category: str, channel) -> discord.ui.LayoutView:
    if category != "all" and category not in LOG_CATEGORIES:
        keys = ", ".join(f"`{k}`" for k in LOG_CATEGORIES)
        return err(f"Unknown category. Use: {keys}, `all`")
    label = "all" if category == "all" else LOG_CATEGORIES[category]
    try:
        await channel.send(
            view=LogView("Log Channel Set", f"This channel will now receive **{label}** logs.", discord.Color.green()),
            allowed_mentions=NO_MENTIONS
        )
    except Exception:
        return err(f"I can't send messages in {channel.mention}.")
    targets = list(LOG_CATEGORIES) if category == "all" else [category]
    cfg = config.setdefault("log_channels", {})
    for key in targets:
        cfg[key] = channel.id
    save_config(config)
    return ok("Log Channel Updated", f"**{label}** logs → {channel.mention}")


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
    return ok("Logging Disabled", f"**{label}** logs are now off.")


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
        "Giveaway Started",
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
            await channel.send(f"Giveaway rerolled! **{gw['prize']}**\nNew winner(s): {winner_text}", reference=original)
        except Exception:
            pass
    await interaction.response.send_message(view=GiveawayRerollView(picked), ephemeral=True)
    await log_bot(
        interaction.guild,
        "Giveaway Rerolled",
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


@bot.event
async def on_message_delete(message):
    if message.author.bot:
        return
    if message.guild and not message.content.lower().startswith((f"{PREFIX}purge", f"{PREFIX}say")):
        await log_message_delete(message)


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if await mx_message_hook(message):
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

    if not message.content.startswith(PREFIX):
        if message.guild and await counting_check(message):
            return
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

    if await logs_prefix(message, cmd, args):
        return

    if await util_prefix(message, cmd, args):
        return

    if await mx_prefix(message, cmd, args):
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

    elif cmd == "welcome":
        if not message.author.guild_permissions.manage_guild:
            return
        await wl_prefix(message)

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
            "Giveaway Started",
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
                await channel.send(f"Giveaway rerolled! **{gw['prize']}**\nNew winner(s): {winner_text}", reference=original)
            except Exception:
                pass
        await message.channel.send(view=GiveawayRerollView(picked))
        await log_bot(
            message.guild,
            "Giveaway Rerolled",
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
    await send_log(after.guild, "message", LogView("Message Edited", body, discord.Color.orange()))


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
    await send_log(guild, "message", LogView("Bulk Delete", body, discord.Color.red()))


@bot.event
async def on_member_join(member):
    if member.guild.id != GUILD_ID:
        return
    asyncio.create_task(wl_on_join(member))
    asyncio.create_task(mx_on_join(member))
    age = (discord.utils.utcnow() - member.created_at).days
    body = (
        f"**User:** {fmt_ref(member)}\n"
        f"**Account Created:** <t:{int(member.created_at.timestamp())}:R>\n"
        f"**Account Age:** {age} day(s)\n"
        f"**Member Count:** {member.guild.member_count}"
    )
    if age < 7:
        body += "\n**New account**"
    await send_log(member.guild, "member", LogView("Member Joined", body, discord.Color.green()))


@bot.event
async def on_member_remove(member):
    guild = member.guild
    if guild.id != GUILD_ID:
        return
    mx_on_leave(member)
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
    await send_log(guild, "member", LogView("Member Left", body, discord.Color.red()))


@bot.event
async def on_member_ban(guild, user):
    if guild.id != GUILD_ID:
        return
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
    asyncio.create_task(mx_on_member_update(before, after))
    if before.nick != after.nick:
        entry = await find_audit(guild, discord.AuditLogAction.member_update, after.id)
        body = (
            f"**Member:** {fmt_ref(after)}\n"
            f"**Before:** {before.nick or '*none*'}\n"
            f"**After:** {after.nick or '*none*'}"
        )
        if entry and entry.user and entry.user.id != after.id:
            body += f"\n**Changed by:** {fmt_ref(entry.user)}"
        await send_log(guild, "member", LogView("Nickname Changed", body, discord.Color.blurple()))
    added = [r for r in after.roles if r not in before.roles]
    removed = [r for r in before.roles if r not in after.roles]
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
        await send_log(guild, "member", LogView("Roles Updated", body, color))
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
        title, color = "Voice Join", discord.Color.green()
        body = f"{fmt_ref(member)} joined {after.channel.mention}"
    elif after.channel is None:
        title, color = "Voice Leave", discord.Color.red()
        body = f"{fmt_ref(member)} left {before.channel.mention}"
    else:
        title, color = "Voice Move", discord.Color.blurple()
        body = f"{fmt_ref(member)} moved from {before.channel.mention} to {after.channel.mention}"
    await send_log(member.guild, "voice", LogView(title, body, color))


@bot.event
async def on_guild_channel_create(channel):
    if channel.guild.id != GUILD_ID:
        return
    entry = await find_audit(channel.guild, discord.AuditLogAction.channel_create, channel.id)
    body = f"**Channel:** {channel.mention} (`{channel.id}`)\n**Type:** {str(channel.type).replace('_', ' ').title()}"
    if entry and entry.user:
        body += f"\n**Created by:** {fmt_ref(entry.user)}"
    await send_log(channel.guild, "server", LogView("Channel Created", body, discord.Color.green()))


@bot.event
async def on_guild_channel_delete(channel):
    if channel.guild.id != GUILD_ID:
        return
    await ticket_channel_deleted(channel)
    entry = await find_audit(channel.guild, discord.AuditLogAction.channel_delete, channel.id)
    body = f"**Channel:** #{channel.name} (`{channel.id}`)\n**Type:** {str(channel.type).replace('_', ' ').title()}"
    if entry and entry.user:
        body += f"\n**Deleted by:** {fmt_ref(entry.user)}"
    await send_log(channel.guild, "server", LogView("Channel Deleted", body, discord.Color.red()))


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
    await send_log(after.guild, "server", LogView("Channel Updated", body, discord.Color.orange()))


@bot.event
async def on_guild_role_create(role):
    if role.guild.id != GUILD_ID:
        return
    entry = await find_audit(role.guild, discord.AuditLogAction.role_create, role.id)
    body = f"**Role:** {role.mention} (`{role.id}`)"
    if entry and entry.user:
        body += f"\n**Created by:** {fmt_ref(entry.user)}"
    await send_log(role.guild, "server", LogView("Role Created", body, discord.Color.green()))


@bot.event
async def on_guild_role_delete(role):
    if role.guild.id != GUILD_ID:
        return
    entry = await find_audit(role.guild, discord.AuditLogAction.role_delete, role.id)
    body = f"**Role:** {role.name} (`{role.id}`)"
    if entry and entry.user:
        body += f"\n**Deleted by:** {fmt_ref(entry.user)}"
    await send_log(role.guild, "server", LogView("Role Deleted", body, discord.Color.red()))


@bot.event
async def on_guild_role_update(before, after):
    if after.guild.id != GUILD_ID:
        return
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
    await send_log(after.guild, "server", LogView("Role Updated", body, discord.Color.orange()))


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
    await send_log(after, "server", LogView("Server Updated", body, discord.Color.orange()))


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
    await send_log(guild, "server", LogView("Invite Created", body, discord.Color.green()))


@bot.event
async def on_invite_delete(invite):
    guild = bot.get_guild(invite.guild.id) if invite.guild else None
    if guild is None:
        return
    body = f"**Code:** `{invite.code}`\n**Channel:** {invite.channel.mention if invite.channel else 'Unknown'}"
    await send_log(guild, "server", LogView("Invite Deleted", body, discord.Color.red()))


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
            view=tk_ok("Ticket Claimed", f"{interaction.user.mention} will be assisting <@{data['user_id']}>."),
            allowed_mentions=discord.AllowedMentions.none()
        )


class TicketCloseButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Close",
            style=discord.ButtonStyle.danger,
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

    @discord.ui.button(label="Confirm Close", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Closing ticket...", view=None)
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
        status = f"Claimed by <@{data['claimed_by']}>" if data.get("claimed_by") else "Waiting for staff"
        product = data.get("product") or "Not specified"
        tried = data.get("tried") or "Not specified"
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# Support Ticket"),
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
        await interaction.followup.send(view=tk_ok("Ticket Created", f"Your ticket is ready: {channel.mention}"), ephemeral=True)

        log_ch = guild.get_channel(t["log_id"]) if t.get("log_id") else None
        if log_ch:
            try:
                await log_ch.send(view=TicketInfoView(
                    "Ticket Opened",
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
                    "Ticket Closed",
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
                f"Your ticket **{channel.name}** in **{guild.name}** was closed. Transcript attached.",
                file=discord.File(BytesIO(transcript), filename=fname)
            )
        except Exception:
            pass

    try:
        await channel.send(view=TicketInfoView("Ticket Closed", f"Closed by {closer.mention}. Deleting in 5 seconds...", discord.Color.red()))
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
            section = discord.ui.Section(accessory=discord.ui.Thumbnail(media=tf(url)))
            section.add_item(discord.ui.TextDisplay(tf(comp["content"])))
            container.add_item(section)
        else:
            container.add_item(discord.ui.TextDisplay(tf(comp["content"])))
    elif t == "media_gallery":
        if comp["urls"]:
            gallery = discord.ui.MediaGallery()
            for u in comp["urls"]:
                gallery.add_item(media=tf(u))
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
        if url and not (url.startswith(("http://", "https://")) or url == "{avatar}"):
            await interaction.response.send_message("Thumbnail must be a valid http(s) URL or `{avatar}`.", ephemeral=True)
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
        lines = [ln.strip() for ln in self._urls.value.splitlines() if ln.strip().startswith(("http://", "https://")) or ln.strip() == "{avatar}"]
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
                eb_render_into(c, comp, lambda s: s.replace("{avatar}", "https://cdn.discordapp.com/embed/avatars/0.png"))
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
                view=eb_make_view(self._data, self._color_key, lambda s: s.replace("{avatar}", interaction.user.display_avatar.url)),
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
        .replace("{avatar}", message.author.display_avatar.url)
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


def wl_cfg() -> dict:
    c = config.setdefault("welcome", {})
    c.setdefault("enabled", False)
    c.setdefault("channel_id", None)
    c.setdefault("autorole_id", None)
    c.setdefault("data", None)
    c.setdefault("color", "none")
    return c


def wl_default_data() -> list:
    return [
        {
            "type": "section",
            "content": "# Welcome to {server}!\nHey {user}, glad to have you here.\nYou are member **#{membercount}**.",
            "thumbnail_url": "{avatar}"
        },
        {"type": "separator", "divider": True, "spacing": "small"},
        {"type": "text", "content": "-# Make yourself at home and enjoy your stay."}
    ]


def wl_vars(text: str, member: discord.Member) -> str:
    return (
        text.replace("{user}", member.mention)
        .replace("{username}", member.display_name)
        .replace("{server}", member.guild.name)
        .replace("{membercount}", str(member.guild.member_count))
        .replace("{avatar}", member.display_avatar.url)
    )


async def wl_send(member: discord.Member, channel) -> None:
    c = wl_cfg()
    data = c.get("data") or wl_default_data()
    view = eb_make_view(data, c.get("color", "none"), lambda s: wl_vars(s, member))
    await channel.send(view=view, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))


async def wl_on_join(member: discord.Member) -> None:
    if member.bot:
        return
    c = wl_cfg()
    if c.get("autorole_id"):
        role = member.guild.get_role(c["autorole_id"])
        if role:
            try:
                await member.add_roles(role, reason="Welcome auto role")
            except Exception as e:
                print(f"Welcome auto role failed: {e}")
    if not c.get("enabled") or not c.get("channel_id"):
        return
    channel = member.guild.get_channel(c["channel_id"])
    if channel is None:
        return
    try:
        await wl_send(member, channel)
    except Exception as e:
        print(f"Welcome message failed: {e}")


def wl_builder(author_id: int) -> EmbedBuilderView:
    c = wl_cfg()

    async def cb(interaction, built, color):
        cfg = wl_cfg()
        cfg["data"] = built
        cfg["color"] = color
        save_config(config)
        return "Welcome message saved. Use `/welcome test` to preview it."

    return EmbedBuilderView(author_id, c.get("data") or wl_default_data(), c.get("color", "none"), cb, "Welcome Message")


def wl_status_text(guild: discord.Guild) -> str:
    c = wl_cfg()
    channel = guild.get_channel(c["channel_id"]) if c.get("channel_id") else None
    role = guild.get_role(c["autorole_id"]) if c.get("autorole_id") else None
    return (
        f"**Status:** {'Enabled' if c.get('enabled') else 'Disabled'}\n"
        f"**Channel:** {channel.mention if channel else 'Not set'}\n"
        f"**Auto role:** {role.mention if role else 'None'}\n"
        f"**Message:** {'Custom' if c.get('data') else 'Default'}\n\n"
        "**Variables:** `{user}` `{username}` `{server}` `{membercount}` `{avatar}`"
    )


def wl_denied(user) -> bool:
    return not user.guild_permissions.manage_guild


wl_group = discord.app_commands.Group(
    name="welcome",
    description="Welcome system commands",
    guild_ids=[GUILD_ID],
    default_permissions=discord.Permissions(manage_guild=True)
)


@wl_group.command(name="setup", description="Set the welcome channel and enable the welcome system")
@discord.app_commands.describe(channel="Channel where welcome messages are sent", autorole="Role given to new members (optional)")
async def wl_setup(interaction: discord.Interaction, channel: discord.TextChannel, autorole: discord.Role = None):
    if wl_denied(interaction.user):
        await interaction.response.send_message(view=tk_err("You need Manage Server to use this."), ephemeral=True)
        return
    c = wl_cfg()
    c["channel_id"] = channel.id
    c["enabled"] = True
    if autorole:
        c["autorole_id"] = autorole.id
    save_config(config)
    await interaction.response.send_message(
        view=TicketInfoView("Welcome System Ready", wl_status_text(interaction.guild) + "\n\nUse `/welcome design` to customize the message."),
        ephemeral=True
    )


@wl_group.command(name="design", description="Design the welcome message in the container builder")
async def wl_design(interaction: discord.Interaction):
    if wl_denied(interaction.user):
        await interaction.response.send_message(view=tk_err("You need Manage Server to use this."), ephemeral=True)
        return
    await interaction.response.send_message(view=wl_builder(interaction.user.id), ephemeral=True)


@wl_group.command(name="test", description="Send a test welcome message using your account")
async def wl_test(interaction: discord.Interaction):
    if wl_denied(interaction.user):
        await interaction.response.send_message(view=tk_err("You need Manage Server to use this."), ephemeral=True)
        return
    c = wl_cfg()
    channel = interaction.guild.get_channel(c["channel_id"]) if c.get("channel_id") else interaction.channel
    try:
        await wl_send(interaction.user, channel)
    except Exception as e:
        await interaction.response.send_message(view=tk_err(f"Could not send the test message: `{e}`"), ephemeral=True)
        return
    await interaction.response.send_message(view=tk_ok("Test Sent", f"Sent a test welcome message to {channel.mention}."), ephemeral=True)


@wl_group.command(name="toggle", description="Turn the welcome message on or off")
async def wl_toggle(interaction: discord.Interaction):
    if wl_denied(interaction.user):
        await interaction.response.send_message(view=tk_err("You need Manage Server to use this."), ephemeral=True)
        return
    c = wl_cfg()
    if not c.get("channel_id"):
        await interaction.response.send_message(view=tk_err("Set a channel first with `/welcome setup`."), ephemeral=True)
        return
    c["enabled"] = not c["enabled"]
    save_config(config)
    await interaction.response.send_message(view=tk_ok("Welcome System", f"Welcome messages are now **{'enabled' if c['enabled'] else 'disabled'}**."), ephemeral=True)


@wl_group.command(name="autorole", description="Set or clear the role given to new members")
@discord.app_commands.describe(role="Role to give (leave empty to clear)")
async def wl_autorole(interaction: discord.Interaction, role: discord.Role = None):
    if wl_denied(interaction.user):
        await interaction.response.send_message(view=tk_err("You need Manage Server to use this."), ephemeral=True)
        return
    c = wl_cfg()
    c["autorole_id"] = role.id if role else None
    save_config(config)
    await interaction.response.send_message(
        view=tk_ok("Auto Role", f"New members will receive {role.mention}." if role else "Auto role cleared."),
        ephemeral=True
    )


@wl_group.command(name="reset", description="Reset the welcome message to the default design")
async def wl_reset(interaction: discord.Interaction):
    if wl_denied(interaction.user):
        await interaction.response.send_message(view=tk_err("You need Manage Server to use this."), ephemeral=True)
        return
    c = wl_cfg()
    c["data"] = None
    c["color"] = "none"
    save_config(config)
    await interaction.response.send_message(view=tk_ok("Welcome Message Reset", "The default design is back."), ephemeral=True)


@wl_group.command(name="status", description="Show the welcome system settings")
async def wl_status(interaction: discord.Interaction):
    if wl_denied(interaction.user):
        await interaction.response.send_message(view=tk_err("You need Manage Server to use this."), ephemeral=True)
        return
    await interaction.response.send_message(view=TicketInfoView("Welcome System", wl_status_text(interaction.guild)), ephemeral=True)


tree.add_command(wl_group, guild=discord.Object(id=GUILD_ID))


async def wl_prefix(message: discord.Message):
    parts = message.content[len(PREFIX):].split(None, 2)
    sub = parts[1].lower() if len(parts) > 1 else ""
    c = wl_cfg()
    usage = (
        "`.welcome setup #channel [@role]`\n"
        "`.welcome design` `.welcome test` `.welcome toggle`\n"
        "`.welcome autorole @role|off` `.welcome reset` `.welcome status`"
    )
    if sub == "setup":
        if not message.channel_mentions:
            await message.channel.send(view=TicketInfoView("Welcome System", usage))
            return
        c["channel_id"] = message.channel_mentions[0].id
        c["enabled"] = True
        if message.role_mentions:
            c["autorole_id"] = message.role_mentions[0].id
        save_config(config)
        await message.channel.send(view=TicketInfoView("Welcome System Ready", wl_status_text(message.guild) + "\n\nUse `.welcome design` to customize the message."))
    elif sub == "design":
        await message.channel.send(view=wl_builder(message.author.id))
    elif sub == "test":
        channel = message.guild.get_channel(c["channel_id"]) if c.get("channel_id") else message.channel
        try:
            await wl_send(message.author, channel)
        except Exception as e:
            await message.channel.send(view=tk_err(f"Could not send the test message: `{e}`"))
            return
        if channel.id != message.channel.id:
            await message.channel.send(view=tk_ok("Test Sent", f"Sent a test welcome message to {channel.mention}."))
    elif sub == "toggle":
        if not c.get("channel_id"):
            await message.channel.send(view=tk_err("Set a channel first with `.welcome setup #channel`."))
            return
        c["enabled"] = not c["enabled"]
        save_config(config)
        await message.channel.send(view=tk_ok("Welcome System", f"Welcome messages are now **{'enabled' if c['enabled'] else 'disabled'}**."))
    elif sub == "autorole":
        if message.role_mentions:
            c["autorole_id"] = message.role_mentions[0].id
            save_config(config)
            await message.channel.send(view=tk_ok("Auto Role", f"New members will receive {message.role_mentions[0].mention}."))
        else:
            c["autorole_id"] = None
            save_config(config)
            await message.channel.send(view=tk_ok("Auto Role", "Auto role cleared."))
    elif sub == "reset":
        c["data"] = None
        c["color"] = "none"
        save_config(config)
        await message.channel.send(view=tk_ok("Welcome Message Reset", "The default design is back."))
    elif sub == "status":
        await message.channel.send(view=TicketInfoView("Welcome System", wl_status_text(message.guild)))
    else:
        await message.channel.send(view=TicketInfoView("Welcome System", usage))


BOT_START = time.time()
ut_last = {}
UT_UA = {"User-Agent": "Mozilla/5.0 (compatible; DiscordUtilityBot/1.0)"}
UT_NO_MENTIONS = discord.AllowedMentions.none()


def ut_rest(message: discord.Message) -> str:
    parts = message.content[len(PREFIX):].split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def ut_clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def ut_block(text: str, lang: str = "") -> str:
    return f"```{lang}\n{text.replace('```', '`​``')}\n```"


def ut_ts(dt) -> str:
    ts = int(dt.timestamp())
    return f"<t:{ts}:F> (<t:{ts}:R>)"


def ut_view(title: str, body: str, thumb: str = None, image: str = None) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container()
    container.add_item(discord.ui.TextDisplay(f"# {ut_clip(title, 200)}"))
    container.add_item(discord.ui.Separator())
    body = ut_clip(body, 3500)
    if thumb:
        section = discord.ui.Section(accessory=discord.ui.Thumbnail(media=thumb))
        section.add_item(discord.ui.TextDisplay(body))
        container.add_item(section)
    else:
        container.add_item(discord.ui.TextDisplay(body))
    if image:
        gallery = discord.ui.MediaGallery()
        gallery.add_item(media=image)
        container.add_item(gallery)
    view.add_item(container)
    return view


async def ut_send(message: discord.Message, title: str, body: str, thumb: str = None, image: str = None):
    await message.channel.send(view=ut_view(title, body, thumb, image), allowed_mentions=UT_NO_MENTIONS)


async def ut_fail(message: discord.Message, text: str):
    await message.channel.send(view=tk_err(text), allowed_mentions=UT_NO_MENTIONS)


async def ut_usage(message: discord.Message, usage: str):
    await ut_fail(message, f"Usage: {usage}")


async def ut_http(url: str, params: dict = None, as_json: bool = True, headers: dict = None, timeout: int = 8):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout), headers=headers or UT_UA) as session:
            async with session.get(url, params=params) as res:
                if res.status != 200:
                    return None
                if as_json:
                    return await res.json(content_type=None)
                return await res.text()
    except Exception:
        return None


def ut_reply_text(message: discord.Message) -> str:
    ref = message.reference.resolved if message.reference else None
    return getattr(ref, "content", "") or ""


async def ut_member(message: discord.Message, rest: str):
    if message.mentions:
        return message.mentions[0]
    token = rest.split()[0] if rest else ""
    if token.isdigit():
        uid = int(token)
        m = message.guild.get_member(uid)
        if m:
            return m
        try:
            return await message.guild.fetch_member(uid)
        except Exception:
            try:
                return await bot.fetch_user(uid)
            except Exception:
                return None
    if token:
        return message.guild.get_member_named(token)
    return message.author


def ut_human(seconds: float) -> str:
    seconds = int(seconds)
    units = [("y", 31536000), ("d", 86400), ("h", 3600), ("m", 60), ("s", 1)]
    out = []
    for name, size in units:
        if seconds >= size:
            out.append(f"{seconds // size}{name}")
            seconds %= size
    return " ".join(out[:3]) or "0s"


async def c_userinfo(message, rest):
    u = await ut_member(message, rest)
    if u is None:
        return await ut_fail(message, "User not found.")
    lines = [
        f"**Username:** {u}",
        f"**Display name:** {u.display_name}",
        f"**ID:** `{u.id}`",
        f"**Bot:** {'Yes' if u.bot else 'No'}",
        f"**Created:** {ut_ts(u.created_at)}",
    ]
    if isinstance(u, discord.Member):
        if u.joined_at:
            lines.append(f"**Joined:** {ut_ts(u.joined_at)}")
        lines.append(f"**Top role:** {u.top_role.mention}")
        roles = [r.mention for r in reversed(u.roles) if not r.is_default()]
        if roles:
            lines.append(f"**Roles ({len(roles)}):** {ut_clip(' '.join(roles), 600)}")
        if u.premium_since:
            lines.append(f"**Boosting since:** {ut_ts(u.premium_since)}")
        if u.timed_out_until:
            lines.append(f"**Timed out until:** {ut_ts(u.timed_out_until)}")
    badges = [n.replace("_", " ").title() for n, v in u.public_flags if v]
    if badges:
        lines.append(f"**Badges:** {', '.join(badges)}")
    await ut_send(message, "User Info", "\n".join(lines), thumb=u.display_avatar.url)


async def c_serverinfo(message, rest):
    g = message.guild
    bots = sum(1 for m in g.members if m.bot)
    lines = [
        f"**Name:** {g.name}",
        f"**ID:** `{g.id}`",
        f"**Owner:** <@{g.owner_id}>",
        f"**Created:** {ut_ts(g.created_at)}",
        f"**Members:** {g.member_count} ({g.member_count - bots} humans, {bots} bots)",
        f"**Channels:** {len(g.text_channels)} text, {len(g.voice_channels)} voice, {len(g.categories)} categories, {len(g.forums)} forums",
        f"**Roles:** {len(g.roles)}",
        f"**Emojis:** {len(g.emojis)} / {g.emoji_limit}",
        f"**Stickers:** {len(g.stickers)} / {g.sticker_limit}",
        f"**Boosts:** {g.premium_subscription_count} (Level {g.premium_tier})",
        f"**Verification:** {str(g.verification_level).title()}",
    ]
    await ut_send(message, g.name, "\n".join(lines), thumb=g.icon.url if g.icon else None, image=g.banner.url if g.banner else None)


def ut_find_role(guild: discord.Guild, message: discord.Message, text: str):
    if message.role_mentions:
        return message.role_mentions[0]
    t = text.strip()
    if t.isdigit():
        return guild.get_role(int(t))
    low = t.lower()
    for r in guild.roles:
        if r.name.lower() == low:
            return r
    return None


async def c_roleinfo(message, rest):
    if not rest:
        return await ut_usage(message, "`.roleinfo <@role | role id | role name>`")
    r = ut_find_role(message.guild, message, rest)
    if r is None:
        return await ut_fail(message, "Role not found.")
    key = ["administrator", "manage_guild", "manage_roles", "manage_channels", "kick_members", "ban_members", "moderate_members", "manage_messages", "mention_everyone"]
    perms = [p.replace("_", " ").title() for p in key if getattr(r.permissions, p)]
    lines = [
        f"**Name:** {r.name}",
        f"**ID:** `{r.id}`",
        f"**Mention:** {r.mention}",
        f"**Color:** `#{r.color.value:06X}`",
        f"**Position:** {r.position}",
        f"**Members:** {len(r.members)}",
        f"**Hoisted:** {'Yes' if r.hoist else 'No'}",
        f"**Mentionable:** {'Yes' if r.mentionable else 'No'}",
        f"**Managed:** {'Yes' if r.managed else 'No'}",
        f"**Created:** {ut_ts(r.created_at)}",
        f"**Key permissions:** {', '.join(perms) if perms else 'None'}",
    ]
    await ut_send(message, "Role Info", "\n".join(lines), thumb=r.icon.url if r.icon else None)


async def c_banner(message, rest):
    u = await ut_member(message, rest)
    if u is None:
        return await ut_fail(message, "User not found.")
    try:
        full = await bot.fetch_user(u.id)
    except Exception:
        return await ut_fail(message, "Could not fetch that user.")
    if full.banner:
        await ut_send(message, f"{full.display_name}'s Banner", f"**User:** {full.mention}", image=full.banner.url)
    elif full.accent_color:
        await ut_send(message, f"{full.display_name}'s Banner", f"No banner image. Accent color: `#{full.accent_color.value:06X}`")
    else:
        await ut_fail(message, "That user has no banner.")


async def c_botinfo(message, rest):
    lines = [
        f"**Name:** {bot.user}",
        f"**ID:** `{bot.user.id}`",
        f"**Created:** {ut_ts(bot.user.created_at)}",
        f"**Servers:** {len(bot.guilds)}",
        f"**Latency:** {round(bot.latency * 1000)}ms",
        f"**Uptime:** {ut_human(time.time() - BOT_START)}",
        f"**Prefix:** `{PREFIX}`",
        f"**Python:** {platform.python_version()}",
        f"**discord.py:** {discord.__version__}",
    ]
    await ut_send(message, "Bot Info", "\n".join(lines), thumb=bot.user.display_avatar.url)


async def c_stats(message, rest):
    g = message.guild
    bots = sum(1 for m in g.members if m.bot)
    lines = [
        "**Server**",
        f"Members: {g.member_count} ({g.member_count - bots} humans, {bots} bots)",
        f"Channels: {len(g.channels)} | Roles: {len(g.roles)} | Emojis: {len(g.emojis)}",
        f"Boosts: {g.premium_subscription_count} (Level {g.premium_tier})",
        "",
        "**Bot**",
        f"Servers: {len(bot.guilds)} | Users: {sum(x.member_count or 0 for x in bot.guilds)}",
        f"Latency: {round(bot.latency * 1000)}ms | Uptime: {ut_human(time.time() - BOT_START)}",
    ]
    try:
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        mb = usage / (1024 * 1024) if sys.platform == "darwin" else usage / 1024
        lines.append(f"Memory: {mb:.1f} MB")
    except Exception:
        pass
    await ut_send(message, "Stats", "\n".join(lines))


UT_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
}
UT_UNOPS = {ast.USub: operator.neg, ast.UAdd: operator.pos}
UT_FUNCS = {
    "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan, "asin": math.asin,
    "acos": math.acos, "atan": math.atan, "log": math.log, "log10": math.log10, "log2": math.log2,
    "abs": abs, "round": round, "floor": math.floor, "ceil": math.ceil, "factorial": math.factorial,
    "exp": math.exp, "radians": math.radians, "degrees": math.degrees, "min": min, "max": max,
}
UT_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


def ut_eval(node):
    if isinstance(node, ast.Expression):
        return ut_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.Name) and node.id in UT_CONSTS:
        return UT_CONSTS[node.id]
    if isinstance(node, ast.UnaryOp) and type(node.op) in UT_UNOPS:
        return UT_UNOPS[type(node.op)](ut_eval(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in UT_BINOPS:
        left = ut_eval(node.left)
        right = ut_eval(node.right)
        if isinstance(node.op, ast.Pow):
            if abs(right) > 10000 or (left not in (0, 1, -1) and abs(right) * math.log10(abs(left)) > 1000):
                raise ValueError("Result is too large")
        return UT_BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in UT_FUNCS and not node.keywords:
        args = [ut_eval(a) for a in node.args]
        if node.func.id == "factorial" and (not args or args[0] > 500):
            raise ValueError("Factorial is too large")
        return UT_FUNCS[node.func.id](*args)
    raise ValueError("Unsupported expression")


async def c_stealsticker(message, rest):
    if not message.author.guild_permissions.manage_emojis_and_stickers:
        return await ut_fail(message, "You need Manage Emojis and Stickers to use this.")
    src = message if message.stickers else None
    if src is None and message.reference and isinstance(message.reference.resolved, discord.Message):
        src = message.reference.resolved
    if not src or not src.stickers:
        return await ut_usage(message, "`.stealsticker` while replying to a message with a sticker")
    item = src.stickers[0]
    if item.format == discord.StickerFormatType.lottie:
        return await ut_fail(message, "Lottie stickers can't be copied.")
    try:
        data = await item.read()
    except Exception:
        data = await ut_http(item.url, as_json=False)
        if not data:
            return await ut_fail(message, "Could not download that sticker.")
        data = data.encode("latin-1", "ignore")
    try:
        sticker = await message.guild.create_sticker(
            name=ut_clip(item.name, 30) if len(item.name) >= 2 else "sticker",
            description=ut_clip(item.name, 90),
            emoji="🙂",
            file=discord.File(BytesIO(data), filename=f"{item.name}.png"),
            reason=f"Stolen by {message.author}"
        )
    except discord.Forbidden:
        return await ut_fail(message, "I don't have permission to manage stickers.")
    except discord.HTTPException as e:
        return await ut_fail(message, f"Could not add the sticker: `{ut_clip(str(e), 150)}`")
    await ut_send(message, "Sticker Stolen", f"Added sticker **{sticker.name}**.", image=sticker.url)


async def c_emojis(message, rest):
    emojis = message.guild.emojis
    if not emojis:
        return await ut_fail(message, "This server has no custom emojis.")
    out = []
    size = 0
    for e in emojis:
        s = str(e)
        if size + len(s) + 1 > 3200:
            break
        out.append(s)
        size += len(s) + 1
    rest_count = len(emojis) - len(out)
    body = " ".join(out) + (f"\n\n…and {rest_count} more" if rest_count else "")
    await ut_send(message, f"Emojis ({len(emojis)})", body)


UT_ACTIVITIES = {
    "watch": ("Watch Together", 880218394199220334),
    "chess": ("Chess In The Park", 832012774040141894),
    "checkers": ("Checkers In The Park", 832013003968348200),
    "poker": ("Poker Night", 755827207812677713),
    "sketch": ("Sketch Heads", 902271654783242291),
    "letter": ("Letter League", 879863686565621790),
    "snacks": ("Word Snacks", 879863976006127627),
    "blazing": ("Blazing 8s", 832025144389533716),
    "putt": ("Putt Party", 945737671220322374),
    "landio": ("Land-io", 903769130790969345),
    "fishing": ("Fishington", 814288819477020702),
    "spellcast": ("SpellCast", 852509694341283871),
}


async def c_activity(message, rest):
    key = rest.lower().split()[0] if rest else ""
    if key not in UT_ACTIVITIES:
        names = ", ".join(f"`{k}`" for k in UT_ACTIVITIES)
        return await ut_usage(message, f"`.activity <name>` (join a voice channel first)\nActivities: {names}")
    voice = message.author.voice.channel if message.author.voice else None
    if voice is None:
        return await ut_fail(message, "Join a voice channel first.")
    name, app_id = UT_ACTIVITIES[key]
    try:
        invite = await voice.create_invite(
            max_age=3600,
            target_type=discord.InviteTarget.embedded_application,
            target_application_id=app_id
        )
    except discord.Forbidden:
        return await ut_fail(message, "I need the Create Invite permission in that voice channel.")
    except discord.HTTPException as e:
        return await ut_fail(message, f"Could not start the activity: `{ut_clip(str(e), 150)}`")
    await ut_send(message, name, f"Started in {voice.mention}.\n[Click here to join]({invite.url})")


def ut_alpha_style(text, upper, lower, digit=None, exceptions=None):
    out = []
    for ch in text:
        if exceptions and ch in exceptions:
            out.append(exceptions[ch])
        elif "A" <= ch <= "Z":
            out.append(chr(upper + ord(ch) - 65))
        elif "a" <= ch <= "z":
            out.append(chr(lower + ord(ch) - 97))
        elif digit is not None and "0" <= ch <= "9":
            out.append(chr(digit + ord(ch) - 48))
        else:
            out.append(ch)
    return "".join(out)


UT_SMALLCAPS = dict(zip("abcdefghijklmnopqrstuvwxyz", "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀsᴛᴜᴠᴡxʏᴢ"))
UT_DOUBLE_EX = {"C": "ℂ", "H": "ℍ", "N": "ℕ", "P": "ℙ", "Q": "ℚ", "R": "ℝ", "Z": "ℤ"}


def ut_circled(text):
    out = []
    for ch in text:
        if "A" <= ch <= "Z":
            out.append(chr(0x24B6 + ord(ch) - 65))
        elif "a" <= ch <= "z":
            out.append(chr(0x24D0 + ord(ch) - 97))
        elif "1" <= ch <= "9":
            out.append(chr(0x2460 + ord(ch) - 49))
        elif ch == "0":
            out.append("⓪")
        else:
            out.append(ch)
    return "".join(out)


async def c_ascii(message, rest):
    text = rest or ut_reply_text(message)
    if not text:
        return await ut_usage(message, "`.ascii <text>`")
    text = text[:100]
    styles = [
        ("Bold", ut_alpha_style(text, 0x1D400, 0x1D41A, 0x1D7CE)),
        ("Italic", ut_alpha_style(text, 0x1D434, 0x1D44E, None, {"h": "ℎ"})),
        ("Bold Italic", ut_alpha_style(text, 0x1D468, 0x1D482, 0x1D7CE)),
        ("Script", ut_alpha_style(text, 0x1D4D0, 0x1D4EA, 0x1D7CE)),
        ("Fraktur", ut_alpha_style(text, 0x1D56C, 0x1D586, 0x1D7CE)),
        ("Double Struck", ut_alpha_style(text, 0x1D538, 0x1D552, 0x1D7D8, UT_DOUBLE_EX)),
        ("Monospace", ut_alpha_style(text, 0x1D670, 0x1D68A, 0x1D7F6)),
        ("Sans Bold", ut_alpha_style(text, 0x1D5D4, 0x1D5EE, 0x1D7EC)),
        ("Circled", ut_circled(text)),
        ("Small Caps", "".join(UT_SMALLCAPS.get(c, c) for c in text.lower())),
        ("Fullwidth", "".join(chr(ord(c) + 0xFEE0) if 33 <= ord(c) <= 126 else c for c in text)),
    ]
    await ut_send(message, "Fancy Text", "\n".join(f"**{n}:** {s}" for n, s in styles))


UT_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def ut_to_base(n: int, base: int) -> str:
    if n == 0:
        return "0"
    sign = "-" if n < 0 else ""
    n = abs(n)
    out = []
    while n:
        n, r = divmod(n, base)
        out.append(UT_DIGITS[r])
    return sign + "".join(reversed(out))


async def c_base(message, rest):
    parts = rest.split()
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts[1:]):
        return await ut_usage(message, "`.base <number> <from base> [to base]` (bases 2-36)")
    src = int(parts[1])
    if not 2 <= src <= 36:
        return await ut_fail(message, "Bases must be between 2 and 36.")
    try:
        n = int(parts[0], src)
    except ValueError:
        return await ut_fail(message, f"`{parts[0]}` is not a valid base {src} number.")
    if len(parts) == 3:
        dst = int(parts[2])
        if not 2 <= dst <= 36:
            return await ut_fail(message, "Bases must be between 2 and 36.")
        return await ut_send(message, "Base Convert", f"**Input:** `{parts[0]}` (base {src})\n**Output:** `{ut_to_base(n, dst)}` (base {dst})")
    lines = [f"**Input:** `{parts[0]}` (base {src})"]
    for b, name in ((2, "Binary"), (8, "Octal"), (10, "Decimal"), (16, "Hex")):
        lines.append(f"**{name}:** `{ut_to_base(n, b)}`")
    await ut_send(message, "Base Convert", "\n".join(lines))


def ut_codec(kind: str):
    async def handler(message, rest):
        parts = rest.split(None, 1)
        if not parts:
            return await ut_usage(message, f"`.{kind} <encode|decode> <text>`")
        mode = parts[0].lower()
        if mode in ("encode", "decode"):
            text = parts[1] if len(parts) > 1 else ut_reply_text(message)
        else:
            mode, text = "encode", rest
        if not text:
            return await ut_usage(message, f"`.{kind} <encode|decode> <text>`")
        try:
            if kind == "base64":
                out = base64.b64encode(text.encode()).decode() if mode == "encode" else base64.b64decode(text.strip() + "=" * (-len(text.strip()) % 4)).decode("utf-8", "replace")
            elif kind == "base32":
                out = base64.b32encode(text.encode()).decode() if mode == "encode" else base64.b32decode(text.strip().upper() + "=" * (-len(text.strip()) % 8)).decode("utf-8", "replace")
            elif kind == "binary":
                if mode == "encode":
                    out = " ".join(f"{b:08b}" for b in text.encode())
                else:
                    bits = re.sub(r"[^01]", "", text)
                    if not bits or len(bits) % 8:
                        raise ValueError("bad length")
                    out = bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)).decode("utf-8", "replace")
            else:
                return
        except Exception:
            return await ut_fail(message, f"That isn't valid {kind} to decode.")
        await ut_send(message, f"{kind.title()} {mode.title()}", ut_block(ut_clip(out, 1800)))
    return handler


async def c_char(message, rest):
    if not rest:
        return await ut_usage(message, "`.char <characters>` or `.char U+1F600`")
    m = re.fullmatch(r"(?:u\+|\\u|0x)([0-9a-fA-F]{1,6})", rest.strip(), re.I)
    chars = [chr(int(m.group(1), 16))] if m else list(rest.strip())[:10]
    lines = []
    for ch in chars:
        cp = ord(ch)
        name = unicodedata.name(ch, "(unnamed)")
        cat = unicodedata.category(ch)
        utf8 = " ".join(f"{b:02x}" for b in ch.encode("utf-8", "replace"))
        shown = ch if ch.isprintable() and not ch.isspace() else "␣"
        lines.append(f"`{shown}` **U+{cp:04X}** — {name} ({cat})\nUTF-8: `{utf8}` • Decimal: `{cp}`")
    await ut_send(message, "Character Info", "\n\n".join(lines))


async def c_cidr(message, rest):
    if not rest:
        return await ut_usage(message, "`.cidr <ip/prefix>` e.g. `.cidr 192.168.1.0/24`")
    try:
        net = ipaddress.ip_network(rest.split()[0], strict=False)
    except ValueError:
        return await ut_fail(message, "Invalid CIDR block.")
    lines = [
        f"**Network:** `{net.network_address}/{net.prefixlen}`",
        f"**Version:** IPv{net.version}",
        f"**Netmask:** `{net.netmask}`",
        f"**Wildcard:** `{net.hostmask}`",
    ]
    if net.version == 4:
        lines.append(f"**Broadcast:** `{net.broadcast_address}`")
        usable = max(net.num_addresses - 2, 0) if net.prefixlen < 31 else net.num_addresses
        first = net.network_address + 1 if net.prefixlen < 31 else net.network_address
        last = net.broadcast_address - 1 if net.prefixlen < 31 else net.broadcast_address
        lines.append(f"**Usable hosts:** {usable:,}")
        lines.append(f"**Host range:** `{first}` – `{last}`")
    else:
        lines.append(f"**Addresses:** {net.num_addresses:,}")
        lines.append(f"**Range:** `{net.network_address}` – `{net.broadcast_address}`")
    lines.append(f"**Total addresses:** {net.num_addresses:,}")
    lines.append(f"**Private:** {'Yes' if net.is_private else 'No'}")
    await ut_send(message, "CIDR", "\n".join(lines))


def ut_counting() -> dict:
    c = config.setdefault("counting", {})
    c.setdefault("channel_id", None)
    c.setdefault("count", 0)
    c.setdefault("last_user", None)
    c.setdefault("record", 0)
    return c


async def counting_check(message: discord.Message) -> bool:
    c = ut_counting()
    if not c["channel_id"] or message.channel.id != c["channel_id"]:
        return False
    text = message.content.strip()
    if not re.fullmatch(r"\d{1,15}", text):
        return False
    n = int(text)
    expected = c["count"] + 1
    if c["last_user"] == message.author.id and c["count"] > 0:
        reason = "You can't count twice in a row."
    elif n != expected:
        reason = f"Wrong number. The next number was **{expected}**."
    else:
        c["count"] = n
        c["last_user"] = message.author.id
        if n > c["record"]:
            c["record"] = n
        save_config(config)
        try:
            await message.add_reaction("✅")
        except Exception:
            pass
        return True
    try:
        await message.add_reaction("❌")
    except Exception:
        pass
    ruined = c["count"]
    c["count"] = 0
    c["last_user"] = None
    save_config(config)
    await message.channel.send(
        f"{message.author.mention} ruined it at **{ruined}**! {reason}\nThe count is reset. Next number is **1**.",
        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
    )
    return True


async def c_counting(message, rest):
    parts = rest.split()
    sub = parts[0].lower() if parts else "status"
    c = ut_counting()
    usage = "`.counting setup #channel` `.counting off` `.counting reset` `.counting status`"
    if sub in ("setup", "off", "reset") and not message.author.guild_permissions.manage_channels:
        return await ut_fail(message, "You need Manage Channels to use this.")
    if sub == "setup":
        if not message.channel_mentions:
            return await ut_usage(message, usage)
        c["channel_id"] = message.channel_mentions[0].id
        c["count"] = 0
        c["last_user"] = None
        save_config(config)
        await ut_send(message, "Counting Game", f"Counting is set up in {message.channel_mentions[0].mention}. Start with **1**!\n-# Same person can't count twice in a row. A wrong number resets the count.")
    elif sub == "off":
        c["channel_id"] = None
        save_config(config)
        await ut_send(message, "Counting Game", "Counting game turned off.")
    elif sub == "reset":
        c["count"] = 0
        c["last_user"] = None
        save_config(config)
        await ut_send(message, "Counting Game", "The count was reset to 0.")
    elif sub == "status":
        ch = f"<#{c['channel_id']}>" if c["channel_id"] else "Not set"
        await ut_send(message, "Counting Game", f"**Channel:** {ch}\n**Current count:** {c['count']}\n**Record:** {c['record']}\n\n{usage}")
    else:
        await ut_usage(message, usage)


def ut_todos(uid: int) -> list:
    return config.setdefault("todos", {}).setdefault(str(uid), [])


def ut_todo_view(items: list) -> str:
    if not items:
        return "Your todo list is empty. Add one with `.todo add <task>`."
    return "\n".join(f"{i}. {'~~' + t['t'] + '~~' if t['d'] else '' + t['t']}" for i, t in enumerate(items, 1))


async def c_todo(message, rest):
    parts = rest.split(None, 1)
    sub = parts[0].lower() if parts else "list"
    arg = parts[1].strip() if len(parts) > 1 else ""
    items = ut_todos(message.author.id)
    usage = "`.todo add <task>` `.todo list` `.todo done <n>` `.todo undo <n>` `.todo remove <n>` `.todo clear`"
    if sub == "add":
        if not arg:
            return await ut_usage(message, usage)
        if len(items) >= 25:
            return await ut_fail(message, "Your list is full (max 25 tasks).")
        items.append({"t": ut_clip(arg, 200), "d": False})
        save_config(config)
    elif sub in ("done", "undo", "remove", "delete"):
        if not arg.isdigit() or not 1 <= int(arg) <= len(items):
            return await ut_fail(message, "Give a valid task number from your list.")
        i = int(arg) - 1
        if sub == "done":
            items[i]["d"] = True
        elif sub == "undo":
            items[i]["d"] = False
        else:
            items.pop(i)
        save_config(config)
    elif sub == "clear":
        items.clear()
        save_config(config)
    elif sub != "list":
        if rest:
            if len(items) >= 25:
                return await ut_fail(message, "Your list is full (max 25 tasks).")
            items.append({"t": ut_clip(rest, 200), "d": False})
            save_config(config)
        else:
            return await ut_usage(message, usage)
    await ut_send(message, f"{message.author.display_name}'s Todo", ut_todo_view(items))


UTIL_CMDS = {
    "userinfo": c_userinfo, "serverinfo": c_serverinfo, "roleinfo": c_roleinfo, "banner": c_banner,
    "botinfo": c_botinfo, "stats": c_stats, "stealsticker": c_stealsticker, "emojis": c_emojis,
    "activity": c_activity, "ascii": c_ascii, "base": c_base, "base32": ut_codec("base32"),
    "base64": ut_codec("base64"), "binary": ut_codec("binary"), "char": c_char, "cidr": c_cidr,
    "counting": c_counting, "todo": c_todo,
}


async def util_prefix(message: discord.Message, cmd: str, args: list) -> bool:
    fn = UTIL_CMDS.get(cmd)
    if fn is None:
        return False
    now = time.time()
    if now - ut_last.get(message.author.id, 0) < 2:
        return True
    ut_last[message.author.id] = now
    try:
        await fn(message, ut_rest(message))
    except Exception as e:
        print(f"Utility command {cmd} failed: {e}")
        try:
            await message.channel.send(view=tk_err(f"Something went wrong running `{cmd}`."))
        except Exception:
            pass
    return True


ACTION_STYLE.update({
    "DM": ("", discord.Color.blurple()),
    "Softban": ("", discord.Color.orange()),
    "Jail": ("", discord.Color.red()),
    "Unjail": ("", discord.Color.green()),
    "Honeypot": ("", discord.Color.red()),
    "Mass Purge": ("", discord.Color.blurple()),
    "Mass Role": ("", discord.Color.blurple()),
    "Role All": ("", discord.Color.blurple()),
    "Force Nick": ("", discord.Color.blurple()),
    "Restrict": ("", discord.Color.orange()),
    "Unrestrict": ("", discord.Color.green()),
    "Mod Note": ("", discord.Color.blurple()),
    "Move All": ("", discord.Color.blurple()),
    "Pic Perms": ("", discord.Color.orange()),
    "ModMail": ("", discord.Color.blurple()),
    "Staff Roles": ("", discord.Color.blurple()),
    "Extra Owner": ("", discord.Color.gold()),
})

MX_NO_MENTIONS = discord.AllowedMentions.none()
MX_IMAGE_RE = re.compile(r"https?://\S*(\.(png|jpe?g|gif|webp)|tenor\.com|giphy\.com|imgur\.com|cdn\.discordapp|media\.discordapp)\S*", re.I)
MX_PUNISHMENTS = ["timeout", "quarantine", "kick", "ban", "softban"]
MX_HP_LABELS = {"timeout": "Timeouts", "quarantine": "Quarantines", "kick": "Kicks", "ban": "Bans", "softban": "Softbans"}
MX_HP_ICON = "https://cdn.jsdelivr.net/gh/jdecked/twemoji@latest/assets/72x72/1f36f.png"
MX_HP_PENDING = {"task": None}


def mx_cfg() -> dict:
    store = config.setdefault("mx", {})
    defaults = {
        "forcenick": {},
        "honeypot": {},
        "ignored": [],
        "jail": {},
        "jail_cfg": {},
        "modmail": {},
        "modnotes": {},
        "modlog": [],
        "reactionroles": {},
        "restricted": [],
        "staffroles": [],
        "sticky": {},
        "sticky_on": False,
    }
    for key, value in defaults.items():
        store.setdefault(key, value)
    return store


def mx_save():
    save_config(config)


class MxCtx:
    def __init__(self, source):
        self.source = source
        self.is_slash = isinstance(source, discord.Interaction)
        self.guild = source.guild
        self.user = source.user if self.is_slash else source.author
        self.channel = source.channel

    async def defer(self):
        if self.is_slash and not self.source.response.is_done():
            await self.source.response.defer()

    async def working(self, text: str):
        if self.is_slash:
            await self.defer()
        else:
            await self.channel.send(view=InfoView("Working...", text, discord.Color.blurple()), allowed_mentions=MX_NO_MENTIONS)

    async def send(self, view, ephemeral: bool = False):
        if self.is_slash:
            if self.source.response.is_done():
                return await self.source.followup.send(view=view, ephemeral=ephemeral, allowed_mentions=MX_NO_MENTIONS)
            await self.source.response.send_message(view=view, ephemeral=ephemeral, allowed_mentions=MX_NO_MENTIONS)
            return None
        return await self.channel.send(view=view, allowed_mentions=MX_NO_MENTIONS)


async def mx_guard(ctx, coro):
    try:
        await coro
    except discord.Forbidden:
        await ctx.send(err("I don't have permission to do that. Check my role position and permissions."), ephemeral=True)
    except discord.HTTPException as e:
        await ctx.send(err(clip(str(e), 300)), ephemeral=True)
    except Exception as e:
        print(f"MX command failed: {e}")
        await ctx.send(err("Something went wrong running that command."), ephemeral=True)


def mx_can(member, *perms) -> bool:
    gp = member.guild_permissions
    if gp.administrator or is_trusted(member.guild, member.id):
        return True
    return any(getattr(gp, p) for p in perms)


def mx_is_staff_member(member) -> bool:
    staff = set(mx_cfg()["staffroles"])
    return any(r.id in staff for r in getattr(member, "roles", []))


def mx_exempt(member) -> bool:
    gp = member.guild_permissions
    if gp.administrator or gp.manage_messages:
        return True
    return is_trusted(member.guild, member.id) or mx_is_staff_member(member)


def mx_super(member) -> bool:
    return member.guild_permissions.administrator or is_trusted(member.guild, member.id)


async def mx_deny(ctx):
    await ctx.send(err("You don't have permission to use this command."), ephemeral=True)


def mx_hier_error(guild, actor, target):
    if target.id == guild.owner_id:
        return "You can't do that to the server owner."
    if bot.user is not None and target.id == bot.user.id:
        return "I can't do that to myself."
    if isinstance(target, discord.Member):
        if actor.id != guild.owner_id and target.top_role >= actor.top_role:
            return "That member's top role is equal to or higher than yours."
        if target.top_role >= guild.me.top_role:
            return "That member's top role is equal to or higher than mine."
    return None


def mx_role_error(guild, actor, role):
    if role.is_default() or role.managed:
        return "That role can't be managed."
    if role >= guild.me.top_role:
        return "That role is higher than or equal to my top role."
    if actor.id != guild.owner_id and role >= actor.top_role:
        return "That role is higher than or equal to your top role."
    return None


def mx_color(text):
    t = (text or "").strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{6}", t):
        return discord.Colour(int(t, 16))
    return None


def mx_card(title: str, body: str, color: discord.Color = discord.Color.blurple()) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(
        discord.ui.Container(
            discord.ui.TextDisplay(f"**{clip(title, 200)}**\n{clip(body, 3500)}"),
        )
    )
    return view


def mx_tail(message, n: int) -> str:
    parts = message.content[len(PREFIX):].split(None, n)
    return parts[n].strip() if len(parts) > n else ""


def mx_plain_ids(args) -> list:
    return [int(a) for a in args if a.isdigit() and 15 <= len(a) <= 20]


def mx_members(message, args) -> list:
    found = list(message.mentions)
    for i in mx_plain_ids(args):
        m = message.guild.get_member(i)
        if m is not None and m not in found:
            found.append(m)
    return found


def mx_roles(message, args) -> list:
    found = list(message.role_mentions)
    for i in mx_plain_ids(args):
        r = message.guild.get_role(i)
        if r is not None and r not in found:
            found.append(r)
    return found


def mx_channels(message, args) -> list:
    found = list(message.channel_mentions)
    for i in mx_plain_ids(args):
        c = message.guild.get_channel(i)
        if c is not None and c not in found:
            found.append(c)
    return found


async def mx_user_from(message, args):
    if message.mentions:
        return message.mentions[0]
    for i in mx_plain_ids(args):
        m = message.guild.get_member(i)
        if m is not None:
            return m
        try:
            return await bot.fetch_user(i)
        except Exception:
            continue
    return None


def mx_ids_in(text: str) -> list:
    return [int(x) for x in re.findall(r"\d{15,20}", text or "")]


def mx_record(action: str, moderator):
    log = mx_cfg()["modlog"]
    log.append({"t": int(time.time()), "a": action, "m": getattr(moderator, "id", 0)})
    if len(log) > 5000:
        del log[:len(log) - 5000]
    mx_save()


def mx_choices(*names) -> list:
    return [discord.app_commands.Choice(name=n, value=n) for n in names]


async def mx_punish(guild, member, kind: str, reason: str, minutes: int = 60, role_id: int = 0):
    try:
        if kind == "ban":
            mark_action("ban", member.id)
            await guild.ban(member, reason=reason, delete_message_days=1)
        elif kind == "softban":
            mark_action("ban", member.id)
            await guild.ban(member, reason=reason, delete_message_days=7)
            mark_action("unban", member.id)
            await guild.unban(member, reason="Softban")
        elif kind == "kick":
            mark_action("kick", member.id)
            await member.kick(reason=reason)
        elif kind == "timeout":
            mark_action("timeout", member.id)
            await member.timeout(discord.utils.utcnow() + timedelta(minutes=minutes), reason=reason)
        elif kind == "quarantine":
            role = guild.get_role(role_id)
            if role is None:
                return "Quarantine role not found."
            keep = [r for r in member.roles if not r.is_default() and (r.managed or r >= guild.me.top_role)]
            await member.edit(roles=keep + [role], reason=reason)
        elif kind == "jail":
            failure = await mx_jail_member(guild, member, reason, bot.user)
            if failure:
                return failure
        return None
    except Exception as e:
        return str(e)


async def mx_dm(ctx, member, text: str):
    if not mx_can(ctx.user, "manage_messages"):
        return await mx_deny(ctx)
    text = (text or "").strip()
    if member.bot:
        return await ctx.send(err("You can't DM a bot."), ephemeral=True)
    if not text:
        return await ctx.send(err("Message can't be empty."), ephemeral=True)
    try:
        await member.send(clip(f"**Message from {ctx.guild.name} staff:**\n{text}", 2000))
    except discord.Forbidden:
        return await ctx.send(err(f"{member.mention} has their DMs closed."), ephemeral=True)
    await ctx.send(ok("DM Sent", f"**To:** {member.mention}\n**Message:** {clip(text, 500)}"))
    await log_mod(ctx.guild, "DM", ctx.user, member, clip(text, 300))


async def mx_roleall(ctx, action: str, role, target: str = "humans"):
    if not mx_can(ctx.user, "manage_roles"):
        return await mx_deny(ctx)
    problem = mx_role_error(ctx.guild, ctx.user, role)
    if problem:
        return await ctx.send(err(problem), ephemeral=True)
    adding = action == "add"
    members = ctx.guild.members
    if target == "humans":
        members = [m for m in members if not m.bot]
    elif target == "bots":
        members = [m for m in members if m.bot]
    todo = [m for m in members if (role not in m.roles) == adding]
    if not todo:
        return await ctx.send(err(f"Nobody needs {role.mention} {'added' if adding else 'removed'}."), ephemeral=True)
    await ctx.working(f"{'Adding' if adding else 'Removing'} {role.mention} for {len(todo)} member(s)...")
    done = failed = 0
    for m in todo:
        try:
            if adding:
                await m.add_roles(role, reason=f"Role all by {ctx.user}")
            else:
                await m.remove_roles(role, reason=f"Role all by {ctx.user}")
            done += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.4)
    await ctx.send(ok("Role All Complete", f"**Role:** {role.mention}\n**Action:** {action}\n**Target:** {target}\n**Success:** {done}\n**Failed:** {failed}"))
    await log_mod(ctx.guild, "Role All", ctx.user, None, None, f"**Role:** {role.mention}\n**Action:** {action} ({done} ok, {failed} failed)")


async def mx_massrole(ctx, action: str, role, members: list):
    if not mx_can(ctx.user, "manage_roles"):
        return await mx_deny(ctx)
    problem = mx_role_error(ctx.guild, ctx.user, role)
    if problem:
        return await ctx.send(err(problem), ephemeral=True)
    if not members:
        return await ctx.send(err("No valid members provided."), ephemeral=True)
    adding = action == "add"
    await ctx.working(f"{'Adding' if adding else 'Removing'} {role.mention} for {len(members)} member(s)...")
    done = failed = 0
    for m in members[:200]:
        try:
            if adding:
                await m.add_roles(role, reason=f"Mass role by {ctx.user}")
            else:
                await m.remove_roles(role, reason=f"Mass role by {ctx.user}")
            done += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.4)
    await ctx.send(ok("Mass Role Complete", f"**Role:** {role.mention}\n**Action:** {action}\n**Success:** {done}\n**Failed:** {failed}"))
    await log_mod(ctx.guild, "Mass Role", ctx.user, None, None, f"**Role:** {role.mention}\n**Action:** {action} ({done} ok, {failed} failed)")


async def mx_giverole(ctx, member, role):
    if not mx_can(ctx.user, "manage_roles"):
        return await mx_deny(ctx)
    problem = mx_role_error(ctx.guild, ctx.user, role)
    if problem:
        return await ctx.send(err(problem), ephemeral=True)
    if role in member.roles:
        return await ctx.send(err(f"{member.mention} already has {role.mention}."), ephemeral=True)
    await member.add_roles(role, reason=f"Given by {ctx.user}")
    await ctx.send(ok("Role Given", f"**Member:** {member.mention}\n**Role:** {role.mention}"))


async def mx_role(ctx, action: str, role=None, name: str = None, color: str = None, member=None):
    if not mx_can(ctx.user, "manage_roles"):
        return await mx_deny(ctx)
    guild = ctx.guild
    if action == "create":
        if not name:
            return await ctx.send(err("Provide a name for the new role."), ephemeral=True)
        colour = mx_color(color) if color else discord.Colour.default()
        if colour is None:
            return await ctx.send(err("Invalid color. Use a hex like `#ff0000`."), ephemeral=True)
        created = await guild.create_role(name=name[:100], colour=colour, reason=f"Created by {ctx.user}")
        return await ctx.send(ok("Role Created", f"**Role:** {created.mention}\n**ID:** `{created.id}`"))
    if role is None:
        return await ctx.send(err("Provide a role."), ephemeral=True)
    problem = mx_role_error(guild, ctx.user, role)
    if problem:
        return await ctx.send(err(problem), ephemeral=True)
    if action == "delete":
        label = role.name
        await role.delete(reason=f"Deleted by {ctx.user}")
        return await ctx.send(ok("Role Deleted", f"**Role:** {label}"))
    if action == "rename":
        if not name:
            return await ctx.send(err("Provide the new name."), ephemeral=True)
        old = role.name
        await role.edit(name=name[:100], reason=f"Renamed by {ctx.user}")
        return await ctx.send(ok("Role Renamed", f"**Before:** {old}\n**After:** {role.mention}"))
    if action == "color":
        colour = mx_color(color)
        if colour is None:
            return await ctx.send(err("Invalid color. Use a hex like `#ff0000`."), ephemeral=True)
        await role.edit(colour=colour, reason=f"Recolored by {ctx.user}")
        return await ctx.send(ok("Role Color Changed", f"**Role:** {role.mention}\n**Color:** `#{str(color).lstrip('#').lower()}`"))
    if action == "hoist":
        state = not role.hoist
        await role.edit(hoist=state, reason=f"Hoist toggled by {ctx.user}")
        return await ctx.send(ok("Role Hoist Toggled", f"**Role:** {role.mention}\n**Hoisted:** {state}"))
    if action == "mentionable":
        state = not role.mentionable
        await role.edit(mentionable=state, reason=f"Mentionable toggled by {ctx.user}")
        return await ctx.send(ok("Role Mentionable Toggled", f"**Role:** {role.mention}\n**Mentionable:** {state}"))
    if action in ("add", "remove"):
        if member is None:
            return await ctx.send(err("Provide a member."), ephemeral=True)
        if action == "add":
            await member.add_roles(role, reason=f"Added by {ctx.user}")
        else:
            await member.remove_roles(role, reason=f"Removed by {ctx.user}")
        return await ctx.send(ok(f"Role {'Added' if action == 'add' else 'Removed'}", f"**Member:** {member.mention}\n**Role:** {role.mention}"))
    await ctx.send(err("Unknown role action."), ephemeral=True)


async def mx_forcenick(ctx, member, nick):
    if not mx_can(ctx.user, "manage_nicknames"):
        return await mx_deny(ctx)
    problem = mx_hier_error(ctx.guild, ctx.user, member)
    if problem:
        return await ctx.send(err(problem), ephemeral=True)
    store = mx_cfg()["forcenick"]
    if not nick or nick.lower() in ("off", "reset", "none"):
        if str(member.id) not in store:
            return await ctx.send(err(f"{member.mention} has no forced nickname."), ephemeral=True)
        store.pop(str(member.id), None)
        mx_save()
        return await ctx.send(ok("Forced Nickname Removed", f"**Member:** {member.mention}"))
    if len(nick) > 32:
        return await ctx.send(err("Nicknames can be at most 32 characters."), ephemeral=True)
    store[str(member.id)] = nick
    mx_save()
    await member.edit(nick=nick, reason=f"Forced nickname by {ctx.user}")
    await ctx.send(ok("Nickname Forced", f"**Member:** {member.mention}\n**Nickname:** {nick}"))
    await log_mod(ctx.guild, "Force Nick", ctx.user, member, None, f"**Nickname:** {nick}")


def mx_hp_notice(hp: dict) -> str:
    kind = hp.get("punishment", "ban")
    if kind == "timeout":
        return f"a **{hp.get('minutes', 60)} minute timeout**"
    return f"a **{kind}**"


def mx_hp_view(hp: dict) -> discord.ui.LayoutView:
    kind = hp.get("punishment", "ban")
    section = discord.ui.Section(accessory=discord.ui.Thumbnail(media=MX_HP_ICON))
    section.add_item(discord.ui.TextDisplay("## DO NOT SEND MESSAGES IN THIS CHANNEL"))
    section.add_item(discord.ui.TextDisplay(f"This channel is used to catch spam bots. Any messages sent here will result in {mx_hp_notice(hp)}."))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(
        discord.ui.Container(
            section,
            discord.ui.ActionRow(
                discord.ui.Button(label=f"{MX_HP_LABELS.get(kind, 'Hits')}: {hp.get('count', 0)}", style=discord.ButtonStyle.secondary, disabled=True)
            )
        )
    )
    return view


async def mx_hp_refresh():
    await asyncio.sleep(3)
    MX_HP_PENDING["task"] = None
    hp = mx_cfg()["honeypot"]
    guild = bot.get_guild(GUILD_ID)
    if not hp or guild is None or not hp.get("message"):
        return
    channel = guild.get_channel(hp.get("channel", 0))
    if channel is None:
        return
    try:
        await channel.get_partial_message(hp["message"]).edit(view=mx_hp_view(hp))
    except Exception:
        pass


def mx_hp_schedule():
    if MX_HP_PENDING["task"] is None:
        MX_HP_PENDING["task"] = asyncio.create_task(mx_hp_refresh())


async def mx_honeypot(ctx, action: str, channel=None, punishment: str = None, minutes: int = None, role=None):
    if not mx_can(ctx.user, "manage_guild"):
        return await mx_deny(ctx)
    cfg = mx_cfg()
    guild = ctx.guild
    if action == "info":
        body = (
            f"`{PREFIX}honeypot setup #channel timeout [minutes]`: timeout whoever writes\n"
            f"`{PREFIX}honeypot setup #channel quarantine @role`: strip all roles, add the quarantine role\n"
            f"`{PREFIX}honeypot setup #channel kick` / `ban` / `softban`\n"
            f"`{PREFIX}honeypot off`: disable · `{PREFIX}honeypot stats`: statistics"
        )
        return await ctx.send(mx_card("Honeypot", body))
    old = cfg["honeypot"]
    if action == "off":
        if not old:
            return await ctx.send(err("No honeypot is set."), ephemeral=True)
        old_channel = guild.get_channel(old.get("channel", 0))
        if old_channel is not None and old.get("message"):
            try:
                await old_channel.get_partial_message(old["message"]).delete()
            except Exception:
                pass
        cfg["honeypot"] = {}
        mx_save()
        return await ctx.send(ok("Honeypot Disabled", "The honeypot channel has been removed."))
    if action == "stats":
        if not old:
            return await ctx.send(ok("Honeypot Statistics", "No honeypot is set."))
        kind = old.get("punishment", "ban")
        lines = [f"**Channel:** <#{old['channel']}>", f"**Punishment:** {kind}"]
        if kind == "timeout":
            lines.append(f"**Duration:** {old.get('minutes', 60)} minute(s)")
        if kind == "quarantine":
            lines.append(f"**Quarantine role:** <@&{old.get('role', 0)}>")
        lines.append(f"**{MX_HP_LABELS.get(kind, 'Hits')}:** {old.get('count', 0)}")
        return await ctx.send(ok("Honeypot Statistics", "\n".join(lines)))
    if channel is None or punishment not in MX_PUNISHMENTS:
        return await mx_honeypot(ctx, "info")
    minutes = clamp(minutes or 60, 1, 40320)
    role_id = 0
    if punishment == "quarantine":
        if role is None:
            return await ctx.send(err("Provide the quarantine role."), ephemeral=True)
        problem = mx_role_error(guild, ctx.user, role)
        if problem:
            return await ctx.send(err(problem), ephemeral=True)
        role_id = role.id
    count = old.get("count", 0) if old.get("channel") == channel.id else 0
    if old.get("message"):
        old_channel = guild.get_channel(old.get("channel", 0))
        if old_channel is not None:
            try:
                await old_channel.get_partial_message(old["message"]).delete()
            except Exception:
                pass
    hp = {"channel": channel.id, "punishment": punishment, "minutes": minutes, "role": role_id, "count": count, "message": 0}
    try:
        notice = await channel.send(view=mx_hp_view(hp))
        hp["message"] = notice.id
    except Exception:
        return await ctx.send(err("I can't send messages in that channel."), ephemeral=True)
    cfg["honeypot"] = hp
    mx_save()
    detail = f"**Channel:** {channel.mention}\n**Punishment:** {punishment}"
    if punishment == "timeout":
        detail += f"\n**Duration:** {minutes} minute(s)"
    if punishment == "quarantine":
        detail += f"\n**Quarantine role:** {role.mention}"
    await ctx.send(ok("Honeypot Set", detail + "\nStaff are exempt."))


async def mx_ignored(ctx, action: str, channel=None):
    if not mx_can(ctx.user, "manage_guild"):
        return await mx_deny(ctx)
    ids = mx_cfg()["ignored"]
    if action == "list":
        text = "\n".join(f"<#{i}>" for i in ids) or "No ignored channels."
        return await ctx.send(ok("Ignored Channels", text))
    if channel is None:
        return await ctx.send(err("Provide a channel."), ephemeral=True)
    if action == "add":
        if channel.id in ids:
            return await ctx.send(err("That channel is already ignored."), ephemeral=True)
        ids.append(channel.id)
        mx_save()
        return await ctx.send(ok("Channel Ignored", f"Commands are now ignored in {channel.mention} (admins excluded)."))
    if channel.id not in ids:
        return await ctx.send(err("That channel isn't ignored."), ephemeral=True)
    ids.remove(channel.id)
    mx_save()
    await ctx.send(ok("Channel Restored", f"Commands work again in {channel.mention}."))


async def mx_jail_setup(ctx):
    if not mx_can(ctx.user, "administrator"):
        return await mx_deny(ctx)
    guild = ctx.guild
    await ctx.working("Setting up the jail role and channel...")
    cfg = mx_cfg()["jail_cfg"]
    role = guild.get_role(cfg.get("role", 0))
    if role is None:
        role = await guild.create_role(name="Jailed", permissions=discord.Permissions.none(), colour=discord.Colour.dark_grey(), reason="Jail setup")
    channel = guild.get_channel(cfg.get("channel", 0))
    if channel is None:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        channel = await guild.create_text_channel("jail", overwrites=overwrites, reason="Jail setup")
    for ch in guild.channels:
        if ch.id == channel.id:
            continue
        try:
            await ch.set_permissions(role, view_channel=False, send_messages=False, connect=False, speak=False, reason="Jail setup")
        except Exception:
            pass
        await asyncio.sleep(0.3)
    cfg["role"] = role.id
    cfg["channel"] = channel.id
    mx_save()
    await ctx.send(ok("Jail Ready", f"**Role:** {role.mention}\n**Channel:** {channel.mention}\nJailed members only see the jail channel."))


async def mx_jail_member(guild, member, reason: str, actor):
    cfg = mx_cfg()
    role = guild.get_role(cfg["jail_cfg"].get("role", 0))
    if role is None:
        return "Jail isn't set up yet. Run the jail setup first."
    if str(member.id) in cfg["jail"]:
        return "That member is already jailed."
    removable = [r for r in member.roles if not r.is_default() and not r.managed and r < guild.me.top_role]
    keep = [r for r in member.roles if not r.is_default() and r not in removable]
    await member.edit(roles=keep + [role], reason=f"Jailed: {reason}")
    cfg["jail"][str(member.id)] = {"roles": [r.id for r in removable], "reason": reason, "by": getattr(actor, "id", 0), "t": int(time.time())}
    mx_save()
    return None


async def mx_jail(ctx, member, reason: str = "No reason provided"):
    if not mx_can(ctx.user, "moderate_members", "manage_roles"):
        return await mx_deny(ctx)
    problem = mx_hier_error(ctx.guild, ctx.user, member)
    if problem:
        return await ctx.send(err(problem), ephemeral=True)
    failure = await mx_jail_member(ctx.guild, member, reason, ctx.user)
    if failure:
        return await ctx.send(err(failure), ephemeral=True)
    await ctx.send(ok("Member Jailed", f"**Member:** {member.mention}\n**Reason:** {reason}"))
    await log_mod(ctx.guild, "Jail", ctx.user, member, reason)


async def mx_unjail(ctx, member):
    if not mx_can(ctx.user, "moderate_members", "manage_roles"):
        return await mx_deny(ctx)
    cfg = mx_cfg()
    entry = cfg["jail"].get(str(member.id))
    if entry is None:
        return await ctx.send(err(f"{member.mention} isn't jailed."), ephemeral=True)
    guild = ctx.guild
    jail_role = guild.get_role(cfg["jail_cfg"].get("role", 0))
    restored = [guild.get_role(i) for i in entry["roles"]]
    restored = [r for r in restored if r is not None and r < guild.me.top_role]
    remaining = [r for r in member.roles if not r.is_default() and r != jail_role]
    await member.edit(roles=remaining + [r for r in restored if r not in remaining], reason=f"Unjailed by {ctx.user}")
    cfg["jail"].pop(str(member.id), None)
    mx_save()
    await ctx.send(ok("Member Released", f"**Member:** {member.mention}\n**Roles restored:** {len(restored)}"))
    await log_mod(ctx.guild, "Unjail", ctx.user, member)


async def mx_masspurge(ctx, user, limit: int = 100):
    if not mx_can(ctx.user, "manage_messages"):
        return await mx_deny(ctx)
    limit = clamp(limit, 1, 1000)
    await ctx.working(f"Deleting messages from {fmt_ref(user)} across all channels...")
    total = 0
    channels = 0
    me = ctx.guild.me
    for ch in ctx.guild.text_channels:
        perms = ch.permissions_for(me)
        if not (perms.view_channel and perms.read_message_history and perms.manage_messages):
            continue
        try:
            deleted = await ch.purge(limit=limit, check=lambda m: m.author.id == user.id, bulk=True)
        except Exception:
            continue
        if deleted:
            total += len(deleted)
            channels += 1
        await asyncio.sleep(0.5)
    await ctx.send(ok("Mass Purge Complete", f"**User:** {fmt_ref(user)}\n**Deleted:** {total} message(s)\n**Channels:** {channels}\n**Scanned per channel:** {limit}"))
    await log_mod(ctx.guild, "Mass Purge", ctx.user, user, None, f"**Deleted:** {total} message(s) in {channels} channel(s)")


def mx_staff_role_ids(guild) -> list:
    ids = set(mx_cfg()["staffroles"])
    for r in guild.roles:
        if r.is_default() or r.managed:
            continue
        if r.permissions.administrator or r.permissions.manage_messages:
            ids.add(r.id)
    return list(ids)


def mx_thread_user(channel_id: int):
    for uid, cid in mx_cfg()["modmail"].get("threads", {}).items():
        if cid == channel_id:
            return int(uid)
    return None


async def mx_modmail(ctx, action: str, category=None, text: str = None, member=None):
    cfg = mx_cfg()["modmail"]
    guild = ctx.guild
    if action in ("setup", "off", "block", "unblock", "status"):
        if not mx_can(ctx.user, "manage_guild"):
            return await mx_deny(ctx)
    elif not mx_can(ctx.user, "manage_messages"):
        return await mx_deny(ctx)
    if action == "setup":
        if category is None:
            return await ctx.send(err("Provide a category for ModMail threads."), ephemeral=True)
        cfg["enabled"] = True
        cfg["category_id"] = category.id
        cfg.setdefault("threads", {})
        cfg.setdefault("blocked", [])
        mx_save()
        return await ctx.send(ok("ModMail Enabled", f"Threads will be created in **{category.name}**.\nMembers DM me to open a thread."))
    if action == "off":
        cfg["enabled"] = False
        mx_save()
        return await ctx.send(ok("ModMail Disabled", "DMs will no longer be relayed to staff."))
    if action == "status":
        cat = guild.get_channel(cfg.get("category_id", 0))
        body = (
            f"**Enabled:** {bool(cfg.get('enabled'))}\n"
            f"**Category:** {cat.name if cat else 'Not set'}\n"
            f"**Open threads:** {len(cfg.get('threads', {}))}\n"
            f"**Blocked users:** {len(cfg.get('blocked', []))}"
        )
        return await ctx.send(ok("ModMail", body))
    if action in ("block", "unblock"):
        if member is None:
            return await ctx.send(err("Provide a member."), ephemeral=True)
        blocked = cfg.setdefault("blocked", [])
        if action == "block":
            if member.id not in blocked:
                blocked.append(member.id)
        elif member.id in blocked:
            blocked.remove(member.id)
        mx_save()
        return await ctx.send(ok(f"ModMail {'Blocked' if action == 'block' else 'Unblocked'}", f"**Member:** {member.mention}"))
    uid = mx_thread_user(ctx.channel.id)
    if uid is None:
        return await ctx.send(err("Use this inside a ModMail thread channel."), ephemeral=True)
    try:
        user = await bot.fetch_user(uid)
    except Exception:
        user = None
    if action == "reply":
        text = (text or "").strip()
        if not text:
            return await ctx.send(err("Provide a reply message."), ephemeral=True)
        if user is None:
            return await ctx.send(err("I can't find that user anymore."), ephemeral=True)
        try:
            await user.send(clip(f"**{ctx.user.display_name} (Staff):** {text}", 2000))
        except discord.Forbidden:
            return await ctx.send(err("That user has their DMs closed."), ephemeral=True)
        return await ctx.send(mx_card(f"Reply from {ctx.user.display_name}", text, discord.Color.green()))
    if action == "close":
        reason = (text or "").strip() or "No reason provided"
        if user is not None:
            try:
                await user.send(f"Your ModMail thread in **{guild.name}** was closed.\n**Reason:** {clip(reason, 500)}")
            except Exception:
                pass
        cfg.get("threads", {}).pop(str(uid), None)
        mx_save()
        await ctx.send(ok("Thread Closed", f"Closed by {ctx.user.mention}. This channel will be deleted shortly."))
        await log_mod(guild, "ModMail", ctx.user, user, reason, "**Thread closed**")
        await asyncio.sleep(4)
        try:
            await ctx.channel.delete(reason=f"ModMail closed by {ctx.user}")
        except Exception:
            pass
        return
    await ctx.send(err("Unknown ModMail action."), ephemeral=True)


async def mx_modmail_dm(message) -> bool:
    cfg = mx_cfg()["modmail"]
    if not cfg.get("enabled"):
        return False
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return False
    member = guild.get_member(message.author.id)
    if member is None:
        return False
    if member.id in cfg.get("blocked", []):
        try:
            await message.channel.send("You are blocked from using ModMail.")
        except Exception:
            pass
        return True
    category = guild.get_channel(cfg.get("category_id", 0))
    threads = cfg.setdefault("threads", {})
    channel = guild.get_channel(threads.get(str(member.id), 0))
    created = False
    if channel is None:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True),
        }
        for rid in mx_staff_role_ids(guild):
            role = guild.get_role(rid)
            if role is not None:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        slug = re.sub(r"[^a-z0-9-]", "", member.name.lower())[:20] or "user"
        try:
            channel = await guild.create_text_channel(
                f"modmail-{slug}",
                category=category if isinstance(category, discord.CategoryChannel) else None,
                overwrites=overwrites,
                topic=f"ModMail thread with {member} ({member.id})",
                reason="ModMail thread opened"
            )
        except Exception as e:
            print(f"ModMail channel creation failed: {e}")
            return True
        threads[str(member.id)] = channel.id
        mx_save()
        created = True
        info = (
            f"**User:** {fmt_ref(member)}\n"
            f"**Account created:** <t:{int(member.created_at.timestamp())}:R>\n"
            f"**Joined:** {('<t:' + str(int(member.joined_at.timestamp())) + ':R>') if member.joined_at else 'Unknown'}\n\n"
            f"Reply with `{PREFIX}modmail reply <text>` or `/modmail`. Close with `{PREFIX}modmail close [reason]`."
        )
        await channel.send(view=mx_card("New ModMail Thread", info, discord.Color.green()), allowed_mentions=MX_NO_MENTIONS)
    body = message.content or ""
    if message.attachments:
        body += "\n" + "\n".join(a.url for a in message.attachments)
    body = body.strip() or "*No text content*"
    await channel.send(view=mx_card(f"{member.display_name}", body, discord.Color.blurple()), allowed_mentions=MX_NO_MENTIONS)
    try:
        await message.add_reaction("✅")
        if created:
            await message.channel.send("Your message was sent to the staff team. They'll reply here soon.")
    except Exception:
        pass
    return True


async def mx_modnote(ctx, action: str, user, text: str = None, number: int = None):
    if not mx_can(ctx.user, "manage_messages", "moderate_members"):
        return await mx_deny(ctx)
    notes = mx_cfg()["modnotes"]
    key = str(user.id)
    if action == "add":
        text = (text or "").strip()
        if not text:
            return await ctx.send(err("Provide the note text."), ephemeral=True)
        notes.setdefault(key, []).append({"by": ctx.user.id, "text": clip(text, 500), "t": int(time.time())})
        mx_save()
        await ctx.send(ok("Note Added", f"**User:** {fmt_ref(user)}\n**Note:** {clip(text, 500)}"))
        return await log_mod(ctx.guild, "Mod Note", ctx.user, user, clip(text, 300))
    entries = notes.get(key, [])
    if action == "view":
        if not entries:
            return await ctx.send(ok("Mod Notes", f"No notes for {fmt_ref(user)}."), ephemeral=True)
        lines = [f"**{i}.** {n['text']}\n-# by <@{n['by']}> • <t:{n['t']}:R>" for i, n in enumerate(entries[-15:], start=max(1, len(entries) - 14))]
        return await ctx.send(ok(f"Mod Notes — {len(entries)} total", f"**User:** {fmt_ref(user)}\n\n" + "\n".join(lines)), ephemeral=True)
    if action == "remove":
        if number is None or not 1 <= number <= len(entries):
            return await ctx.send(err("Provide a valid note number."), ephemeral=True)
        removed = entries.pop(number - 1)
        if not entries:
            notes.pop(key, None)
        mx_save()
        return await ctx.send(ok("Note Removed", f"**User:** {fmt_ref(user)}\n**Removed:** {removed['text']}"))
    await ctx.send(err("Unknown modnote action."), ephemeral=True)


async def mx_modstats(ctx, days: int = 7):
    if not mx_can(ctx.user, "manage_messages", "moderate_members"):
        return await mx_deny(ctx)
    days = clamp(days, 1, 365)
    cutoff = int(time.time()) - days * 86400
    entries = [e for e in mx_cfg()["modlog"] if e["t"] >= cutoff]
    if not entries:
        return await ctx.send(ok("Moderation Stats", f"No moderation activity in the last {days} day(s)."))
    by_action = Counter(e["a"] for e in entries)
    by_mod = Counter(e["m"] for e in entries)
    action_lines = "\n".join(f"**{a}:** {n}" for a, n in by_action.most_common(12))
    mod_lines = "\n".join(f"<@{m}> — {n}" for m, n in by_mod.most_common(5))
    await ctx.send(ok(f"Moderation Stats — {days} day(s)", f"**Total actions:** {len(entries)}\n\n{action_lines}\n\n**Top moderators**\n{mod_lines}"))


async def mx_moveall(ctx, channel):
    if not mx_can(ctx.user, "move_members"):
        return await mx_deny(ctx)
    voice = getattr(ctx.user, "voice", None)
    if voice is None or voice.channel is None:
        return await ctx.send(err("Join a voice channel first."), ephemeral=True)
    source = voice.channel
    if source.id == channel.id:
        return await ctx.send(err("That's the channel you're already in."), ephemeral=True)
    members = list(source.members)
    await ctx.working(f"Moving {len(members)} member(s) to {channel.mention}...")
    moved = failed = 0
    for m in members:
        try:
            await m.move_to(channel, reason=f"Move all by {ctx.user}")
            moved += 1
        except Exception:
            failed += 1
    await ctx.send(ok("Move All Complete", f"**From:** {source.mention}\n**To:** {channel.mention}\n**Moved:** {moved}\n**Failed:** {failed}"))
    await log_mod(ctx.guild, "Move All", ctx.user, None, None, f"**From:** {source.mention}\n**To:** {channel.mention}\n**Moved:** {moved}")


async def mx_picperms(ctx, member, channel=None):
    if not mx_can(ctx.user, "manage_roles", "manage_channels"):
        return await mx_deny(ctx)
    channel = channel or ctx.channel
    problem = mx_hier_error(ctx.guild, ctx.user, member)
    if problem:
        return await ctx.send(err(problem), ephemeral=True)
    ow = channel.overwrites_for(member)
    blocked = ow.attach_files is False or ow.embed_links is False
    if blocked:
        ow.attach_files = None
        ow.embed_links = None
    else:
        ow.attach_files = False
        ow.embed_links = False
    if ow.is_empty():
        await channel.set_permissions(member, overwrite=None, reason=f"Pic perms by {ctx.user}")
    else:
        await channel.set_permissions(member, overwrite=ow, reason=f"Pic perms by {ctx.user}")
    state = "restored" if blocked else "removed"
    await ctx.send(ok("Pic Perms Toggled", f"**Member:** {member.mention}\n**Channel:** {channel.mention}\n**Images/links:** {state}"))
    await log_mod(ctx.guild, "Pic Perms", ctx.user, member, None, f"**Channel:** {channel.mention}\n**Images/links:** {state}")


def mx_emoji_norm(text: str) -> str:
    return str(text).replace("\ufe0f", "")


def mx_rr_role(entry, emoji) -> int:
    target = mx_emoji_norm(emoji)
    for key, rid in entry["map"].items():
        if mx_emoji_norm(key) == target:
            return rid
    return 0


def mx_parse_message_ref(text: str, default_channel_id: int):
    m = re.search(r"/channels/\d+/(\d+)/(\d+)", text or "")
    if m:
        return int(m.group(1)), int(m.group(2))
    if (text or "").isdigit():
        return default_channel_id, int(text)
    return None, None


async def mx_reactionrole(ctx, action: str, message_ref: str = None, emoji: str = None, role=None, channel=None):
    if not mx_can(ctx.user, "manage_roles"):
        return await mx_deny(ctx)
    store = mx_cfg()["reactionroles"]
    if action == "list":
        if not store:
            return await ctx.send(ok("Reaction Roles", "No reaction roles are set."))
        lines = []
        for mid, entry in store.items():
            pairs = ", ".join(f"{e} → <@&{r}>" for e, r in entry["map"].items())
            lines.append(f"<#{entry['channel']}> `{mid}`: {pairs}")
        return await ctx.send(ok("Reaction Roles", "\n".join(lines)))
    default_channel = channel.id if channel else ctx.channel.id
    channel_id, message_id = mx_parse_message_ref(message_ref, default_channel)
    if message_id is None:
        return await ctx.send(err("Provide a valid message ID or link."), ephemeral=True)
    target_channel = ctx.guild.get_channel(channel_id)
    if target_channel is None:
        return await ctx.send(err("I can't find that channel."), ephemeral=True)
    if not emoji:
        return await ctx.send(err("Provide an emoji."), ephemeral=True)
    partial = discord.PartialEmoji.from_str(emoji.strip())
    key = mx_emoji_norm(partial)
    if action == "add":
        if role is None:
            return await ctx.send(err("Provide a role."), ephemeral=True)
        problem = mx_role_error(ctx.guild, ctx.user, role)
        if problem:
            return await ctx.send(err(problem), ephemeral=True)
        try:
            msg = await target_channel.fetch_message(message_id)
        except Exception:
            return await ctx.send(err("I can't find that message."), ephemeral=True)
        try:
            await msg.add_reaction(partial)
        except Exception:
            return await ctx.send(err("I couldn't add that emoji. Use a standard emoji or one from this server."), ephemeral=True)
        entry = store.setdefault(str(message_id), {"channel": target_channel.id, "map": {}})
        entry["map"][key] = role.id
        mx_save()
        return await ctx.send(ok("Reaction Role Added", f"**Message:** {msg.jump_url}\n**Emoji:** {key}\n**Role:** {role.mention}"))
    if action == "remove":
        entry = store.get(str(message_id))
        if entry is None or not mx_rr_role(entry, key):
            return await ctx.send(err("No reaction role found for that message and emoji."), ephemeral=True)
        for existing in list(entry["map"]):
            if mx_emoji_norm(existing) == key:
                entry["map"].pop(existing, None)
        if not entry["map"]:
            store.pop(str(message_id), None)
        mx_save()
        return await ctx.send(ok("Reaction Role Removed", f"**Message ID:** `{message_id}`\n**Emoji:** {key}"))
    await ctx.send(err("Unknown reaction role action."), ephemeral=True)


async def mx_restrict(ctx, member):
    if not mx_can(ctx.user, "manage_messages", "moderate_members"):
        return await mx_deny(ctx)
    problem = mx_hier_error(ctx.guild, ctx.user, member)
    if problem:
        return await ctx.send(err(problem), ephemeral=True)
    ids = mx_cfg()["restricted"]
    if member.id in ids:
        ids.remove(member.id)
        mx_save()
        await ctx.send(ok("Member Unrestricted", f"{member.mention} can post images and add reactions again."))
        return await log_mod(ctx.guild, "Unrestrict", ctx.user, member)
    ids.append(member.id)
    mx_save()
    await ctx.send(ok("Member Restricted", f"{member.mention} can no longer post images or add reactions."))
    await log_mod(ctx.guild, "Restrict", ctx.user, member)


async def mx_softban(ctx, user, reason: str = "No reason provided"):
    if not mx_can(ctx.user, "ban_members"):
        return await mx_deny(ctx)
    problem = mx_hier_error(ctx.guild, ctx.user, user)
    if problem:
        return await ctx.send(err(problem), ephemeral=True)
    await ctx.defer()
    mark_action("ban", user.id)
    await ctx.guild.ban(user, reason=f"Softban by {ctx.user}: {reason}", delete_message_days=7)
    mark_action("unban", user.id)
    await ctx.guild.unban(user, reason="Softban")
    await ctx.send(ok("Member Softbanned", f"**User:** {fmt_ref(user)}\n**Reason:** {reason}\nTheir last 7 days of messages were deleted."))
    await log_mod(ctx.guild, "Softban", ctx.user, user, reason)


async def mx_staffroles(ctx, action: str, role=None):
    if not mx_can(ctx.user, "administrator"):
        return await mx_deny(ctx)
    ids = mx_cfg()["staffroles"]
    if action == "list":
        text = "\n".join(f"<@&{i}>" for i in ids) or "No staff roles set."
        return await ctx.send(ok("Staff Roles", text + "\n\nThese roles are exempt from the honeypot and can see ModMail threads."))
    if role is None:
        return await ctx.send(err("Provide a role."), ephemeral=True)
    if action == "add":
        if role.id in ids:
            return await ctx.send(err("That role is already a staff role."), ephemeral=True)
        ids.append(role.id)
        mx_save()
        await ctx.send(ok("Staff Role Added", f"{role.mention} is now a staff role."))
        return await log_mod(ctx.guild, "Staff Roles", ctx.user, None, None, f"**Added:** {role.mention}")
    if role.id not in ids:
        return await ctx.send(err("That role isn't a staff role."), ephemeral=True)
    ids.remove(role.id)
    mx_save()
    await ctx.send(ok("Staff Role Removed", f"{role.mention} is no longer a staff role."))
    await log_mod(ctx.guild, "Staff Roles", ctx.user, None, None, f"**Removed:** {role.mention}")


async def mx_stickyroles(ctx, action: str):
    if not mx_can(ctx.user, "manage_guild"):
        return await mx_deny(ctx)
    cfg = mx_cfg()
    if action == "status":
        return await ctx.send(ok("Sticky Roles", f"**Enabled:** {cfg['sticky_on']}\n**Members saved:** {len(cfg['sticky'])}"))
    cfg["sticky_on"] = action == "on"
    if not cfg["sticky_on"]:
        cfg["sticky"] = {}
    mx_save()
    await ctx.send(ok("Sticky Roles", "Roles will be re-applied when members rejoin." if cfg["sticky_on"] else "Sticky roles disabled and saved roles cleared."))


async def mx_extraowner(ctx, action: str, member=None):
    if ctx.user.id != ctx.guild.owner_id:
        return await ctx.send(err("Only the server owner can manage extra owners."), ephemeral=True)
    ids = whitelist_ids()
    if action == "list":
        text = "\n".join(f"<@{i}>" for i in ids) or "No extra owners."
        return await ctx.send(ok("Extra Owners", text))
    if member is None:
        return await ctx.send(err("Provide a member."), ephemeral=True)
    if action == "add":
        if member.id in ids:
            return await ctx.send(err("That user already has owner-level access."), ephemeral=True)
        ids.append(member.id)
        save_config(config)
        await ctx.send(ok("Extra Owner Added", f"{member.mention} is now a trusted user with owner-level access to the staff commands."))
        return await log_mod(ctx.guild, "Extra Owner", ctx.user, member, None, "**Added**")
    if member.id not in ids:
        return await ctx.send(err("That user isn't an extra owner."), ephemeral=True)
    ids.remove(member.id)
    save_config(config)
    await ctx.send(ok("Extra Owner Removed", f"{member.mention} no longer has owner-level access."))
    await log_mod(ctx.guild, "Extra Owner", ctx.user, member, None, "**Removed**")


def mx_has_image(message) -> bool:
    if message.attachments:
        return True
    return bool(MX_IMAGE_RE.search(message.content or ""))


async def mx_message_hook(message) -> bool:
    if message.guild is None:
        return await mx_modmail_dm(message)
    if message.guild.id != GUILD_ID:
        return False
    member = message.author
    if not isinstance(member, discord.Member):
        return False
    cfg = mx_cfg()
    hp = cfg["honeypot"]
    if hp and message.channel.id == hp.get("channel") and not mx_exempt(member):
        try:
            await message.delete()
        except Exception:
            pass
        reason = "Honeypot triggered"
        failure = await mx_punish(message.guild, member, hp.get("punishment", "ban"), reason, hp.get("minutes", 60), hp.get("role", 0))
        extra = f"**Action:** {hp.get('punishment', 'ban')}\n**Channel:** {message.channel.mention}"
        if failure:
            extra += f"\n**Failed:** {clip(failure, 200)}"
        else:
            hp["count"] = hp.get("count", 0) + 1
            mx_save()
            mx_hp_schedule()
        await log_mod(message.guild, "Honeypot", bot.user, member, reason, extra)
        return True
    if message.content.startswith(PREFIX) and message.channel.id in cfg["ignored"] and not mx_super(member):
        return True
    if member.id in cfg["restricted"] and mx_has_image(message):
        try:
            await message.delete()
            sent = await message.channel.send(f"{member.mention} you are restricted from posting images.", allowed_mentions=discord.AllowedMentions(users=[member]))
            asyncio.create_task(delete_after(sent, 5))
        except Exception:
            pass
        return True
    return False


async def mx_on_join(member):
    cfg = mx_cfg()
    guild = member.guild
    forced = cfg["forcenick"].get(str(member.id))
    if forced:
        try:
            await member.edit(nick=forced, reason="Forced nickname")
        except Exception:
            pass
    if str(member.id) in cfg["jail"]:
        role = guild.get_role(cfg["jail_cfg"].get("role", 0))
        if role is not None:
            try:
                await member.add_roles(role, reason="Jail persists after rejoin")
            except Exception:
                pass
        return
    saved = cfg["sticky"].pop(str(member.id), None) if cfg["sticky_on"] else None
    if saved:
        roles = [guild.get_role(i) for i in saved]
        roles = [r for r in roles if r is not None and not r.managed and not r.is_default() and r < guild.me.top_role]
        if roles:
            try:
                await member.add_roles(*roles, reason="Sticky roles")
            except Exception:
                pass
        mx_save()


def mx_on_leave(member):
    cfg = mx_cfg()
    if not cfg["sticky_on"] or member.bot:
        return
    roles = [r.id for r in member.roles if not r.is_default() and not r.managed]
    if roles:
        cfg["sticky"][str(member.id)] = roles
        mx_save()


async def mx_on_member_update(before, after):
    forced = mx_cfg()["forcenick"].get(str(after.id))
    if forced and after.nick != forced:
        try:
            await after.edit(nick=forced, reason="Forced nickname")
        except Exception:
            pass


@bot.event
async def on_raw_reaction_add(payload):
    if payload.guild_id != GUILD_ID or bot.user is None or payload.user_id == bot.user.id:
        return
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return
    cfg = mx_cfg()
    if payload.user_id in cfg["restricted"]:
        channel = guild.get_channel_or_thread(payload.channel_id)
        if channel is not None:
            try:
                await channel.get_partial_message(payload.message_id).remove_reaction(payload.emoji, discord.Object(id=payload.user_id))
            except Exception:
                pass
        return
    entry = cfg["reactionroles"].get(str(payload.message_id))
    if entry is None:
        return
    role = guild.get_role(mx_rr_role(entry, payload.emoji))
    member = payload.member or guild.get_member(payload.user_id)
    if role is None or member is None or member.bot:
        return
    try:
        await member.add_roles(role, reason="Reaction role")
    except Exception:
        pass


@bot.event
async def on_raw_reaction_remove(payload):
    if payload.guild_id != GUILD_ID or bot.user is None or payload.user_id == bot.user.id:
        return
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return
    entry = mx_cfg()["reactionroles"].get(str(payload.message_id))
    if entry is None:
        return
    role = guild.get_role(mx_rr_role(entry, payload.emoji))
    member = guild.get_member(payload.user_id)
    if role is None or member is None:
        return
    try:
        await member.remove_roles(role, reason="Reaction role")
    except Exception:
        pass


async def mx_interaction_check(interaction: discord.Interaction) -> bool:
    if (
        interaction.guild_id == GUILD_ID
        and interaction.type == discord.InteractionType.application_command
        and interaction.channel_id in mx_cfg()["ignored"]
        and isinstance(interaction.user, discord.Member)
        and not mx_super(interaction.user)
    ):
        await interaction.response.send_message(view=err("Commands are ignored in this channel."), ephemeral=True)
        return False
    return True


tree.interaction_check = mx_interaction_check


def mx_first(items):
    return items[0] if items else None


async def mxp_dm(ctx, message, args):
    member = mx_first(mx_members(message, args[1:2]))
    text = mx_tail(message, 2)
    if member is None or not text:
        return await ctx.send(err(f"Usage: `{PREFIX}dm @user <message>`"))
    await mx_dm(ctx, member, text)


async def mxp_roleall(ctx, message, args):
    action = "remove" if len(args) > 1 and args[1].lower() == "remove" else "add"
    role = mx_first(mx_roles(message, args[1:]))
    target = args[-1].lower() if args[-1].lower() in ("everyone", "humans", "bots") else "humans"
    if role is None:
        return await ctx.send(err(f"Usage: `{PREFIX}roleall <add|remove> @role [everyone|humans|bots]`"))
    await mx_roleall(ctx, action, role, target)


async def mxp_massrole(ctx, message, args):
    if len(args) < 3 or args[1].lower() not in ("add", "remove"):
        return await ctx.send(err(f"Usage: `{PREFIX}massrole <add|remove> @role @user @user ...`"))
    role = mx_first(mx_roles(message, args[2:]))
    members = mx_members(message, args[2:])
    if role is None:
        return await ctx.send(err("Provide a valid role."))
    await mx_massrole(ctx, args[1].lower(), role, members)


async def mxp_giverole(ctx, message, args):
    member = mx_first(mx_members(message, args[1:]))
    role = mx_first(mx_roles(message, args[1:]))
    if member is None or role is None:
        return await ctx.send(err(f"Usage: `{PREFIX}giverole @user @role`"))
    await mx_giverole(ctx, member, role)


async def mxp_role(ctx, message, args):
    usage = (
        f"Usage:\n`{PREFIX}role create <name> [#hex]`\n`{PREFIX}role delete @role`\n`{PREFIX}role rename @role <name>`\n"
        f"`{PREFIX}role color @role <#hex>`\n`{PREFIX}role hoist @role`\n`{PREFIX}role mentionable @role`\n"
        f"`{PREFIX}role add @user @role`\n`{PREFIX}role remove @user @role`"
    )
    if len(args) < 2:
        return await ctx.send(err(usage))
    action = args[1].lower()
    if action == "create":
        parts = args[2:]
        color = None
        if parts and parts[-1].startswith("#") and mx_color(parts[-1]) is not None:
            color = parts[-1]
            parts = parts[:-1]
        name = " ".join(parts)
        return await mx_role(ctx, "create", name=name, color=color)
    if action in ("delete", "hoist", "mentionable"):
        role = mx_first(mx_roles(message, args[2:]))
        return await mx_role(ctx, action, role=role)
    if action == "rename":
        role = mx_first(mx_roles(message, args[2:3]))
        return await mx_role(ctx, "rename", role=role, name=mx_tail(message, 3))
    if action == "color":
        role = mx_first(mx_roles(message, args[2:3]))
        return await mx_role(ctx, "color", role=role, color=args[3] if len(args) > 3 else None)
    if action in ("add", "remove"):
        member = mx_first(mx_members(message, args[2:]))
        role = mx_first(mx_roles(message, args[2:]))
        return await mx_role(ctx, action, role=role, member=member)
    await ctx.send(err(usage))


async def mxp_forcenick(ctx, message, args):
    member = mx_first(mx_members(message, args[1:2]))
    if member is None:
        return await ctx.send(err(f"Usage: `{PREFIX}forcenick @user <nickname|off>`"))
    await mx_forcenick(ctx, member, mx_tail(message, 2))


async def mxp_honeypot(ctx, message, args):
    sub = args[1].lower() if len(args) > 1 else "info"
    if sub in ("off", "stats"):
        return await mx_honeypot(ctx, sub)
    if sub != "setup":
        return await mx_honeypot(ctx, "info")
    rest = args[2:]
    channel = mx_first([c for c in mx_channels(message, rest) if isinstance(c, discord.TextChannel)])
    punishment = next((a.lower() for a in rest if a.lower() in MX_PUNISHMENTS), None)
    minutes = next((int(a) for a in rest if a.isdigit() and len(a) < 6), None)
    role = mx_first(mx_roles(message, rest))
    await mx_honeypot(ctx, "setup", channel, punishment, minutes, role)


async def mxp_ignoredchannels(ctx, message, args):
    sub = args[1].lower() if len(args) > 1 else "list"
    if sub not in ("add", "remove", "list"):
        return await ctx.send(err(f"Usage: `{PREFIX}ignoredchannels <add|remove|list> [#channel]`"))
    await mx_ignored(ctx, sub, mx_first(mx_channels(message, args[2:])))


async def mxp_jail(ctx, message, args):
    if len(args) > 1 and args[1].lower() == "setup":
        return await mx_jail_setup(ctx)
    member = mx_first(mx_members(message, args[1:2]))
    if member is None:
        return await ctx.send(err(f"Usage: `{PREFIX}jail @user [reason]` | `{PREFIX}jail setup`"))
    await mx_jail(ctx, member, mx_tail(message, 2) or "No reason provided")


async def mxp_unjail(ctx, message, args):
    member = mx_first(mx_members(message, args[1:2]))
    if member is None:
        return await ctx.send(err(f"Usage: `{PREFIX}unjail @user`"))
    await mx_unjail(ctx, member)


async def mxp_masspurge(ctx, message, args):
    user = await mx_user_from(message, args[1:2])
    if user is None:
        return await ctx.send(err(f"Usage: `{PREFIX}masspurge @user|id [messages per channel]`"))
    limit = 100
    if len(args) > 2 and args[-1].isdigit() and len(args[-1]) < 6:
        limit = int(args[-1])
    await mx_masspurge(ctx, user, limit)


async def mxp_modmail(ctx, message, args):
    sub = args[1].lower() if len(args) > 1 else "status"
    if sub == "setup":
        category = None
        if len(args) > 2:
            token = " ".join(args[2:])
            if token.isdigit():
                category = ctx.guild.get_channel(int(token))
            else:
                category = discord.utils.find(lambda c: c.name.lower() == token.lower(), ctx.guild.categories)
        if not isinstance(category, discord.CategoryChannel):
            return await ctx.send(err(f"Usage: `{PREFIX}modmail setup <category id or name>`"))
        return await mx_modmail(ctx, "setup", category=category)
    if sub in ("reply", "close"):
        return await mx_modmail(ctx, sub, text=mx_tail(message, 2))
    if sub in ("block", "unblock"):
        member = mx_first(mx_members(message, args[2:]))
        return await mx_modmail(ctx, sub, member=member)
    if sub in ("off", "status"):
        return await mx_modmail(ctx, sub)
    await ctx.send(err(f"Usage: `{PREFIX}modmail <setup|off|status|reply|close|block|unblock>`"))


async def mxp_modnote(ctx, message, args):
    sub = args[1].lower() if len(args) > 1 else ""
    if sub in ("add", "view", "remove"):
        user = await mx_user_from(message, args[2:3])
        if user is None:
            return await ctx.send(err(f"Usage: `{PREFIX}modnote <add|view|remove> @user [note|number]`"))
        if sub == "add":
            return await mx_modnote(ctx, "add", user, text=mx_tail(message, 3))
        if sub == "remove":
            number = int(args[3]) if len(args) > 3 and args[3].isdigit() else None
            return await mx_modnote(ctx, "remove", user, number=number)
        return await mx_modnote(ctx, "view", user)
    user = await mx_user_from(message, args[1:2])
    if user is None:
        return await ctx.send(err(f"Usage: `{PREFIX}modnote <add|view|remove> @user [note|number]`"))
    await mx_modnote(ctx, "view", user)


async def mxp_modstats(ctx, message, args):
    days = int(args[1]) if len(args) > 1 and args[1].isdigit() else 7
    await mx_modstats(ctx, days)


async def mxp_moveall(ctx, message, args):
    channel = next((c for c in mx_channels(message, args[1:]) if isinstance(c, (discord.VoiceChannel, discord.StageChannel))), None)
    if channel is None:
        return await ctx.send(err(f"Usage: `{PREFIX}moveall #voice-channel`"))
    await mx_moveall(ctx, channel)


async def mxp_picperms(ctx, message, args):
    member = mx_first(mx_members(message, args[1:]))
    if member is None:
        return await ctx.send(err(f"Usage: `{PREFIX}picperms @user [#channel]`"))
    channel = next((c for c in mx_channels(message, args[1:]) if isinstance(c, discord.TextChannel)), None)
    await mx_picperms(ctx, member, channel)


async def mxp_reactionrole(ctx, message, args):
    sub = args[1].lower() if len(args) > 1 else ""
    usage = f"Usage: `{PREFIX}reactionrole add <message id|link> <emoji> @role` | `{PREFIX}reactionrole remove <message id|link> <emoji>` | `{PREFIX}reactionrole list`"
    if sub == "list":
        return await mx_reactionrole(ctx, "list")
    if sub not in ("add", "remove") or len(args) < 4:
        return await ctx.send(err(usage))
    role = mx_first(mx_roles(message, args[4:])) if sub == "add" else None
    channel = next((c for c in mx_channels(message, args[4:]) if isinstance(c, discord.TextChannel)), None)
    await mx_reactionrole(ctx, sub, args[2], args[3], role, channel)


async def mxp_restrict(ctx, message, args):
    member = mx_first(mx_members(message, args[1:2]))
    if member is None:
        return await ctx.send(err(f"Usage: `{PREFIX}restrict @user`"))
    await mx_restrict(ctx, member)


async def mxp_softban(ctx, message, args):
    user = await mx_user_from(message, args[1:2])
    if user is None:
        return await ctx.send(err(f"Usage: `{PREFIX}softban @user [reason]`"))
    await mx_softban(ctx, user, mx_tail(message, 2) or "No reason provided")


async def mxp_staffroles(ctx, message, args):
    sub = args[1].lower() if len(args) > 1 else "list"
    if sub not in ("add", "remove", "list"):
        return await ctx.send(err(f"Usage: `{PREFIX}staffroles <add|remove|list> [@role]`"))
    await mx_staffroles(ctx, sub, mx_first(mx_roles(message, args[2:])))


async def mxp_stickyroles(ctx, message, args):
    sub = args[1].lower() if len(args) > 1 else "status"
    if sub not in ("on", "off", "status"):
        return await ctx.send(err(f"Usage: `{PREFIX}stickyroles <on|off|status>`"))
    await mx_stickyroles(ctx, sub)


async def mxp_extraowner(ctx, message, args):
    sub = args[1].lower() if len(args) > 1 else "list"
    if sub not in ("add", "remove", "list"):
        return await ctx.send(err(f"Usage: `{PREFIX}extraowner <add|remove|list> [@user]`"))
    await mx_extraowner(ctx, sub, mx_first(mx_members(message, args[2:])))


MX_PREFIX = {
    "dm": mxp_dm,
    "roleall": mxp_roleall,
    "massrole": mxp_massrole,
    "giverole": mxp_giverole,
    "role": mxp_role,
    "forcenick": mxp_forcenick,
    "honeypot": mxp_honeypot,
    "ignoredchannels": mxp_ignoredchannels,
    "jail": mxp_jail,
    "unjail": mxp_unjail,
    "masspurge": mxp_masspurge,
    "modmail": mxp_modmail,
    "modnote": mxp_modnote,
    "modstats": mxp_modstats,
    "moveall": mxp_moveall,
    "picperms": mxp_picperms,
    "reactionrole": mxp_reactionrole,
    "restrict": mxp_restrict,
    "softban": mxp_softban,
    "staffroles": mxp_staffroles,
    "stickyroles": mxp_stickyroles,
    "extraowner": mxp_extraowner,
}


async def mx_prefix(message: discord.Message, cmd: str, args: list) -> bool:
    fn = MX_PREFIX.get(cmd)
    if fn is None:
        return False
    ctx = MxCtx(message)
    await mx_guard(ctx, fn(ctx, message, args))
    return True


@tree.command(name="dm", description="Send a direct message to a member as staff", guild=GUILD_OBJ)
@discord.app_commands.describe(member="Member to message", message="Message to send")
@discord.app_commands.default_permissions(manage_messages=True)
async def dm_slash(interaction: discord.Interaction, member: discord.Member, message: str):
    ctx = MxCtx(interaction)
    await mx_guard(ctx, mx_dm(ctx, member, message))


@tree.command(name="roleall", description="Add or remove a role from every member", guild=GUILD_OBJ)
@discord.app_commands.describe(action="Add or remove", role="Role to apply", target="Who to apply it to")
@discord.app_commands.choices(
    action=mx_choices("add", "remove"),
    target=mx_choices("humans", "bots", "everyone")
)
@discord.app_commands.default_permissions(manage_roles=True)
async def roleall_slash(interaction: discord.Interaction, action: discord.app_commands.Choice[str], role: discord.Role, target: discord.app_commands.Choice[str] = None):
    ctx = MxCtx(interaction)
    await mx_guard(ctx, mx_roleall(ctx, action.value, role, target.value if target else "humans"))


@tree.command(name="massrole", description="Add or remove a role from many members at once", guild=GUILD_OBJ)
@discord.app_commands.describe(action="Add or remove", role="Role to apply", members="Mention or paste IDs of the members")
@discord.app_commands.choices(action=mx_choices("add", "remove"))
@discord.app_commands.default_permissions(manage_roles=True)
async def massrole_slash(interaction: discord.Interaction, action: discord.app_commands.Choice[str], role: discord.Role, members: str):
    ctx = MxCtx(interaction)
    found = [interaction.guild.get_member(i) for i in mx_ids_in(members)]
    await mx_guard(ctx, mx_massrole(ctx, action.value, role, [m for m in found if m is not None]))


@tree.command(name="giverole", description="Give a role to a member", guild=GUILD_OBJ)
@discord.app_commands.describe(member="Member to give the role to", role="Role to give")
@discord.app_commands.default_permissions(manage_roles=True)
async def giverole_slash(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    ctx = MxCtx(interaction)
    await mx_guard(ctx, mx_giverole(ctx, member, role))


@tree.command(name="role", description="Create, edit and manage roles", guild=GUILD_OBJ)
@discord.app_commands.describe(action="What to do", role="Target role", name="Name for create or rename", color="Hex color like #ff0000", member="Member for add or remove")
@discord.app_commands.choices(action=mx_choices("create", "delete", "rename", "color", "hoist", "mentionable", "add", "remove"))
@discord.app_commands.default_permissions(manage_roles=True)
async def role_slash(
    interaction: discord.Interaction,
    action: discord.app_commands.Choice[str],
    role: discord.Role = None,
    name: str = None,
    color: str = None,
    member: discord.Member = None
):
    ctx = MxCtx(interaction)
    await mx_guard(ctx, mx_role(ctx, action.value, role, name, color, member))


@tree.command(name="forcenick", description="Lock a member to a nickname", guild=GUILD_OBJ)
@discord.app_commands.describe(member="Member to lock", nickname="Nickname to force (leave empty to remove the lock)")
@discord.app_commands.default_permissions(manage_nicknames=True)
async def forcenick_slash(interaction: discord.Interaction, member: discord.Member, nickname: str = None):
    ctx = MxCtx(interaction)
    await mx_guard(ctx, mx_forcenick(ctx, member, nickname))


@tree.command(name="honeypot", description="Trap channel that punishes anyone who writes in it", guild=GUILD_OBJ)
@discord.app_commands.describe(action="Setup, disable, statistics or usage", channel="Honeypot channel", punishment="What happens to the offender", minutes="Timeout length in minutes", role="Quarantine role")
@discord.app_commands.choices(action=mx_choices("setup", "off", "stats", "info"), punishment=mx_choices(*MX_PUNISHMENTS))
@discord.app_commands.default_permissions(manage_guild=True)
async def honeypot_slash(
    interaction: discord.Interaction,
    action: discord.app_commands.Choice[str],
    channel: discord.TextChannel = None,
    punishment: discord.app_commands.Choice[str] = None,
    minutes: int = None,
    role: discord.Role = None
):
    ctx = MxCtx(interaction)
    await mx_guard(ctx, mx_honeypot(ctx, action.value, channel, punishment.value if punishment else None, minutes, role))


@tree.command(name="ignoredchannels", description="Manage channels where bot commands are ignored", guild=GUILD_OBJ)
@discord.app_commands.describe(action="Add, remove or list", channel="Channel to change")
@discord.app_commands.choices(action=mx_choices("add", "remove", "list"))
@discord.app_commands.default_permissions(manage_guild=True)
async def ignoredchannels_slash(interaction: discord.Interaction, action: discord.app_commands.Choice[str], channel: discord.TextChannel = None):
    ctx = MxCtx(interaction)
    await mx_guard(ctx, mx_ignored(ctx, action.value, channel))


@tree.command(name="jail", description="Lock a member into the jail channel by stripping their roles", guild=GUILD_OBJ)
@discord.app_commands.describe(member="Member to jail", reason="Reason for the jail")
@discord.app_commands.default_permissions(moderate_members=True)
async def jail_slash(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    ctx = MxCtx(interaction)
    await mx_guard(ctx, mx_jail(ctx, member, reason))


@tree.command(name="unjail", description="Release a jailed member and restore their roles", guild=GUILD_OBJ)
@discord.app_commands.describe(member="Member to release")
@discord.app_commands.default_permissions(moderate_members=True)
async def unjail_slash(interaction: discord.Interaction, member: discord.Member):
    ctx = MxCtx(interaction)
    await mx_guard(ctx, mx_unjail(ctx, member))


@tree.command(name="jailsetup", description="Create the jail role and channel", guild=GUILD_OBJ)
@discord.app_commands.default_permissions(administrator=True)
async def jailsetup_slash(interaction: discord.Interaction):
    ctx = MxCtx(interaction)
    await mx_guard(ctx, mx_jail_setup(ctx))


@tree.command(name="masspurge", description="Delete a user's messages across every channel", guild=GUILD_OBJ)
@discord.app_commands.describe(user="User whose messages to delete", limit="Messages to scan per channel (max 1000)")
@discord.app_commands.default_permissions(manage_messages=True)
async def masspurge_slash(interaction: discord.Interaction, user: discord.User, limit: int = 100):
    ctx = MxCtx(interaction)
    await mx_guard(ctx, mx_masspurge(ctx, user, limit))


@tree.command(name="modmail", description="Configure and use ModMail (DM-to-staff messaging)", guild=GUILD_OBJ)
@discord.app_commands.describe(action="What to do", category="Category for threads (setup)", text="Reply text or close reason", member="Member to block or unblock")
@discord.app_commands.choices(action=mx_choices("setup", "off", "status", "reply", "close", "block", "unblock"))
@discord.app_commands.default_permissions(manage_messages=True)
async def modmail_slash(
    interaction: discord.Interaction,
    action: discord.app_commands.Choice[str],
    category: discord.CategoryChannel = None,
    text: str = None,
    member: discord.Member = None
):
    ctx = MxCtx(interaction)
    await mx_guard(ctx, mx_modmail(ctx, action.value, category, text, member))


@tree.command(name="modnote", description="Add or view private staff notes for a user", guild=GUILD_OBJ)
@discord.app_commands.describe(action="Add, view or remove", user="User the note is about", text="Note text (add)", number="Note number (remove)")
@discord.app_commands.choices(action=mx_choices("add", "view", "remove"))
@discord.app_commands.default_permissions(manage_messages=True)
async def modnote_slash(
    interaction: discord.Interaction,
    action: discord.app_commands.Choice[str],
    user: discord.User,
    text: str = None,
    number: int = None
):
    ctx = MxCtx(interaction)
    await mx_guard(ctx, mx_modnote(ctx, action.value, user, text, number))


@tree.command(name="modstats", description="Moderation activity over the last days", guild=GUILD_OBJ)
@discord.app_commands.describe(days="How many days to include")
@discord.app_commands.default_permissions(manage_messages=True)
async def modstats_slash(interaction: discord.Interaction, days: int = 7):
    ctx = MxCtx(interaction)
    await mx_guard(ctx, mx_modstats(ctx, days))


@tree.command(name="moveall", description="Move everyone in your voice channel to another channel", guild=GUILD_OBJ)
@discord.app_commands.describe(channel="Voice channel to move everyone to")
@discord.app_commands.default_permissions(move_members=True)
async def moveall_slash(interaction: discord.Interaction, channel: discord.VoiceChannel):
    ctx = MxCtx(interaction)
    await mx_guard(ctx, mx_moveall(ctx, channel))


@tree.command(name="picperms", description="Toggle a member's permission to post images/links in a channel", guild=GUILD_OBJ)
@discord.app_commands.describe(member="Member to toggle", channel="Channel (defaults to this one)")
@discord.app_commands.default_permissions(manage_roles=True)
async def picperms_slash(interaction: discord.Interaction, member: discord.Member, channel: discord.TextChannel = None):
    ctx = MxCtx(interaction)
    await mx_guard(ctx, mx_picperms(ctx, member, channel))


@tree.command(name="reactionrole", description="Give a role when members react to a message with an emoji", guild=GUILD_OBJ)
@discord.app_commands.describe(action="Add, remove or list", message_id="Message ID or link", emoji="Emoji to react with", role="Role to give", channel="Channel of the message (defaults to this one)")
@discord.app_commands.choices(action=mx_choices("add", "remove", "list"))
@discord.app_commands.default_permissions(manage_roles=True)
async def reactionrole_slash(
    interaction: discord.Interaction,
    action: discord.app_commands.Choice[str],
    message_id: str = None,
    emoji: str = None,
    role: discord.Role = None,
    channel: discord.TextChannel = None
):
    ctx = MxCtx(interaction)
    await mx_guard(ctx, mx_reactionrole(ctx, action.value, message_id, emoji, role, channel))


@tree.command(name="restrict", description="Block a member from posting images or adding reactions", guild=GUILD_OBJ)
@discord.app_commands.describe(member="Member to restrict or unrestrict")
@discord.app_commands.default_permissions(manage_messages=True)
async def restrict_slash(interaction: discord.Interaction, member: discord.Member):
    ctx = MxCtx(interaction)
    await mx_guard(ctx, mx_restrict(ctx, member))


@tree.command(name="softban", description="Ban then immediately unban a user to clear their messages", guild=GUILD_OBJ)
@discord.app_commands.describe(user="User to softban", reason="Reason for the softban")
@discord.app_commands.default_permissions(ban_members=True)
async def softban_slash(interaction: discord.Interaction, user: discord.User, reason: str = "No reason provided"):
    ctx = MxCtx(interaction)
    target = interaction.guild.get_member(user.id) or user
    await mx_guard(ctx, mx_softban(ctx, target, reason))


@tree.command(name="staffroles", description="Designate staff roles and moderators", guild=GUILD_OBJ)
@discord.app_commands.describe(action="Add, remove or list", role="Role to change")
@discord.app_commands.choices(action=mx_choices("add", "remove", "list"))
@discord.app_commands.default_permissions(administrator=True)
async def staffroles_slash(interaction: discord.Interaction, action: discord.app_commands.Choice[str], role: discord.Role = None):
    ctx = MxCtx(interaction)
    await mx_guard(ctx, mx_staffroles(ctx, action.value, role))


@tree.command(name="stickyroles", description="Re-apply a member's roles when they rejoin", guild=GUILD_OBJ)
@discord.app_commands.describe(action="Turn on, off or view status")
@discord.app_commands.choices(action=mx_choices("on", "off", "status"))
@discord.app_commands.default_permissions(manage_guild=True)
async def stickyroles_slash(interaction: discord.Interaction, action: discord.app_commands.Choice[str]):
    ctx = MxCtx(interaction)
    await mx_guard(ctx, mx_stickyroles(ctx, action.value))


@tree.command(name="extraowner", description="Grant trusted users owner-level access to the staff commands", guild=GUILD_OBJ)
@discord.app_commands.describe(action="Add, remove or list", member="Member to change")
@discord.app_commands.choices(action=mx_choices("add", "remove", "list"))
@discord.app_commands.default_permissions(administrator=True)
async def extraowner_slash(interaction: discord.Interaction, action: discord.app_commands.Choice[str], member: discord.Member = None):
    ctx = MxCtx(interaction)
    await mx_guard(ctx, mx_extraowner(ctx, action.value, member))


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
