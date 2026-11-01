from __future__ import annotations

from typing import Any

import discord

NO_PROOF = "No tool can prove a file is real."

VERDICT_CAVEATS = {
    "verified_capture": (
        "This verifies the file's signed origin, not the depicted scene: a camera can photograph a screen or "
        "a staged scene."
    ),
    "verified_ai": "Trusted credentials declare generative AI; they do not say which parts are synthetic.",
    "ai": "Markers can be faked and detectors can be wrong. Check where the file came from.",
    "edited": "Editing can be harmless, and this does not mean the original scene was real.",
    "camera": "Camera metadata can be copied, edited or faked. It does not verify that the file or scene is real.",
    "unknown": "Finding nothing is not evidence that the file is real; sharing often removes metadata.",
}

VERDICT_COLOURS = {
    "verified_capture": 0x5865F2,
    "verified_ai": 0xE67E22,
    "ai": 0xE74C3C,
    "edited": 0xF1C40F,
    "camera": 0x95A5A6,
    "unknown": 0x95A5A6,
}

SIGNAL_LABELS = {
    "ai": "AI",
    "edit": "EDITED",
    "camera": "CAMERA",
    "c2pa": "C2PA",
    "model": "MODEL",
    "info": "INFO",
}


def _plain(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    escaped = discord.utils.escape_markdown(value.strip())
    return escaped if len(escaped) <= limit else f"{escaped[: limit - 1]}…"


def _detail_without_no_proof(value: Any) -> str:
    detail = _plain(value, 1800)
    suffix = discord.utils.escape_markdown(NO_PROOF)
    if detail.endswith(suffix):
        detail = detail[: -len(suffix)].rstrip()
    return detail


def format_report(report: dict[str, Any], server_url: str) -> discord.Embed:
    verdict = report.get("verdict") if report.get("verdict") in VERDICT_CAVEATS else "unknown"
    title = _plain(report.get("verdict_title"), 256) or "BanTheFake scan result"
    detail = _detail_without_no_proof(report.get("verdict_detail"))
    caveat = VERDICT_CAVEATS[verdict]
    description = "\n\n".join(part for part in (detail, caveat, f"**{NO_PROOF}**") if part)
    embed = discord.Embed(title=title, description=description[:4096], colour=VERDICT_COLOURS[verdict])

    raw_signals = report.get("signals")
    signals = raw_signals if isinstance(raw_signals, list) else []
    lines: list[str] = []
    shown_limit = 7 if len(signals) > 8 else 8
    for signal in signals[:shown_limit]:
        if not isinstance(signal, dict) or not isinstance(signal.get("text"), str):
            continue
        label = SIGNAL_LABELS.get(signal.get("kind"), "INFO")
        text = _plain(signal["text"], 100)
        if text:
            lines.append(f"**{label}:** {text}")
    if len(signals) > shown_limit:
        lines.append(f"*{len(signals) - shown_limit} more signal(s) are in the full report.*")
    if lines:
        embed.add_field(name="Main signals", value="\n".join(lines)[:1024], inline=False)
    embed.add_field(name="Get help or report abuse", value=f"[Open BanTheFake resources]({server_url}/#report)", inline=False)
    embed.set_footer(text="Results are indicators, not proof. The bot does not store the file or result.")
    return embed


HELP_RESOURCES = (
    (
        "Report Remove",
        "https://www.childline.org.uk/info-advice/bullying-abuse-safety/online-mobile-safety/report-remove/",
        "UK under-18s: confidential help removing a nude image or video.",
    ),
    ("Childline", "https://www.childline.org.uk/get-support/", "Free, confidential UK support for anyone under 19."),
    ("StopNCII", "https://stopncii.org/", "Adults can create an on-device fingerprint to help block intimate images."),
    ("CEOP", "https://www.ceop.police.uk/Safety-Centre/", "Report online sexual abuse or grooming of a child in the UK."),
    (
        "Take It Down",
        "https://takeitdown.ncmec.org/",
        "Free, anonymous help limiting images taken when the person was under 18, including synthetic images.",
    ),
)


def format_help(server_url: str) -> discord.Embed:
    embed = discord.Embed(
        title="BanTheFake help",
        description=(
            "Use **/scan** or **Apps → Check with BanTheFake** on a message. The bot privately sends one "
            "Discord attachment to your configured BanTheFake server and returns its indicators. It does not "
            "run detectors itself, prove a scene is real, or store files or results."
        ),
        colour=0x5865F2,
    )
    for name, url, description in HELP_RESOURCES:
        embed.add_field(name=name, value=f"{description} [Open resource]({url})", inline=False)
    embed.add_field(name="More reporting help", value=f"[Open the BanTheFake report section]({server_url}/#report)", inline=False)
    return embed
