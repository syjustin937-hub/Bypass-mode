import discord
import aiohttp
import asyncio
import urllib.parse
import json
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
CONFIG_FILE = "config.json"
API_URL = "https://doitenroi.win/api/bypass?url="

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


config = load_config()

intents = discord.Intents.default()
intents.message_content = True

bot = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(bot)


class CopyButton(discord.ui.Button):
    def __init__(self, result):
        super().__init__(
            label="Mobile Copy",
            style=discord.ButtonStyle.secondary
        )
        self.result = result

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            self.result,
            ephemeral=True
        )


class ProcessingView(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)

        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# Processing..."),
                discord.ui.Separator(),
                discord.ui.TextDisplay("Please wait while I bypass the link.")
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
                discord.ui.ActionRow(
                    CopyButton(result)
                )
            )
        )


class FailedView(discord.ui.LayoutView):
    def __init__(self, message):
        super().__init__(timeout=None)

        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# Failed!"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(message)
            )
        )


class PingView(discord.ui.LayoutView):
    def __init__(self, latency):
        super().__init__(timeout=None)

        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# Pong!"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(f"Latency: **{latency}ms**")
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
                )
            )
        )


@tree.command(
    name="ping",
    description="Check bot latency",
    guild=discord.Object(id=GUILD_ID)
)
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(
        view=PingView(latency)
    )


@tree.command(
    name="bypass",
    description="Bypass a link",
    guild=discord.Object(id=GUILD_ID)
)
@discord.app_commands.describe(link="The link to bypass")
async def bypass(interaction: discord.Interaction, link: str):
    await interaction.response.send_message(
        view=ProcessingView()
    )

    try:
        encoded = urllib.parse.quote(link, safe="")
        timeout = aiohttp.ClientTimeout(total=120)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(API_URL + encoded) as res:
                data = await res.json(content_type=None)
                status = data.get("status")

                if status == "success":
                    result = data.get("result")
                    await interaction.edit_original_response(
                        view=SuccessView(result)
                    )

                elif status in ("failed", "error"):
                    await interaction.edit_original_response(
                        view=FailedView(
                            data.get("message", "Failed to bypass")
                        )
                    )

                else:
                    await interaction.edit_original_response(
                        view=FailedView("Invalid API response.")
                    )

    except asyncio.TimeoutError:
        await interaction.edit_original_response(
            view=FailedView("API request timed out.")
        )

    except Exception as e:
        print("Error:", e)
        await interaction.edit_original_response(
            view=FailedView(str(e))
        )


@tree.command(
    name="autobypass",
    description="Setup auto bypass channel",
    guild=discord.Object(id=GUILD_ID)
)
@discord.app_commands.describe(channel="Channel to watch for links")
@discord.app_commands.default_permissions(administrator=True)
async def autobypass(interaction: discord.Interaction, channel: discord.TextChannel):
    config["channel_id"] = channel.id
    save_config(config)

    await interaction.response.send_message(
        view=SetupView(channel)
    )


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    channel_id = config.get("channel_id")

    if not channel_id:
        return

    if (
        message.guild
        and message.guild.id == GUILD_ID
        and message.channel.id == channel_id
    ):
        urls = []
        for word in message.content.split():
            if word.startswith("http://") or word.startswith("https://"):
                urls.append(word)

        for url in urls:
            msg = await message.reply(view=ProcessingView())

            try:
                encoded = urllib.parse.quote(url, safe="")
                timeout = aiohttp.ClientTimeout(total=120)

                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(API_URL + encoded) as res:
                        data = await res.json(content_type=None)
                        status = data.get("status")

                        if status == "success":
                            result = data.get("result")
                            await msg.edit(view=SuccessView(result))

                        elif status in ("failed", "error"):
                            await msg.edit(
                                view=FailedView(
                                    data.get("message", "Failed to bypass")
                                )
                            )

                        else:
                            await msg.edit(view=FailedView("Invalid API response."))

            except asyncio.TimeoutError:
                await msg.edit(view=FailedView("API request timed out."))

            except Exception as e:
                print("Error:", e)
                await msg.edit(view=FailedView(str(e)))


@bot.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print(f"Logged in as {bot.user}")
    channel_id = config.get("channel_id")
    if channel_id:
        print(f"Auto bypass channel: {channel_id}")
    else:
        print("No auto bypass channel set. Use /autobypass to configure.")


bot.run(TOKEN)
