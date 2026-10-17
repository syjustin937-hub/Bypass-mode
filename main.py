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
                    "`.serverinfo`\n"
                    "`.say #channel <message>`\n"
                    "`.afk [reason]`\n"
                    "`.snipe`\n"
                    "`.avatar [@user]`\n"
                    "`.bypass <link>`\n"
                    "`.stealemoji <emoji> [name]`\n"
                    "`.addemoji <name> <url>`\n"
                    "`.addsticker <name> [desc]` — reply to sticker\n"
                    "`.help`"
                ),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    "**Economy Commands**\n"
                    "`.balance [@user]` / `.bal`\n"
                    "`.daily` `.work` `.slut` `.crime`\n"
                    "`.hunt` `.search` `.beg` `.mine` `.fish` `.dig`\n"
                    "`.deposit <amount|all>` / `.dep`\n"
                    "`.withdraw <amount|all>` / `.with`\n"
                    "`.send @user <amount>` / `.pay` / `.give`\n"
                    "`.steal @user` / `.rob`\n"
                    "`.gamble <amount|all>` / `.bet`\n"
                    "`.coinflip <heads|tails> <amount|all>` / `.cf`\n"
                    "`.scatter <amount|all>`\n"
                    "`.highlow <high|low> <amount|all>` / `.hl`\n"
                    "`.shop list` `.shop inventory` `.shop sell <item>`\n"
                    "`.setcoins @user <amount>` — Server Owner only"
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

    else:
        # Try economy commands
        eco_cmds = {
            "balance", "bal", "setcoins", "daily", "deposit", "dep",
            "withdraw", "with", "send", "pay", "give", "steal", "rob",
            "work", "slut", "crime", "hunt", "search", "beg", "mine",
            "fish", "dig", "gamble", "bet", "coinflip", "cf", "scatter",
            "highlow", "hl", "shop"
        }
        util_cmds = {"serverinfo", "stealemoji", "addemoji", "addsticker"}
        if cmd in eco_cmds:
            await handle_economy(message, cmd, args)
        elif cmd in util_cmds:
            await handle_extra_utility(message, cmd, args)


@bot.event
async def on_ready():
    bot.add_view(VerifyView())
    bot.add_view(GiveawayPersistentView())
    asyncio.create_task(giveaway_loop())
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print(f"Logged in as {bot.user}")
    print(f"Reference images loaded: {len(reference_hashes)}")


bot.run(TOKEN)


# ─── ECONOMY SYSTEM ───────────────────────────────────────────────────────────

ECONOMY_FILE = "economy.json"

def load_economy():
    if os.path.exists(ECONOMY_FILE):
        with open(ECONOMY_FILE, "r") as f:
            return json.load(f)
    return {}

