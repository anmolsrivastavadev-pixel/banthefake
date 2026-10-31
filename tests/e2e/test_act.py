"""Take action: the petition form and the letter generator."""

import uuid

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def api_count(page: Page) -> int:
    return page.request.get("/api/petition/count").json()["count"]


def fill_petition(page: Page, email: str, consent: bool = True) -> None:
    page.fill("#pName", "Ada Lovelace")
    page.fill("#pEmail", email)
    page.fill("#pLocation", "London")
    if consent:
        page.check("#petition input[name=consent]")


def unique_email() -> str:
    return f"e2e-{uuid.uuid4().hex[:10]}@example.org"


def test_consent_is_required(page: Page, watch):
    page.goto("/#act")
    posts = []
    page.on("request", lambda r: r.method == "POST" and "/api/petition" in r.url and posts.append(r))
    fill_petition(page, unique_email(), consent=False)
    page.click("#petition button[type=submit]")
    page.wait_for_timeout(300)
    assert posts == [], "the form must not submit without consent"
    assert page.eval_on_selector("#petition input[name=consent]", "c => c.validity.valueMissing")
    expect(page.locator("#thanks")).to_have_text("")


def test_sign_increments_count_then_duplicate_is_rejected(page: Page, watch):
    watch.allow(r"status of 409")
    page.goto("/#act")
    before = api_count(page)
    expect(page.locator("#count")).to_have_text(f"{before:,}")

    email = unique_email()
    fill_petition(page, email)
    page.click("#petition button[type=submit]")
    expect(page.locator("#thanks")).to_have_text("Signed. Thank you! Now send a letter →")
    expect(page.locator("#count")).to_have_text(f"{before + 1:,}")
    assert api_count(page) == before + 1
    width = page.eval_on_selector("#bar", "b => b.style.width")
    assert width.endswith("%") and float(width[:-1]) > 0
    # The form was reset after a successful signature.
    expect(page.locator("#pEmail")).to_have_value("")
    expect(page.locator("#petition input[name=consent]")).not_to_be_checked()

    fill_petition(page, email.upper())  # emails are compared case-insensitively
    page.click("#petition button[type=submit]")
    expect(page.locator("#thanks")).to_have_text("You've already signed. Thank you!")
    expect(page.locator("#count")).to_have_text(f"{before + 1:,}")
    assert api_count(page) == before + 1


def test_server_validation_error_is_shown(page: Page, watch):
    watch.allow(r"status of 422")
    page.goto("/#act")
    fill_petition(page, "not-an-email@")
    # Bypass the browser's own type=email check so the server's 422 path is exercised.
    page.eval_on_selector("#petition", "f => f.noValidate = true")
    page.click("#petition button[type=submit]")
    expect(page.locator("#thanks")).to_have_class("thanks err")
    expect(page.locator("#thanks")).not_to_have_text("")


def test_honeypot_is_hidden_and_not_counted(page: Page, watch):
    page.goto("/#act")
    hp = page.locator("#petition .hp")
    expect(hp).to_have_attribute("aria-hidden", "true")
    box = hp.bounding_box()
    assert box is not None and box["x"] + box["width"] <= 0, f"honeypot should be off-screen, got {box}"
    expect(page.locator("#petition input[name=website]")).to_have_attribute("tabindex", "-1")

    before = api_count(page)
    fill_petition(page, unique_email())
    page.eval_on_selector("#petition input[name=website]", "i => { i.value = 'http://spam.example'; }")
    page.click("#petition button[type=submit]")
    expect(page.locator("#thanks")).to_have_text("Signed. Thank you! Now send a letter →")  # bots see success
    assert api_count(page) == before


def test_letter_updates_with_inputs(page: Page, watch):
    page.goto("/#act")
    letter = page.locator("#letter")
    assert "[your name]" in letter.input_value()

    page.fill("#lname", "Grace Hopper")
    page.fill("#lplace", "Arlington")
    text = letter.input_value()
    assert text.startswith("Dear Representative,")
    assert "I'm Grace Hopper, a constituent from Arlington." in text
    assert text.rstrip().endswith("Grace Hopper")
    assert "[your name]" not in text

    page.select_option("#recipient", "platform")
    text = letter.input_value()
    assert text.startswith("To the Trust & Safety team,")
    assert "a user from Arlington" in text

    page.select_option("#recipient", "school")
    assert letter.input_value().startswith("Dear Administration,")
    expect(letter).to_have_attribute("readonly", "")


def test_copy_button_copies_letter(page: Page, watch, context, base_url):
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=base_url)
    page.goto("/#act")
    page.fill("#lname", "Katherine Johnson")
    page.click("#copy")
    expect(page.locator("#copy")).to_have_text("Copied ✓")
    expect(page.locator("#copyStatus")).to_have_text("Letter copied to the clipboard.")
    assert page.evaluate("navigator.clipboard.readText()") == page.locator("#letter").input_value()
    expect(page.locator("#copy")).to_have_text("Copy letter", timeout=5000)
