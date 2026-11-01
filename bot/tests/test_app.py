from types import SimpleNamespace

import pytest

from bot.app import UserCooldown, create_bot
from bot.config import Config
from bot.scanner import MediaSource


class FakeResponse:
    def __init__(self):
        self.deferred = None
        self.sent = []

    def is_done(self):
        return self.deferred is not None or bool(self.sent)

    async def defer(self, **kwargs):
        self.deferred = kwargs

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)


class FakeInteraction:
    def __init__(self):
        self.user = SimpleNamespace(id=123)
        self.guild_id = 987654321
        self.response = FakeResponse()
        self.followup = FakeFollowup()


class FakeScanner:
    def __init__(self, report):
        self.report = report
        self.sources = []

    async def scan(self, source):
        self.sources.append(source)
        return self.report

    async def close(self):
        pass


def test_per_user_cooldown_is_five_scans_per_minute():
    cooldown = UserCooldown()
    assert all(cooldown.allow(1, now=0) for _ in range(5))
    assert not cooldown.allow(1, now=1)
    assert cooldown.allow(2, now=1)
    assert cooldown.allow(1, now=60)


@pytest.mark.asyncio
async def test_fake_interaction_receives_ephemeral_scan_result():
    report = {
        "verdict": "unknown",
        "verdict_title": "Nothing conclusive",
        "verdict_detail": "No strong indicators were found.",
        "signals": [],
    }
    scanner = FakeScanner(report)
    bot = create_bot(Config(token="secret", server_url="https://scan.example.org"), scanner=scanner)
    interaction = FakeInteraction()
    source = MediaSource("https://cdn.discordapp.com/attachments/1/2/file.png", "image/png", 10)

    await bot._run_scan(interaction, source, "scan")

    assert scanner.sources == [source]
    assert interaction.response.deferred == {"ephemeral": True, "thinking": True}
    assert interaction.followup.sent[0]["ephemeral"] is True
    assert interaction.followup.sent[0]["embed"].title == "Nothing conclusive"
    await bot.close()
