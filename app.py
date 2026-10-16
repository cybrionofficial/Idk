"""Upload .lua/.txt Discord slash-command attachments to Pastefy.

Set the configuration constants below, then run: python bot.py.
Keep this file private: it contains your API credentials.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import aiohttp
import discord
from discord import app_commands

LOG = logging.getLogger("pastefy_bot")
PASTEFY_ENDPOINT = "https://pastefy.app/api/v2/paste"
MAX_FILE_BYTES = 2_000_000  # 2 MB, decimal; exactly this size is allowed.
ALLOWED_EXTENSIONS = {".lua", ".txt"}
MAX_TITLE_LENGTH = 100

# --------------------------- Configuration ---------------------------
# Replace the placeholder values before running the bot. Do not commit or
# share this file after adding real tokens.
DISCORD_TOKEN = "MTU2MDcwNDY4MDM4MjY5NzU4NQ.GBC3PR.UCus3jz0XLHo1hJGHH-9SUI8y2TUiuUEacMbAg"
PASTEFY_API_KEY = "JpLGHgtcaRLmLzgg2gybMWbw5PH8GYbVNG8OyFLXxXxnGmfFUw3OwlqHllLK"
# Leave blank to register globally. Set to one server ID for faster testing.
DISCORD_GUILD_ID = "1538618066970349629"
# Optional: comma-separated Discord user IDs allowed to use /paste.
# Leave blank to let any member of the configured server use it.
ALLOWED_USER_IDS = "1099104183837266044"
# ---------------------------------------------------------------------


class UploadError(Exception):
    """A safe, user-facing error message; never include tokens or response bodies."""


@dataclass(frozen=True)
class Config:
    discord_token: str = field(repr=False)
    pastefy_api_key: str = field(repr=False)
    guild_id: int | None = None
    allowed_user_ids: frozenset[int] = frozenset()

    @classmethod
    def from_embedded_config(cls) -> Config:
        def secret(value: str, name: str) -> str:
            value = value.strip()
            if not value or value.startswith("replace_with_"):
                raise ValueError(f"Set {name} near the top of bot.py.")
            if any(char.isspace() for char in value):
                raise ValueError(f"{name} must not contain whitespace.")
            return value

        def snowflake(value: str, name: str) -> int:
            if not value.isascii() or not value.isdecimal() or int(value) <= 0:
                raise ValueError(f"{name} must contain positive numeric Discord IDs.")
            return int(value)

        guild = DISCORD_GUILD_ID.strip()
        users = ALLOWED_USER_IDS.strip()
        return cls(
            discord_token=secret(DISCORD_TOKEN, "DISCORD_TOKEN"),
            pastefy_api_key=secret(PASTEFY_API_KEY, "PASTEFY_API_KEY"),
            guild_id=snowflake(guild, "DISCORD_GUILD_ID") if guild else None,
            allowed_user_ids=frozenset(
                snowflake(item.strip(), "ALLOWED_USER_IDS")
                for item in users.split(",") if item.strip()
            ),
        )


def validate_attachment(filename: str, size: int) -> None:
    if Path(filename).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise UploadError("Only .lua and .txt files are allowed.")
    if size <= 0:
        raise UploadError("The file is empty.")
    if size > MAX_FILE_BYTES:
        raise UploadError("The file exceeds the 2 MB limit (2,000,000 bytes).")


def decode_text(data: bytes) -> str:
    # Check the actual download too, not just Discord's size metadata.
    if len(data) > MAX_FILE_BYTES:
        raise UploadError("The file exceeds the 2 MB limit (2,000,000 bytes).")
    try:
        text = data.decode("utf-8-sig")  # Accept UTF-8 with or without a BOM.
    except UnicodeDecodeError:
        raise UploadError("Save the file as UTF-8 text and try again.") from None
    if not text:
        raise UploadError("The file is empty.")
    if "\x00" in text:
        raise UploadError("The file contains binary/NUL data. Upload a text file.")
    return text


def choose_title(filename: str, title: str | None) -> str:
    if title and title.strip():
        result = title.strip()
        if len(result) > MAX_TITLE_LENGTH:
            raise UploadError("The title must be 100 characters or fewer.")
    else:
        result = filename[:MAX_TITLE_LENGTH]
    if "\x00" in result:
        raise UploadError("The title contains an invalid character.")
    return result


@dataclass(frozen=True)
class PasteLinks:
    url: str
    raw_url: str


class PastefyAPI:
    def __init__(self, session: aiohttp.ClientSession, api_key: str) -> None:
        self.session = session
        self.api_key = api_key

    async def create_paste(self, title: str, content: str) -> PasteLinks:
        payload = {
            "title": title,
            "content": content,
            "type": "PASTE",
            "visibility": "UNLISTED",
            "encrypted": False,
        }
        try:
            # Auth is scoped to this request, never a shared session default.
            # Do not follow redirects with an API token and uploaded content.
            async with self.session.post(
                PASTEFY_ENDPOINT,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                allow_redirects=False,
            ) as response:
                if response.status in (401, 403):
                    raise UploadError(
                        "Pastefy denied access. Ask the bot owner to check "
                        "the API key and its permissions."
                    )
                if response.status == 413:
                    raise UploadError(
                        "Pastefy rejected the upload as too large. Its service "
                        "limit may be lower than the bot's 2 MB cap."
                    )
                if response.status == 429:
                    raise UploadError("Pastefy is rate-limiting requests. Try again later.")
                if not 200 <= response.status < 300:
                    raise UploadError(
                        f"Pastefy returned HTTP {response.status}. "
                        "Ask the bot owner to check the service before retrying."
                    )
                try:
                    result = await response.json(content_type=None)
                except ValueError:
                    raise UploadError(
                        "Pastefy returned an unreadable response. "
                        "Check your Pastefy account before retrying."
                    ) from None
        except (asyncio.TimeoutError, aiohttp.ClientError):
            # Never retry a create request automatically: it could make duplicates.
            raise UploadError(
                "Could not confirm the upload. A paste may already have been "
                "created; check your Pastefy account before retrying."
            ) from None

        paste = result.get("paste") if isinstance(result, dict) else None
        paste_id = paste.get("id") if isinstance(paste, dict) else None
        failed = not isinstance(result, dict) or result.get("success") is False
        if (
            failed
            or not isinstance(paste_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", paste_id)
        ):
            raise UploadError(
                "Pastefy did not return a valid paste ID. "
                "Check your Pastefy account before retrying."
            )
        url = f"https://pastefy.app/{paste_id}"
        return PasteLinks(url=url, raw_url=f"{url}/raw")


class PastefyBot(discord.Client):
    def __init__(self, config: Config) -> None:
        super().__init__(
            intents=discord.Intents.default(),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.config = config
        self.session: aiohttp.ClientSession | None = None
        self.api: PastefyAPI | None = None
        self.upload_slots = asyncio.Semaphore(3)
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(paste)
        self.tree.on_error = self.on_command_error

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30, connect=10)
        )
        self.api = PastefyAPI(self.session, self.config.pastefy_api_key)
        if self.config.guild_id:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            commands = await self.tree.sync(guild=guild)
        else:
            commands = await self.tree.sync()
        LOG.info("Registered %d slash command(s).", len(commands))

    async def on_ready(self) -> None:
        LOG.info("Connected as %s. Use /paste in Discord.", self.user)

    async def close(self) -> None:
        try:
            if self.session is not None:
                await self.session.close()
        finally:
            await super().close()

    async def upload_attachment(
        self, file: discord.Attachment, title: str
    ) -> tuple[PasteLinks, int]:
        try:
            await asyncio.wait_for(self.upload_slots.acquire(), timeout=5)
        except asyncio.TimeoutError:
            raise UploadError("The bot is busy. Try again in a few seconds.") from None
        try:
            try:
                data = await asyncio.wait_for(file.read(), timeout=20)
            except asyncio.TimeoutError:
                raise UploadError("Downloading the attachment timed out. Try again.") from None
            except discord.HTTPException:
                raise UploadError(
                    "Could not download the attachment. Attach the file again."
                ) from None
            content = decode_text(data)
            if self.api is None:
                raise UploadError("The bot is not ready yet. Try again shortly.")
            links = await self.api.create_paste(title, content)
            return links, len(data)
        finally:
            self.upload_slots.release()

    async def on_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            message = f"Wait {math.ceil(error.retry_after)} seconds before uploading again."
        elif isinstance(error, app_commands.CheckFailure):
            message = "You cannot use this command here."
        else:
            # Do not print exception bodies: they might contain sensitive input.
            original = getattr(error, "original", error)
            LOG.error("Unexpected command error: %s", type(original).__name__)
            message = "An unexpected error occurred. Ask the bot owner to check the bot."
        if interaction.response.is_done():
            await interaction.edit_original_response(content=message)
        else:
            await interaction.response.send_message(message, ephemeral=True)


@app_commands.command(name="paste", description="Upload a .lua or .txt file (maximum 2 MB) to Pastefy.")
@app_commands.describe(
    file="The UTF-8 .lua or .txt attachment to upload (maximum 2 MB).",
    title="Optional paste title, up to 100 characters; defaults to the filename.",
    public="Show the result to the channel instead of only to you (default: false).",
)
@app_commands.guild_only()
@app_commands.checks.cooldown(1, 10.0, key=lambda interaction: interaction.user.id)
async def paste(
    interaction: discord.Interaction,
    file: discord.Attachment,
    title: str | None = None,
    public: bool = False,
) -> None:
    bot = cast(PastefyBot, interaction.client)
    if bot.config.guild_id and interaction.guild_id != bot.config.guild_id:
        await interaction.response.send_message(
            "This bot is restricted to its configured server.", ephemeral=True
        )
        return
    if bot.config.allowed_user_ids and interaction.user.id not in bot.config.allowed_user_ids:
        await interaction.response.send_message(
            "You are not on this bot's upload allowlist.", ephemeral=True
        )
        return
    try:
        validate_attachment(file.filename, file.size)
        paste_title = choose_title(file.filename, title)
    except UploadError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    # Acknowledge promptly before any network I/O to avoid interaction expiry.
    await interaction.response.defer(ephemeral=not public, thinking=True)
    try:
        links, size = await bot.upload_attachment(file, paste_title)
    except UploadError as exc:
        await interaction.edit_original_response(content=str(exc))
        return

    filename = discord.utils.escape_markdown(file.filename[:100])
    await interaction.edit_original_response(
        content=(
            f"Uploaded **{filename}** ({size:,} bytes).\n"
            f"Paste: <{links.url}>\n"
            f"Raw text: <{links.raw_url}>\n"
            f"Loader: `loadstring(game:HttpGet(\"{links.raw_url}\"))()`\n"
            "Unlisted, not private: anyone with the link can read it."
        ),
        allowed_mentions=discord.AllowedMentions.none(),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        config = Config.from_embedded_config()
    except ValueError as exc:
        raise SystemExit(f"Configuration error: {exc}") from None
    try:
        PastefyBot(config).run(config.discord_token, log_handler=None)
    except discord.LoginFailure:
        raise SystemExit("Discord rejected DISCORD_TOKEN. Check your bot token.") from None


if __name__ == "__main__":
    main()