def save_economy(data):
    with open(ECONOMY_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_user(data, uid):
    uid = str(uid)
    if uid not in data:
        data[uid] = {"wallet": 0, "bank": 0, "last_daily": 0, "last_work": 0,
                     "last_slut": 0, "last_crime": 0, "last_hunt": 0,
                     "last_search": 0, "last_beg": 0, "last_mine": 0,
                     "last_fish": 0, "last_dig": 0, "inventory": []}
    return data[uid]

def economy_embed(title, desc, color=discord.Color.gold()):
    e = discord.Embed(title=title, description=desc, color=color)
    return e

COOLDOWNS = {
    "daily": 86400, "work": 3600, "slut": 1800, "crime": 3600,
    "hunt": 1800, "search": 900, "beg": 600, "mine": 3600,
    "fish": 1800, "dig": 2700
}

WORK_MSGS = ["You fixed some bugs", "You delivered packages", "You walked dogs",
             "You mowed lawns", "You tutored students", "You drove for a rideshare"]
SLUT_MSGS = ["You did an onlyfans shoot", "You danced at a club", "You modeled online"]
CRIME_MSGS = ["You robbed a store", "You hacked an account", "You pickpocketed someone"]
HUNT_MSGS = ["You hunted a deer", "You caught a rabbit", "You bagged a boar"]
SEARCH_MSGS = ["You searched the couch", "You found a wallet", "You dug through trash"]
BEG_MSGS = ["Someone felt generous", "A stranger tossed you coins", "You busked downtown"]
MINE_MSGS = ["You mined coal", "You found iron", "You struck gold veins"]
FISH_MSGS = ["You caught a tuna", "You reeled in a bass", "You got a rare koi"]
DIG_MSGS = ["You dug up old coins", "You found buried treasure", "You unearthed artifacts"]

SHOP_ITEMS = {
    "shovel": {"price": 500, "desc": "Dig deeper for more coins"},
    "fishing_rod": {"price": 750, "desc": "Catch bigger fish"},
    "pickaxe": {"price": 1000, "desc": "Mine faster"},
    "rifle": {"price": 1500, "desc": "Better hunting results"},
    "laptop": {"price": 2000, "desc": "Work from home faster"},
}

def check_cooldown(user_data, action):
    now = int(datetime.now(timezone.utc).timestamp())
    last = user_data.get(f"last_{action}", 0)
    cd = COOLDOWNS.get(action, 0)
    remaining = (last + cd) - now
    return remaining if remaining > 0 else 0

def format_cd(seconds):
    if seconds <= 0:
        return "Ready!"
    h, r = divmod(int(seconds), 3600)
    m, s = divmod(r, 60)
    parts = []
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    if s: parts.append(f"{s}s")
    return " ".join(parts)


# ─── ECONOMY VIEWS ────────────────────────────────────────────────────────────

class BalanceView(discord.ui.LayoutView):
    def __init__(self, member, wallet, bank):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"# 💰 {member.display_name}'s Balance"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                f"**Wallet:** 🪙 {wallet:,}\n"
                f"**Bank:** 🏦 {bank:,}\n"
                f"**Total:** 💎 {wallet + bank:,}"
            ),
            accent_color=discord.Color.gold()
        ))

class EconomyView(discord.ui.LayoutView):
    def __init__(self, title, desc, color=discord.Color.gold()):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"# {title}"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(desc),
            accent_color=color
        ))

class ShopListView(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)
        lines = []
        for item, info in SHOP_ITEMS.items():
            lines.append(f"**{item.replace('_',' ').title()}** — 🪙 {info['price']:,}\n> {info['desc']}")
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("# 🛒 Shop"),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n\n".join(lines)),
            accent_color=discord.Color.blurple()
        ))

class InventoryView(discord.ui.LayoutView):
    def __init__(self, member, inventory):
        super().__init__(timeout=None)
        if not inventory:
            body = "Your inventory is empty."
        else:
            from collections import Counter
            counts = Counter(inventory)
            body = "\n".join(f"**{k.replace('_',' ').title()}** x{v}" for k, v in counts.items())
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"# 🎒 {member.display_name}'s Inventory"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(body),
            accent_color=discord.Color.green()
        ))


# ─── ECONOMY PREFIX HANDLERS (injected into on_message) ──────────────────────

