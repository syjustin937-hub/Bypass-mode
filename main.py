import discord
from discord.ext import commands
import aiohttp
import asyncio
import urllib.parse
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))

API_URL = "https://doitenroi.win/api/bypass?url="

if not TOKEN:
    raise ValueError("Missing TOKEN in .env")

intents = discord.Intents.default()

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


@bot.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print(f"Logged in as {bot.user}")


bot.run(TOKEN)
