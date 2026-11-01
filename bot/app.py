from __future__ import annotations

import hashlib
import logging
import os
import time
from collections import defaultdict, deque
from pathlib import PurePosixPath
from typing import Any

import discord
from discord import app_commands

from .config import Config
from .formatting import format_help, format_report
from .scanner import MediaSource, ScanFailure, Scanner, is_discord_attachment_url

LOGGER = logging.getLogger("banthefake.discord")
MEDIA_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif", ".mp4", ".mov", ".webm"})


def default_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.message_content = False
    intents.members = False
    intents.presences = False
    return intents


class UserCooldown:
    def __init__(self, limit: int = 5, period: float = 60.0) -> None:
        self.limit = limit
        self.period = period
        self._uses: dict[int, deque[float]] = defaultdict(deque)

    def allow(self, user_id: int, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        uses = self._uses[user_id]
        while uses and current - uses[0] >= self.period:
            uses.popleft()
        if len(uses) >= self.limit:
            return False
        uses.append(current)
        return True


def _is_media(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()
    if content_type.startswith(("image/", "video/")):
        return True
    return PurePosixPath(attachment.filename).suffix.lower() in MEDIA_EXTENSIONS


def source_from_attachment(attachment: discord.Attachment) -> MediaSource | None:
    if not _is_media(attachment):
        return None
    return MediaSource(url=attachment.url, content_type=attachment.content_type, declared_size=attachment.size)


def source_from_message(message: discord.Message) -> MediaSource | None:
    for attachment in message.attachments:
        source = source_from_attachment(attachment)
        if source:
            return source
    for embed in message.embeds:
        if embed.image and embed.image.url and is_discord_attachment_url(embed.image.url):
            return MediaSource(url=embed.image.url, content_type="image")
    return None


class BanTheFakeBot(discord.Client):
    def __init__(self, config: Config, *, scanner: Scanner | None = None) -> None:
        super().__init__(intents=default_intents(), allowed_mentions=discord.AllowedMentions.none())
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.scanner = scanner or Scanner(config.server_url, config.max_upload_bytes)
        self.cooldown = UserCooldown()
        self._log_salt = os.urandom(16)
        self._install_commands()

    async def setup_hook(self) -> None:
        if self.config.guild_id:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def close(self) -> None:
        await self.scanner.close()
        await super().close()

    def _guild_hash(self, guild_id: int | None) -> str:
        if guild_id is None:
            return "dm"
        digest = hashlib.sha256(self._log_salt + str(guild_id).encode("ascii")).hexdigest()
        return digest[:12]

    def _log(self, command: str, interaction: discord.Interaction[Any], status: str) -> None:
        LOGGER.info("command=%s guild=%s status=%s", command, self._guild_hash(interaction.guild_id), status)

    async def _send_error(self, interaction: discord.Interaction[Any], failure: ScanFailure) -> None:
        embed = discord.Embed(title=failure.title, description=failure.detail, colour=0xE74C3C)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=self.config.ephemeral)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=self.config.ephemeral)

    async def _run_scan(self, interaction: discord.Interaction[Any], source: MediaSource | None, command: str) -> None:
        if source is None:
            failure = ScanFailure(
                "No supported attachment found",
                "Attach an image or video. For message embeds, only Discord CDN attachment images are accepted. Nothing was sent.",
                status="no_media",
            )
            await self._send_error(interaction, failure)
            self._log(command, interaction, failure.status)
            return
        if not self.cooldown.allow(interaction.user.id):
            failure = ScanFailure(
                "Please slow down",
                "You can request up to five scans per minute. Nothing was sent; try again shortly.",
                status="user_cooldown",
            )
            await self._send_error(interaction, failure)
            self._log(command, interaction, failure.status)
            return

        await interaction.response.defer(ephemeral=self.config.ephemeral, thinking=True)
        try:
            report = await self.scanner.scan(source)
        except ScanFailure as failure:
            await self._send_error(interaction, failure)
            self._log(command, interaction, failure.status)
            return
        except Exception:
            failure = ScanFailure(
                "Scan failed",
                "Something went wrong while handling the scan. The file may have been sent; please try again.",
                status="internal_error",
            )
            await self._send_error(interaction, failure)
            self._log(command, interaction, failure.status)
            return
        await interaction.followup.send(embed=format_report(report, self.config.server_url), ephemeral=self.config.ephemeral)
        self._log(command, interaction, "ok")

    def _install_commands(self) -> None:
        @self.tree.command(name="scan", description="Check an image or video with BanTheFake")
        @app_commands.describe(file="The image or video to check")
        async def scan_command(interaction: discord.Interaction[Any], file: discord.Attachment) -> None:
            await self._run_scan(interaction, source_from_attachment(file), "scan")

        @self.tree.command(name="help", description="Learn what the bot does and find reporting support")
        async def help_command(interaction: discord.Interaction[Any]) -> None:
            await interaction.response.send_message(embed=format_help(self.config.server_url), ephemeral=self.config.ephemeral)
            self._log("help", interaction, "ok")

        @self.tree.context_menu(name="Check with BanTheFake")
        async def check_message(interaction: discord.Interaction[Any], message: discord.Message) -> None:
            await self._run_scan(interaction, source_from_message(message), "message_check")


def create_bot(config: Config, *, scanner: Scanner | None = None) -> BanTheFakeBot:
    return BanTheFakeBot(config, scanner=scanner)
