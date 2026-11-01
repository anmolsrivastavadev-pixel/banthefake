#!/usr/bin/env python3
"""Generate BanTheFake street-campaign print materials.

All measurements are in PDF points (72 points per inch). QR code sizes include
their four-module quiet zones.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from urllib.parse import urlparse

import segno
from reportlab.lib.colors import Color, HexColor, white
from reportlab.lib.pagesizes import A4, A6
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"

RED = HexColor("#d42a2a")
BLACK = HexColor("#121212")
MID = HexColor("#505050")
LIGHT = HexColor("#eeeeee")
CUT = HexColor("#b8b8b8")

FONT = "Helvetica"
BOLD = "Helvetica-Bold"


def check_line(text: str, font: str, size: float, max_width: float, label: str) -> None:
    width = stringWidth(text, font, size)
    if width > max_width + 0.05:
        raise ValueError(
            f"Text overflow in {label}: {width:.2f} pt > {max_width:.2f} pt: {text!r}"
        )


def wrap(text: str, font: str, size: float, max_width: float) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if stringWidth(trial, font, size) <= max_width:
            current = trial
        else:
            if not current:
                raise ValueError(f"Single word does not fit: {word!r}")
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    for line in lines:
        check_line(line, font, size, max_width, "wrapped paragraph")
    return lines


def draw_lines(
    c: canvas.Canvas,
    lines: list[str],
    x: float,
    y: float,
    *,
    font: str,
    size: float,
    leading: float,
    color=BLACK,
    max_width: float,
    label: str,
) -> float:
    c.setFont(font, size)
    c.setFillColor(color)
    for line in lines:
        check_line(line, font, size, max_width, label)
        c.drawString(x, y, line)
        y -= leading
    return y


def fitted_size(text: str, font: str, preferred: float, minimum: float, max_width: float) -> float:
    size = preferred
    while size > minimum and stringWidth(text, font, size) > max_width:
        size -= 0.25
    check_line(text, font, size, max_width, "fitted line")
    return size


def make_qr(url: str, error: str = "q"):
    # boost_error=False guarantees that the requested correction level remains Q.
    return segno.make(url, error=error, boost_error=False, micro=False)


def draw_qr(c: canvas.Canvas, url: str, x: float, y: float, size: float) -> None:
    """Draw a vector QR with a four-module quiet zone inside the given square."""
    qr = make_qr(url, "q")
    matrix = tuple(tuple(int(bit) for bit in row) for row in qr.matrix)
    border = 4
    total_modules = len(matrix) + border * 2
    module = size / total_modules

    c.setFillColor(white)
    c.rect(x, y, size, size, fill=1, stroke=0)
    c.setFillColor(BLACK)
    path = c.beginPath()
    for row_index, row in enumerate(matrix):
        py = y + size - (border + row_index + 1) * module
        for col_index, dark in enumerate(row):
            if dark:
                px = x + (border + col_index) * module
                path.rect(px, py, module, module)
    c.drawPath(path, fill=1, stroke=0)


def draw_brand(c: canvas.Canvas, x: float, y: float, size: float = 10) -> None:
    c.setFillColor(RED)
    c.circle(x + 5, y + 4, 4, fill=0, stroke=1)
    c.setLineWidth(1.3)
    c.line(x + 1.7, y + 0.7, x + 8.3, y + 7.3)
    c.setFillColor(BLACK)
    c.setFont(BOLD, size)
    c.drawString(x + 14, y, "BanTheFake")


def build_sign(url: str) -> Path:
    path = OUT / "sign_A4.pdf"
    width, height = A4
    c = canvas.Canvas(str(path), pagesize=A4, pageCompression=1)
    c.setTitle("BanTheFake A4 campaign sign")

    c.setFillColor(white)
    c.rect(0, 0, width, height, fill=1, stroke=0)
    c.setFillColor(RED)
    c.rect(0, height - 10 * mm, width, 10 * mm, fill=1, stroke=0)

    margin = 16 * mm
    usable = width - 2 * margin
    y = height - 25 * mm
    y = draw_lines(
        c,
        ["Your face. Your voice."],
        margin,
        y,
        font=BOLD,
        size=39,
        leading=45,
        color=BLACK,
        max_width=usable,
        label="A4 headline line 1",
    )
    y = draw_lines(
        c,
        ["Your consent."],
        margin,
        y,
        font=BOLD,
        size=43,
        leading=51,
        color=RED,
        max_width=usable,
        label="A4 headline line 2",
    )
    y -= 3
    y = draw_lines(
        c,
        ["Sign to stop harmful deepfakes"],
        margin,
        y,
        font=BOLD,
        size=22,
        leading=32,
        color=BLACK,
        max_width=usable,
        label="A4 subline",
    )

    bullets = [
        "Ban non-consensual sexual and defamatory deepfakes",
        "Require takedowns within 48 hours",
        "Label AI-generated media clearly",
    ]
    c.setFont(FONT, 14.5)
    for bullet in bullets:
        c.setFillColor(RED)
        c.circle(margin + 4, y + 4, 3.2, fill=1, stroke=0)
        lines = wrap(bullet, BOLD, 14.5, usable - 18)
        y = draw_lines(
            c,
            lines,
            margin + 17,
            y,
            font=BOLD,
            size=14.5,
            leading=20,
            color=BLACK,
            max_width=usable - 18,
            label="A4 demand bullet",
        )
        y -= 4

    qr_size = 96 * mm
    qr_x = (width - qr_size) / 2
    qr_y = 57 * mm
    draw_qr(c, url, qr_x, qr_y, qr_size)

    c.setFillColor(BLACK)
    c.setFont(BOLD, 18)
    label = "Scan to sign - 20 seconds"
    check_line(label, BOLD, 18, usable, "A4 QR label")
    c.drawCentredString(width / 2, qr_y - 10 * mm, label)

    url_size = fitted_size(url, FONT, 10.5, 7.5, usable)
    c.setFont(FONT, url_size)
    c.setFillColor(MID)
    c.drawCentredString(width / 2, qr_y - 18 * mm, url)

    c.setStrokeColor(LIGHT)
    c.setLineWidth(0.8)
    c.line(margin, 17 * mm, width - margin, 17 * mm)
    c.setFont(FONT, 9.5)
    c.setFillColor(MID)
    c.drawCentredString(width / 2, 10.5 * mm, "BanTheFake - a student-led campaign")

    c.showPage()
    c.save()
    return path


def draw_flyer(c: canvas.Canvas, x0: float, y0: float, width: float, height: float, url: str) -> None:
    pad = 6 * mm
    usable = width - 2 * pad
    c.setFillColor(white)
    c.rect(x0, y0, width, height, fill=1, stroke=0)
    c.setFillColor(RED)
    c.rect(x0, y0 + height - 3 * mm, width, 3 * mm, fill=1, stroke=0)
    draw_brand(c, x0 + pad, y0 + height - 10.5 * mm, 8.5)

    hook = "Would you be OK with someone making a fake intimate image of you from your photos?"
    hook_lines = wrap(hook, BOLD, 17.5, usable)
    y = y0 + height - 20 * mm
    y = draw_lines(
        c,
        hook_lines,
        x0 + pad,
        y,
        font=BOLD,
        size=17.5,
        leading=19,
        color=BLACK,
        max_width=usable,
        label="A6 flyer hook",
    )

    y -= 3
    explainer = "Deepfakes can use a real person's face or voice in fake media without consent."
    explainer_lines = wrap(explainer, FONT, 9.6, usable)
    draw_lines(
        c,
        explainer_lines,
        x0 + pad,
        y,
        font=FONT,
        size=9.6,
        leading=12,
        color=MID,
        max_width=usable,
        label="A6 flyer explanation",
    )

    qr_size = 38 * mm
    qr_x = x0 + (width - qr_size) / 2
    qr_y = y0 + 49 * mm
    draw_qr(c, url, qr_x, qr_y, qr_size)

    c.setFillColor(BLACK)
    c.setFont(BOLD, 11)
    c.drawCentredString(x0 + width / 2, y0 + 44 * mm, "Scan to sign - 20 seconds")
    url_size = fitted_size(url, FONT, 7.2, 5.5, usable)
    c.setFillColor(MID)
    c.setFont(FONT, url_size)
    c.drawCentredString(x0 + width / 2, y0 + 39.5 * mm, url)

    help_y = y0 + 7 * mm
    help_h = 26 * mm
    c.setFillColor(LIGHT)
    c.roundRect(x0 + pad, help_y, usable, help_h, 4, fill=1, stroke=0)
    c.setFillColor(BLACK)
    c.setFont(BOLD, 8.4)
    c.drawString(x0 + pad + 7, help_y + help_h - 12, "If this happens to you:")
    help_text = "Report Remove (under 18)  \u00b7  StopNCII.org  \u00b7  Childline 0800 1111"
    help_lines = wrap(help_text, FONT, 7.7, usable - 14)
    draw_lines(
        c,
        help_lines,
        x0 + pad + 7,
        help_y + help_h - 25,
        font=FONT,
        size=7.7,
        leading=10,
        color=BLACK,
        max_width=usable - 14,
        label="A6 flyer help block",
    )


def build_flyers(url: str) -> Path:
    path = OUT / "flyers_A6x4.pdf"
    page_w, page_h = A4
    flyer_w = page_w / 2
    flyer_h = page_h / 2
    c = canvas.Canvas(str(path), pagesize=A4, pageCompression=1)
    c.setTitle("BanTheFake four-up A6 flyers")

    for row in range(2):
        for col in range(2):
            draw_flyer(c, col * flyer_w, row * flyer_h, flyer_w, flyer_h, url)

    c.setStrokeColor(CUT)
    c.setLineWidth(0.45)
    mark = 7 * mm
    c.line(page_w / 2, 0, page_w / 2, mark)
    c.line(page_w / 2, page_h - mark, page_w / 2, page_h)
    c.line(0, page_h / 2, mark, page_h / 2)
    c.line(page_w - mark, page_h / 2, page_w, page_h / 2)
    # A small centre cross makes alignment clear without drawing through the flyers.
    c.line(page_w / 2 - 2 * mm, page_h / 2, page_w / 2 + 2 * mm, page_h / 2)
    c.line(page_w / 2, page_h / 2 - 2 * mm, page_w / 2, page_h / 2 + 2 * mm)

    c.showPage()
    c.save()
    return path


def section_label(c: canvas.Canvas, text: str, x: float, y: float) -> None:
    c.setFillColor(RED)
    c.setFont(BOLD, 8)
    c.drawString(x, y, text.upper())


def draw_card_header(c: canvas.Canvas, page_w: float, page_h: float, side: str) -> tuple[float, float]:
    c.setFillColor(white)
    c.rect(0, 0, page_w, page_h, fill=1, stroke=0)
    c.setFillColor(RED)
    c.rect(0, page_h - 4 * mm, page_w, 4 * mm, fill=1, stroke=0)
    draw_brand(c, 9 * mm, page_h - 12 * mm, 10)
    c.setFillColor(MID)
    c.setFont(FONT, 7.5)
    c.drawRightString(page_w - 9 * mm, page_h - 11.5 * mm, side)
    return 9 * mm, page_h - 20 * mm


def card_box(
    c: canvas.Canvas,
    x: float,
    y_top: float,
    width: float,
    title: str,
    body: str,
    *,
    body_size: float = 9.2,
    fill=LIGHT,
) -> float:
    body_lines = wrap(body, FONT, body_size, width - 18)
    box_h = 22 + len(body_lines) * (body_size + 2.2) + 9
    y = y_top - box_h
    if y < 8 * mm:
        raise ValueError(f"Pitch card vertical overflow in section {title!r}")
    c.setFillColor(fill)
    c.roundRect(x, y, width, box_h, 5, fill=1, stroke=0)
    c.setFillColor(RED)
    c.setFont(BOLD, 8)
    c.drawString(x + 9, y_top - 15, title.upper())
    draw_lines(
        c,
        body_lines,
        x + 9,
        y_top - 29,
        font=FONT,
        size=body_size,
        leading=body_size + 2.2,
        color=BLACK,
        max_width=width - 18,
        label=f"pitch card {title}",
    )
    return y - 7


def build_pitch_card() -> Path:
    path = OUT / "pitch_card_A6.pdf"
    page_w, page_h = A6
    margin = 9 * mm
    usable = page_w - 2 * margin
    c = canvas.Canvas(str(path), pagesize=A6, pageCompression=1)
    c.setTitle("BanTheFake volunteer pitch card")

    x, y = draw_card_header(c, page_w, page_h, "FRONT")
    c.setFillColor(BLACK)
    c.setFont(BOLD, 21)
    c.drawString(x, y, "Start a conversation")
    y -= 10 * mm
    y = card_box(
        c,
        x,
        y,
        usable,
        "10-second opener A",
        "Hi - do you think anyone should be allowed to fake your face or voice without your consent?",
    )
    y = card_box(
        c,
        x,
        y,
        usable,
        "10-second opener B",
        "We're asking for fast takedowns and clear labels when AI media can harm real people.",
    )
    y = card_box(
        c,
        x,
        y,
        usable,
        "The ask",
        "Would you sign our petition? It takes about 20 seconds.",
        fill=Color(0.98, 0.91, 0.91),
    )

    section_label(c, "20-second sign flow", x, y - 2)
    y -= 17
    steps = [
        "1  Point to the QR code.",
        "2  Let them read before they sign.",
        "3  Thank them - no pressure.",
    ]
    for step in steps:
        c.setFillColor(BLACK)
        c.setFont(BOLD if step.startswith("1") else FONT, 9)
        check_line(step, c._fontname, 9, usable, "pitch card sign flow")
        c.drawString(x, y, step)
        y -= 14
    c.setFillColor(MID)
    c.setFont(FONT, 7.5)
    c.drawString(x, 7 * mm, "Keep it friendly. A 'no' ends the conversation.")
    c.showPage()

    x, y = draw_card_header(c, page_w, page_h, "BACK")
    c.setFillColor(BLACK)
    c.setFont(BOLD, 20)
    c.drawString(x, y, "Quick answers")
    y -= 9 * mm
    objections = [
        (
            "'Are you banning AI?'",
            "We're not banning AI - just using someone's face or voice without consent.",
        ),
        (
            "'Isn't this already illegal?'",
            "Sharing intimate deepfakes is already a crime, but voice scams, takedowns and election fakes still have gaps.",
        ),
        (
            "'What happens to my email?'",
            "We never share your email; you can ask for deletion - see the privacy page.",
        ),
    ]
    for title, answer in objections:
        y = card_box(c, x, y, usable, title, answer, body_size=8.1)

    section_label(c, "Safety reminders", x, y - 1)
    y -= 16
    safety = [
        "Go with a friend or adult.",
        "Stay on public pavements.",
        "Ask before setting up on private property, including shopping centres and stations.",
        "Do not collect personal data on paper.",
    ]
    for item in safety:
        lines = wrap(item, FONT, 8.1, usable - 14)
        c.setFillColor(RED)
        c.circle(x + 3, y + 3, 2.2, fill=1, stroke=0)
        y = draw_lines(
            c,
            lines,
            x + 12,
            y,
            font=FONT,
            size=8.1,
            leading=10,
            color=BLACK,
            max_width=usable - 14,
            label="pitch card safety reminder",
        )
        y -= 4
    if y < 7 * mm:
        raise ValueError("Pitch card back vertical overflow")
    c.showPage()
    c.save()
    return path


PETITION_TITLE = "Require 48-hour takedowns and ban non-consensual intimate deepfakes"
PETITION_WANT = (
    "Require platforms to remove reported non-consensual intimate deepfakes within 48 hours. "
    "Make creating or sharing them without consent a criminal offence for victims of every age, "
    "and require clear labels on AI-generated media."
)
PETITION_DETAILS = (
    "AI tools can turn ordinary photos into convincing intimate images without the person's consent. "
    "Victims need a fast, consistent route to removal, whatever their age. Current protections should "
    "cover both creating and sharing this material, while platforms should act on valid reports within "
    "48 hours and preserve evidence for law enforcement. Clear, durable labels on AI-generated images, "
    "audio and video would also help people judge what they see and hear. The Government should set "
    "enforceable duties, proportionate penalties and an appeals process that protects lawful journalism, "
    "satire and art."
)


def build_petition_draft() -> Path:
    limits = {
        "title": (PETITION_TITLE, 80),
        "what": (PETITION_WANT, 300),
        "details": (PETITION_DETAILS, 800),
    }
    for label, (text, limit) in limits.items():
        if len(text) > limit:
            raise ValueError(f"Petition {label} is {len(text)} characters; limit is {limit}")

    path = OUT / "parliament_petition_draft.md"
    content = f"""# Draft UK Parliament e-petition

## Title

{PETITION_TITLE}

Character count: **{len(PETITION_TITLE)}/80**

## What we want

{PETITION_WANT}

Character count: **{len(PETITION_WANT)}/300**

## More details

{PETITION_DETAILS}

Character count: **{len(PETITION_DETAILS)}/800**

## Submission note

The student must create and submit this petition themselves at https://petition.parliament.uk/. The petition site's own rules, checks and moderation apply. Review the live rules before submitting; acceptance is not guaranteed.

Counts include spaces and punctuation and are calculated by `make_print.py`.
"""
    path.write_text(content, encoding="utf-8")
    return path


def validate_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise argparse.ArgumentTypeError("--url must be an absolute http:// or https:// URL")
    return url


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        type=validate_url,
        default="https://github.com/anmolsrivastavadev-pixel/banthefake",
        help="Campaign URL encoded in every QR code and printed below it.",
    )
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    outputs = [
        build_sign(args.url),
        build_flyers(args.url),
        build_pitch_card(),
        build_petition_draft(),
    ]
    qr = make_qr(args.url, "q")
    print(f"QR: version {qr.version}, error correction {qr.error}, quiet zone 4 modules")
    for output in outputs:
        print(output.relative_to(ROOT))


if __name__ == "__main__":
    main()
