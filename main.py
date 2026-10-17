import discord
import aiohttp
import asyncio
import urllib.parse
import json
import os
import re
import random
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


class HelpView(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# Help — Commands"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"You can use either prefix commands (`.command`) or slash commands (`/command`)"
                ),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    "**Moderation Commands**\n"
                    "`.kick @user [reason]`\n"
                    "`.ban @user [reason]`\n"
                    "`.unban <userID>`\n"
                    "`.timeout @user <minutes> [reason]`\n"
                    "`.untimeout @user`\n"
                    "`.purge <amount>`"
                ),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    "**Warning Commands**\n"
                    "`.warn @user [reason]`\n"
                    "`.warns [@user]`\n"
                    "`.clearwarns @user`"
                ),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    "**Channel Commands**\n"
                    "`.lock [#channel]`\n"
                    "`.unlock [#channel]`\n"
                    "`.slowmode <seconds> [#channel]`"
                ),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    "**Giveaway Commands**\n"
                    "`.gstart <duration> <winners> <prize>`\n"
                    "`.gend <message_id>`\n"
                    "`.greroll <message_id>`"
                ),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    "**Utility Commands**\n"
                    "`.ping`\n"
                    "`.say #channel <message>`\n"
                    "`.afk [reason]`\n"
                    "`.snipe`\n"
                    "`.avatar [@user]`\n"
                    "`.bypass <link>`\n"
                    "`.steal <emoji>`\n"
                    "`.scatter <bet>`\n"
                    "`.balance`\n"
                    "`.help`"
                ),
                discord.ui.Separator(),
                discord.ui.TextDisplay(f"Prefix: `.` | All commands also available as `/command`"),
                accent_color=discord.Color.blurple()
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


@tree.command(name="help", description="Show all commands", guild=discord.Object(id=GUILD_ID))
async def help_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(view=HelpView())


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
@discord.app_commands.describe(channel="Channel to send the message in", message="Message content")
@discord.app_commands.default_permissions(administrator=True)
async def say(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
    try:
        await channel.send(message)
        await interaction.response.send_message(view=SayView(channel, message), ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(view=FailedView(str(e)), ephemeral=True)


@tree.command(name="kick", description="Kick a member", guild=discord.Object(id=GUILD_ID))
@discord.app_commands.describe(member="Member to kick", reason="Reason for the kick")
@discord.app_commands.default_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    try:
        await member.kick(reason=reason)
        await interaction.response.send_message(view=KickResultView(member, reason))
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
        await member.ban(reason=reason, delete_message_days=delete_days)
        await interaction.response.send_message(view=BanResultView(member, reason))
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
        await interaction.guild.unban(user)
        await interaction.response.send_message(view=UnbanResultView(uid))
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
        deleted = await interaction.channel.purge(limit=amount)
        msg = await interaction.followup.send(view=PurgeResultView(len(deleted), interaction.channel), ephemeral=False)
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
        await member.timeout(until, reason=reason)
        await interaction.response.send_message(view=TimeoutResultView(member, minutes, reason))
    except discord.Forbidden:
        await interaction.response.send_message(view=FailedView("I don't have permission to timeout that member."), ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(view=FailedView(str(e)), ephemeral=True)


@tree.command(name="untimeout", description="Remove timeout from a member", guild=discord.Object(id=GUILD_ID))
@discord.app_commands.describe(member="Member to remove timeout from")
@discord.app_commands.default_permissions(moderate_members=True)
async def untimeout(interaction: discord.Interaction, member: discord.Member):
    try:
        await member.timeout(None)
        await interaction.response.send_message(view=UntimeoutResultView(member))
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


@tree.command(name="slowmode", description="Set slowmode in a channel", guild=discord.Object(id=GUILD_ID))
@discord.app_commands.describe(seconds="Slowmode delay in seconds (0 to disable)", channel="Channel to set slowmode in (defaults to current)")
@discord.app_commands.default_permissions(manage_channels=True)
async def slowmode(interaction: discord.Interaction, seconds: int, channel: discord.TextChannel = None):
    target = channel or interaction.channel
    seconds = max(0, min(21600, seconds))
    try:
        await target.edit(slowmode_delay=seconds)
        await interaction.response.send_message(view=SlowmodeView(target, seconds))
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


@tree.command(name="balance", description="Check your coin balance", guild=discord.Object(id=GUILD_ID))
async def balance_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(view=InfoView("💰 Balance", f"**Coins:** {get_balance(interaction.user.id)}"))


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


tree.add_command(giveaway_group)


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
                    await message.author.kick(reason="MrBeast scam auto detected")
                except Exception as e:
                    print(f"Kick failed: {e}")
                sent = await message.channel.send(view=UserKickedView(message.author))
                asyncio.create_task(delete_after(sent, 60))
                return

    if not message.content.startswith(PREFIX):
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

    if cmd == "help":
        await message.channel.send(view=HelpView())

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
        if not message.channel_mentions or len(args) < 3:
            await message.channel.send(view=FailedView("Usage: `.say #channel <message>`"))
            return
        target_channel = message.channel_mentions[0]
        full = message.content[len(PREFIX) + len("say "):].strip()
        mention_str = target_channel.mention
        content = full[full.index(mention_str) + len(mention_str):].strip() if mention_str in full else " ".join(args[2:])
        if not content:
            await message.channel.send(view=FailedView("Please provide a message to send."))
            return
        await target_channel.send(content)
        await message.channel.send(view=SayView(target_channel, content))

    elif cmd == "kick":
        if not message.author.guild_permissions.kick_members:
            return
        if not message.mentions:
            await message.channel.send(view=FailedView("Usage: `.kick @user [reason]`"))
            return
        member = message.mentions[0]
        reason = " ".join(args[2:]) if len(args) > 2 else "No reason provided"
        try:
            await member.kick(reason=reason)
            await message.channel.send(view=KickResultView(member, reason))
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
            await member.ban(reason=reason, delete_message_days=0)
            await message.channel.send(view=BanResultView(member, reason))
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
            await message.guild.unban(user)
            await message.channel.send(view=UnbanResultView(uid))
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
            deleted = await message.channel.purge(limit=amount)
            msg = await message.channel.send(view=PurgeResultView(len(deleted), message.channel))
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
            await member.timeout(until, reason=reason)
            await message.channel.send(view=TimeoutResultView(member, minutes, reason))
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
            await member.timeout(None)
            await message.channel.send(view=UntimeoutResultView(member))
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

    elif cmd == "balance":
        await message.channel.send(view=InfoView("💰 Balance", f"**Coins:** {get_balance(message.author.id)}"))


@bot.event
async def on_ready():
    bot.add_view(VerifyView())
    bot.add_view(GiveawayPersistentView())
    asyncio.create_task(giveaway_loop())
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print(f"Logged in as {bot.user}")
    print(f"Reference images loaded: {len(reference_hashes)}")


bot.run(TOKEN)