async def handle_economy(message, cmd, args):
    eco = load_economy()
    uid = str(message.author.id)
    user = get_user(eco, uid)
    now = int(datetime.now(timezone.utc).timestamp())

    # ── balance ──
    if cmd in ("balance", "bal"):
        target = message.mentions[0] if message.mentions else message.author
        t_uid = str(target.id)
        t_user = get_user(eco, t_uid)
        await message.channel.send(view=BalanceView(target, t_user["wallet"], t_user["bank"]))

    # ── setcoins (owner only) ──
    elif cmd == "setcoins":
        if message.author.id != message.guild.owner_id:
            await message.channel.send(view=EconomyView("❌ Denied", "Server Owner only.", discord.Color.red()))
            return
        if not message.mentions or len(args) < 3:
            await message.channel.send(view=EconomyView("❌ Usage", "`.setcoins @user <amount>`", discord.Color.red()))
            return
        try:
            amt = int(args[2])
        except ValueError:
            await message.channel.send(view=EconomyView("❌ Error", "Amount must be a number.", discord.Color.red()))
            return
        t_uid = str(message.mentions[0].id)
        t_user = get_user(eco, t_uid)
        t_user["wallet"] = amt
        save_economy(eco)
        await message.channel.send(view=EconomyView("✅ Coins Set", f"Set {message.mentions[0].mention}'s wallet to 🪙 {amt:,}"))

    # ── daily ──
    elif cmd == "daily":
        cd = check_cooldown(user, "daily")
        if cd:
            await message.channel.send(view=EconomyView("⏳ Cooldown", f"Daily resets in **{format_cd(cd)}**", discord.Color.orange()))
            return
        amount = random.randint(200, 500)
        user["wallet"] += amount
        user["last_daily"] = now
        save_economy(eco)
        await message.channel.send(view=EconomyView("📅 Daily Reward", f"You claimed 🪙 **{amount:,}** coins!\n**Wallet:** {user['wallet']:,}"))

    # ── deposit ──
    elif cmd in ("deposit", "dep"):
        if len(args) < 2:
            await message.channel.send(view=EconomyView("❌ Usage", "`.deposit <amount|all>`", discord.Color.red()))
            return
        amt = user["wallet"] if args[1].lower() == "all" else int(args[1])
        amt = min(amt, user["wallet"])
        if amt <= 0:
            await message.channel.send(view=EconomyView("❌ Error", "Nothing to deposit.", discord.Color.red()))
            return
        user["wallet"] -= amt
        user["bank"] += amt
        save_economy(eco)
        await message.channel.send(view=EconomyView("🏦 Deposited", f"Deposited 🪙 **{amt:,}** to bank.\n**Bank:** {user['bank']:,}"))

    # ── withdraw ──
    elif cmd in ("withdraw", "with"):
        if len(args) < 2:
            await message.channel.send(view=EconomyView("❌ Usage", "`.withdraw <amount|all>`", discord.Color.red()))
            return
        amt = user["bank"] if args[1].lower() == "all" else int(args[1])
        amt = min(amt, user["bank"])
        if amt <= 0:
            await message.channel.send(view=EconomyView("❌ Error", "Nothing to withdraw.", discord.Color.red()))
            return
        user["bank"] -= amt
        user["wallet"] += amt
        save_economy(eco)
        await message.channel.send(view=EconomyView("💸 Withdrawn", f"Withdrew 🪙 **{amt:,}** to wallet.\n**Wallet:** {user['wallet']:,}"))

    # ── send/pay/give ──
    elif cmd in ("send", "pay", "give"):
        if not message.mentions or len(args) < 3:
            await message.channel.send(view=EconomyView("❌ Usage", "`.send @user <amount>`", discord.Color.red()))
            return
        try:
            amt = int(args[2])
        except ValueError:
            await message.channel.send(view=EconomyView("❌ Error", "Amount must be a number.", discord.Color.red()))
            return
        if amt <= 0 or amt > user["wallet"]:
            await message.channel.send(view=EconomyView("❌ Error", "Invalid amount or insufficient funds.", discord.Color.red()))
            return
        t_uid = str(message.mentions[0].id)
        if t_uid == uid:
            await message.channel.send(view=EconomyView("❌ Error", "You can't send coins to yourself.", discord.Color.red()))
            return
        t_user = get_user(eco, t_uid)
        user["wallet"] -= amt
        t_user["wallet"] += amt
        save_economy(eco)
        await message.channel.send(view=EconomyView("💸 Sent", f"Sent 🪙 **{amt:,}** to {message.mentions[0].mention}"))

    # ── steal/rob ──
    elif cmd in ("steal", "rob"):
        if not message.mentions:
            await message.channel.send(view=EconomyView("❌ Usage", "`.steal @user`", discord.Color.red()))
            return
        t_uid = str(message.mentions[0].id)
        if t_uid == uid:
            await message.channel.send(view=EconomyView("❌ Error", "Can't rob yourself.", discord.Color.red()))
            return
        t_user = get_user(eco, t_uid)
        if t_user["wallet"] <= 0:
            await message.channel.send(view=EconomyView("💀 Failed", f"{message.mentions[0].mention} has nothing to steal.", discord.Color.red()))
            return
        success = random.random() < 0.45
        if success:
            stolen = random.randint(1, max(1, t_user["wallet"] // 3))
            t_user["wallet"] -= stolen
            user["wallet"] += stolen
            save_economy(eco)
            await message.channel.send(view=EconomyView("🦹 Robbery Success!", f"You stole 🪙 **{stolen:,}** from {message.mentions[0].mention}!"))
        else:
            fine = random.randint(50, 200)
            user["wallet"] = max(0, user["wallet"] - fine)
            save_economy(eco)
            await message.channel.send(view=EconomyView("🚔 Caught!", f"You got caught and paid 🪙 **{fine:,}** fine.", discord.Color.red()))

    # ── work ──
    elif cmd == "work":
        cd = check_cooldown(user, "work")
        if cd:
            await message.channel.send(view=EconomyView("⏳ Cooldown", f"Work again in **{format_cd(cd)}**", discord.Color.orange()))
            return
        amount = random.randint(100, 350)
        user["wallet"] += amount
        user["last_work"] = now
        save_economy(eco)
        msg = random.choice(WORK_MSGS)
        await message.channel.send(view=EconomyView("💼 Work", f"{msg} and earned 🪙 **{amount:,}**!\n**Wallet:** {user['wallet']:,}"))

    # ── slut ──
    elif cmd == "slut":
        cd = check_cooldown(user, "slut")
        if cd:
            await message.channel.send(view=EconomyView("⏳ Cooldown", f"Try again in **{format_cd(cd)}**", discord.Color.orange()))
            return
        success = random.random() < 0.75
        if success:
            amount = random.randint(150, 400)
            user["wallet"] += amount
            msg = random.choice(SLUT_MSGS)
            await message.channel.send(view=EconomyView("💋 Slut", f"{msg} and earned 🪙 **{amount:,}**!\n**Wallet:** {user['wallet']:,}", discord.Color.magenta()))
        else:
            fine = random.randint(50, 150)
            user["wallet"] = max(0, user["wallet"] - fine)
            await message.channel.send(view=EconomyView("😬 Failed", f"You got rejected and lost 🪙 **{fine:,}**.", discord.Color.red()))
        user["last_slut"] = now
        save_economy(eco)

    # ── crime ──
    elif cmd == "crime":
        cd = check_cooldown(user, "crime")
        if cd:
            await message.channel.send(view=EconomyView("⏳ Cooldown", f"Try again in **{format_cd(cd)}**", discord.Color.orange()))
            return
        success = random.random() < 0.6
        if success:
            amount = random.randint(200, 600)
            user["wallet"] += amount
            msg = random.choice(CRIME_MSGS)
            await message.channel.send(view=EconomyView("🔫 Crime", f"{msg} and got 🪙 **{amount:,}**!", discord.Color.dark_red()))
        else:
            fine = random.randint(100, 300)
            user["wallet"] = max(0, user["wallet"] - fine)
            await message.channel.send(view=EconomyView("🚔 Arrested!", f"You got caught and fined 🪙 **{fine:,}**.", discord.Color.red()))
        user["last_crime"] = now
        save_economy(eco)

    # ── hunt ──
    elif cmd == "hunt":
        cd = check_cooldown(user, "hunt")
        if cd:
            await message.channel.send(view=EconomyView("⏳ Cooldown", f"Hunt again in **{format_cd(cd)}**", discord.Color.orange()))
            return
        amount = random.randint(80, 300)
        user["wallet"] += amount
        user["last_hunt"] = now
        save_economy(eco)
        msg = random.choice(HUNT_MSGS)
        await message.channel.send(view=EconomyView("🏹 Hunt", f"{msg} and earned 🪙 **{amount:,}**!", discord.Color.dark_green()))

    # ── search ──
    elif cmd == "search":
        cd = check_cooldown(user, "search")
        if cd:
            await message.channel.send(view=EconomyView("⏳ Cooldown", f"Search again in **{format_cd(cd)}**", discord.Color.orange()))
            return
        amount = random.randint(10, 100)
        user["wallet"] += amount
        user["last_search"] = now
        save_economy(eco)
        msg = random.choice(SEARCH_MSGS)
        await message.channel.send(view=EconomyView("🔍 Search", f"{msg} and found 🪙 **{amount:,}**!"))

    # ── beg ──
    elif cmd == "beg":
        cd = check_cooldown(user, "beg")
        if cd:
            await message.channel.send(view=EconomyView("⏳ Cooldown", f"Beg again in **{format_cd(cd)}**", discord.Color.orange()))
            return
        amount = random.randint(5, 75)
        user["wallet"] += amount
        user["last_beg"] = now
        save_economy(eco)
        msg = random.choice(BEG_MSGS)
        await message.channel.send(view=EconomyView("🙏 Beg", f"{msg} gave you 🪙 **{amount:,}**!"))

    # ── mine ──
    elif cmd == "mine":
        cd = check_cooldown(user, "mine")
        if cd:
            await message.channel.send(view=EconomyView("⏳ Cooldown", f"Mine again in **{format_cd(cd)}**", discord.Color.orange()))
            return
        amount = random.randint(100, 400)
        user["wallet"] += amount
        user["last_mine"] = now
        save_economy(eco)
        msg = random.choice(MINE_MSGS)
        await message.channel.send(view=EconomyView("⛏️ Mine", f"{msg} and earned 🪙 **{amount:,}**!", discord.Color.dark_grey()))

    # ── fish ──
    elif cmd == "fish":
        cd = check_cooldown(user, "fish")
        if cd:
            await message.channel.send(view=EconomyView("⏳ Cooldown", f"Fish again in **{format_cd(cd)}**", discord.Color.orange()))
            return
        amount = random.randint(60, 250)
        user["wallet"] += amount
        user["last_fish"] = now
        save_economy(eco)
        msg = random.choice(FISH_MSGS)
        await message.channel.send(view=EconomyView("🎣 Fish", f"{msg} worth 🪙 **{amount:,}**!", discord.Color.blue()))

    # ── dig ──
    elif cmd == "dig":
        cd = check_cooldown(user, "dig")
        if cd:
            await message.channel.send(view=EconomyView("⏳ Cooldown", f"Dig again in **{format_cd(cd)}**", discord.Color.orange()))
            return
        amount = random.randint(50, 300)
        user["wallet"] += amount
        user["last_dig"] = now
        save_economy(eco)
        msg = random.choice(DIG_MSGS)
        await message.channel.send(view=EconomyView("🪣 Dig", f"{msg} worth 🪙 **{amount:,}**!", discord.Color.dark_orange()))

    # ── gamble/bet ──
    elif cmd in ("gamble", "bet"):
        if len(args) < 2:
            await message.channel.send(view=EconomyView("❌ Usage", "`.gamble <amount|all>`", discord.Color.red()))
            return
        amt = user["wallet"] if args[1].lower() == "all" else int(args[1])
        amt = min(amt, user["wallet"])
        if amt <= 0:
            await message.channel.send(view=EconomyView("❌ Error", "Not enough coins.", discord.Color.red()))
            return
        win = random.random() < 0.45
        if win:
            user["wallet"] += amt
            await message.channel.send(view=EconomyView("🎰 Win!", f"You won 🪙 **{amt:,}**!\n**Wallet:** {user['wallet']:,}", discord.Color.green()))
        else:
            user["wallet"] -= amt
            await message.channel.send(view=EconomyView("🎰 Loss!", f"You lost 🪙 **{amt:,}**.\n**Wallet:** {user['wallet']:,}", discord.Color.red()))
        save_economy(eco)

    # ── coinflip/cf ──
    elif cmd in ("coinflip", "cf"):
        if len(args) < 3:
            await message.channel.send(view=EconomyView("❌ Usage", "`.cf <heads|tails> <amount|all>`", discord.Color.red()))
            return
        choice = args[1].lower()
        if choice not in ("heads", "tails"):
            await message.channel.send(view=EconomyView("❌ Error", "Choose `heads` or `tails`.", discord.Color.red()))
            return
        amt = user["wallet"] if args[2].lower() == "all" else int(args[2])
        amt = min(amt, user["wallet"])
        if amt <= 0:
            await message.channel.send(view=EconomyView("❌ Error", "Not enough coins.", discord.Color.red()))
            return
        result = random.choice(["heads", "tails"])
        won = choice == result
        if won:
            user["wallet"] += amt
            await message.channel.send(view=EconomyView(f"🪙 {result.title()}!", f"Correct! You won 🪙 **{amt:,}**!\n**Wallet:** {user['wallet']:,}", discord.Color.green()))
        else:
            user["wallet"] -= amt
            await message.channel.send(view=EconomyView(f"🪙 {result.title()}!", f"Wrong! You lost 🪙 **{amt:,}**.\n**Wallet:** {user['wallet']:,}", discord.Color.red()))
        save_economy(eco)

    # ── scatter ──
    elif cmd == "scatter":
        if len(args) < 2:
            await message.channel.send(view=EconomyView("❌ Usage", "`.scatter <amount|all>`", discord.Color.red()))
            return
        amt = user["wallet"] if args[1].lower() == "all" else int(args[1])
        amt = min(amt, user["wallet"])
        if amt <= 0:
            await message.channel.send(view=EconomyView("❌ Error", "Not enough coins.", discord.Color.red()))
            return
        multiplier = random.choice([0, 0, 0.5, 1.5, 2, 3])
        winnings = int(amt * multiplier)
        user["wallet"] = user["wallet"] - amt + winnings
        save_economy(eco)
        if winnings > amt:
            await message.channel.send(view=EconomyView("🎲 Scatter Win!", f"Multiplier: **{multiplier}x** — Won 🪙 **{winnings:,}**!\n**Wallet:** {user['wallet']:,}", discord.Color.green()))
        elif winnings == 0:
            await message.channel.send(view=EconomyView("🎲 Scatter Loss!", f"Multiplier: **0x** — Lost 🪙 **{amt:,}**.\n**Wallet:** {user['wallet']:,}", discord.Color.red()))
        else:
            await message.channel.send(view=EconomyView("🎲 Scatter", f"Multiplier: **{multiplier}x** — Got 🪙 **{winnings:,}**.\n**Wallet:** {user['wallet']:,}", discord.Color.orange()))

    # ── highlow/hl ──
    elif cmd in ("highlow", "hl"):
        if len(args) < 3:
            await message.channel.send(view=EconomyView("❌ Usage", "`.hl <high|low> <amount|all>`", discord.Color.red()))
            return
        guess = args[1].lower()
        if guess not in ("high", "low"):
            await message.channel.send(view=EconomyView("❌ Error", "Choose `high` or `low`.", discord.Color.red()))
            return
        amt = user["wallet"] if args[2].lower() == "all" else int(args[2])
        amt = min(amt, user["wallet"])
        if amt <= 0:
            await message.channel.send(view=EconomyView("❌ Error", "Not enough coins.", discord.Color.red()))
            return
        number = random.randint(1, 100)
        correct = (guess == "high" and number > 50) or (guess == "low" and number <= 50)
        if correct:
            user["wallet"] += amt
            await message.channel.send(view=EconomyView("📊 High/Low Win!", f"Number was **{number}** — Correct!\nWon 🪙 **{amt:,}**!\n**Wallet:** {user['wallet']:,}", discord.Color.green()))
        else:
            user["wallet"] -= amt
            await message.channel.send(view=EconomyView("📊 High/Low Loss!", f"Number was **{number}** — Wrong!\nLost 🪙 **{amt:,}**.\n**Wallet:** {user['wallet']:,}", discord.Color.red()))
        save_economy(eco)

    # ── shop inventory ──
    elif cmd == "shop":
        sub = args[1].lower() if len(args) > 1 else ""
        if sub == "list" or sub == "":
            await message.channel.send(view=ShopListView())
        elif sub == "inventory":
            await message.channel.send(view=InventoryView(message.author, user.get("inventory", [])))
        elif sub == "sell":
            inv = user.get("inventory", [])
            if not inv:
                await message.channel.send(view=EconomyView("❌ Error", "Your inventory is empty.", discord.Color.red()))
                return
            item = args[2].lower().replace(" ", "_") if len(args) > 2 else ""
            if item not in inv:
                await message.channel.send(view=EconomyView("❌ Error", f"You don't have `{item}` in your inventory.", discord.Color.red()))
                return
            sell_price = SHOP_ITEMS.get(item, {}).get("price", 100) // 2
            inv.remove(item)
            user["wallet"] += sell_price
            save_economy(eco)
            await message.channel.send(view=EconomyView("✅ Sold", f"Sold **{item.replace('_',' ').title()}** for 🪙 **{sell_price:,}**!\n**Wallet:** {user['wallet']:,}"))
        elif sub == "buy":
            item = args[2].lower().replace(" ", "_") if len(args) > 2 else ""
            if item not in SHOP_ITEMS:
                await message.channel.send(view=EconomyView("❌ Error", f"Item `{item}` not found in shop.", discord.Color.red()))
                return
            price = SHOP_ITEMS[item]["price"]
            if user["wallet"] < price:
                await message.channel.send(view=EconomyView("❌ Error", f"Not enough coins. Need 🪙 **{price:,}**.", discord.Color.red()))
                return
            user["wallet"] -= price
            if "inventory" not in user:
                user["inventory"] = []
            user["inventory"].append(item)
            save_economy(eco)
            await message.channel.send(view=EconomyView("✅ Purchased", f"Bought **{item.replace('_',' ').title()}** for 🪙 **{price:,}**!"))
        else:
            await message.channel.send(view=ShopListView())

    else:
        return False
    return True


# ─── EXTRA UTILITY HANDLERS ───────────────────────────────────────────────────

async def handle_extra_utility(message, cmd, args):

    # ── serverinfo ──
    if cmd == "serverinfo":
        guild = message.guild
        embed = discord.Embed(title=f"🏰 {guild.name}", color=discord.Color.blurple())
        embed.add_field(name="Owner", value=f"<@{guild.owner_id}>", inline=True)
        embed.add_field(name="Members", value=f"{guild.member_count:,}", inline=True)
        embed.add_field(name="Channels", value=str(len(guild.channels)), inline=True)
        embed.add_field(name="Roles", value=str(len(guild.roles)), inline=True)
        embed.add_field(name="Boosts", value=str(guild.premium_subscription_count), inline=True)
        embed.add_field(name="Created", value=f"<t:{int(guild.created_at.timestamp())}:R>", inline=True)
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        await message.channel.send(embed=embed)
        return True

    # ── stealemoji ──
    elif cmd == "stealemoji":
        if not message.author.guild_permissions.manage_emojis:
            await message.channel.send(view=EconomyView("❌ Denied", "You need Manage Emojis permission.", discord.Color.red()))
            return True
        # reply to emoji: get from reference
        if message.reference:
            ref = await message.channel.fetch_message(message.reference.message_id)
            name = args[1] if len(args) > 1 else None
            import re as _re
            emoji_pattern = _re.compile(r"<a?:(\w+):(\d+)>")
            matches = emoji_pattern.findall(ref.content)
            if not matches:
                await message.channel.send(view=EconomyView("❌ Error", "No custom emoji found in replied message.", discord.Color.red()))
                return True
            e_name, e_id = matches[0]
            use_name = name or e_name
            ext = "gif" if ref.content.startswith("<a:") else "png"
            url = f"https://cdn.discordapp.com/emojis/{e_id}.{ext}"
        elif len(args) >= 2:
            # stealemoji <emoji> [name] — emoji as text
            import re as _re
            emoji_pattern = _re.compile(r"<a?:(\w+):(\d+)>")
            matches = emoji_pattern.findall(args[1])
            if not matches:
                await message.channel.send(view=EconomyView("❌ Error", "Provide a custom emoji.", discord.Color.red()))
                return True
            e_name, e_id = matches[0]
            use_name = args[2] if len(args) > 2 else e_name
            anim = args[1].startswith("<a:")
            ext = "gif" if anim else "png"
            url = f"https://cdn.discordapp.com/emojis/{e_id}.{ext}"
        else:
            await message.channel.send(view=EconomyView("❌ Usage", "`.stealemoji <emoji> [name]` or reply to a message", discord.Color.red()))
            return True
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    img_data = await resp.read()
            new_emoji = await message.guild.create_custom_emoji(name=use_name, image=img_data)
            await message.channel.send(view=EconomyView("✅ Emoji Stolen", f"Added {new_emoji} as `:{use_name}:`"))
        except Exception as e:
            await message.channel.send(view=EconomyView("❌ Failed", str(e), discord.Color.red()))
        return True

    # ── addemoji ──
    elif cmd == "addemoji":
        if not message.author.guild_permissions.manage_emojis:
            await message.channel.send(view=EconomyView("❌ Denied", "You need Manage Emojis permission.", discord.Color.red()))
            return True
        if len(args) < 3:
            await message.channel.send(view=EconomyView("❌ Usage", "`.addemoji <name> <url>`", discord.Color.red()))
            return True
        name = args[1]
        url = args[2]
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    img_data = await resp.read()
            new_emoji = await message.guild.create_custom_emoji(name=name, image=img_data)
            await message.channel.send(view=EconomyView("✅ Emoji Added", f"Added {new_emoji} as `:{name}:`"))
        except Exception as e:
            await message.channel.send(view=EconomyView("❌ Failed", str(e), discord.Color.red()))
        return True

    # ── addsticker ──
    elif cmd == "addsticker":
        if not message.author.guild_permissions.manage_emojis:
            await message.channel.send(view=EconomyView("❌ Denied", "You need Manage Emojis permission.", discord.Color.red()))
            return True
        if not message.reference:
            await message.channel.send(view=EconomyView("❌ Usage", "Reply to a sticker message with `.addsticker <name> [description]`", discord.Color.red()))
            return True
        ref = await message.channel.fetch_message(message.reference.message_id)
        if not ref.stickers:
            await message.channel.send(view=EconomyView("❌ Error", "No sticker found in replied message.", discord.Color.red()))
            return True
        sticker = ref.stickers[0]
        name = args[1] if len(args) > 1 else sticker.name
        description = " ".join(args[2:]) if len(args) > 2 else "Stolen sticker"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(str(sticker.url)) as resp:
                    sticker_data = await resp.read()
            file = discord.File(fp=__import__("io").BytesIO(sticker_data), filename=f"{name}.png")
            await message.guild.create_sticker(name=name, description=description, emoji="⭐", file=file)
            await message.channel.send(view=EconomyView("✅ Sticker Added", f"Added sticker **{name}** to the server!"))
        except Exception as e:
            await message.channel.send(view=EconomyView("❌ Failed", str(e), discord.Color.red()))
        return True

    return False

