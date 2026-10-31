"""Page loads, keyboard access, small screens and reduced motion."""

import pytest
from playwright.sync_api import Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tests.e2e.conftest import fixture_path

pytestmark = pytest.mark.e2e

PAGES = ["/", "/privacy.html"]


@pytest.mark.parametrize("path", PAGES)
def test_page_loads_without_console_or_csp_errors(page: Page, watch, path):
    res = page.goto(path)
    assert res is not None and res.ok
    assert "default-src 'self'" in res.headers["content-security-policy"]
    page.wait_for_load_state("networkidle")
    expect(page.locator("h1")).to_be_visible()
    expect(page.locator("main#main")).to_have_count(1)
    assert not watch.problems


def test_index_scripts_ran(page: Page, watch):
    """app.js ran under the CSP: the letter is pre-filled and the petition count was fetched."""
    page.goto("/")
    assert page.locator("#letter").input_value().startswith("Dear Representative,")
    api = page.request.get("/api/petition/count").json()
    expect(page.locator("#count")).to_have_text(f"{api['count']:,}")
    expect(page.locator("#goal")).to_have_text(f"{api['goal']:,}")


@pytest.mark.parametrize("path", PAGES)
def test_skip_link_is_first_and_moves_focus_to_main(page: Page, watch, path):
    page.goto(path)
    page.keyboard.press("Tab")
    skip = page.locator("a.skip")
    expect(skip).to_be_focused()
    box = skip.bounding_box()
    assert box is not None and box["y"] >= 0, "skip link should slide into view when focused"
    page.keyboard.press("Enter")
    assert page.evaluate("document.activeElement.id") == "main"


def _tab_until(page: Page, predicate_js: str, limit: int = 60) -> int:
    for i in range(1, limit + 1):
        page.keyboard.press("Tab")
        if page.evaluate(predicate_js):
            return i
    raise AssertionError(f"never reached `{predicate_js}` after {limit} tabs")


def test_keyboard_only_scan_flow(page: Page, watch):
    page.goto("/")
    page.keyboard.press("Tab")
    page.keyboard.press("Enter")  # skip link
    _tab_until(page, "document.activeElement.id === 'file'")

    # The hidden file input is focused, so the drop zone must show the focus ring.
    ring = page.eval_on_selector(
        "#drop", "d => { const s = getComputedStyle(d); return [s.outlineStyle, s.outlineWidth, s.outlineColor]; }"
    )
    assert ring[0] == "solid" and ring[1] == "3px", ring
    assert ring[2] == "rgb(255, 181, 71)", ring  # --focus: #ffb547

    # Headless Chromium occasionally drops the first activation of a file input, so allow a retry,
    # still keyboard-only: Enter twice, then Space (the other standard activation key).
    chooser = None
    for key in ("Enter", "Enter", "Space"):
        expect(page.locator("#file")).to_be_focused()
        try:
            with page.expect_file_chooser(timeout=5000) as fc:
                page.keyboard.press(key)
            chooser = fc.value
            break
        except PlaywrightTimeoutError:
            continue
    assert chooser is not None, "keyboard activation never opened the file chooser"
    chooser.set_files(fixture_path("real_cat_camera_exif.jpg"))
    expect(page.locator("#verdict b")).to_have_text("Camera data found, no AI markers")
    expect(page.locator("#scanStatus")).to_contain_text("Scan finished.")


def test_focus_ring_on_form_controls(page: Page, watch):
    page.goto("/#act")
    page.locator("#pLocation").focus()
    page.keyboard.press("Tab")  # honeypot is tabindex=-1: next stop is the consent checkbox
    assert page.evaluate("document.activeElement.name") == "consent"
    style = page.evaluate("getComputedStyle(document.activeElement).outlineStyle")
    assert style == "solid"


@pytest.mark.parametrize("path", PAGES)
def test_mobile_viewport_has_no_horizontal_overflow(page: Page, watch, path):
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto(path)
    page.wait_for_load_state("networkidle")
    assert page.evaluate("document.scrollingElement.scrollWidth <= innerWidth"), page.evaluate(
        "[document.scrollingElement.scrollWidth, innerWidth]"
    )


@pytest.mark.parametrize("fixture", ["pixel10_verified.jpg", "C.jpg", "test_clip.mp4"])
def test_mobile_viewport_no_overflow_after_scan(page: Page, watch, fixture):
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto("/")
    page.set_input_files("#file", fixture_path(fixture))
    expect(page.locator("#scanStatus")).to_contain_text("Scan finished.")
    widths = page.evaluate("[document.scrollingElement.scrollWidth, innerWidth]")
    assert widths[0] <= widths[1], widths


def test_reduced_motion_is_respected(page: Page, watch):
    page.goto("/")
    assert page.evaluate("getComputedStyle(document.documentElement).scrollBehavior") == "smooth"
    assert page.eval_on_selector(".btn", "b => getComputedStyle(b).transitionDuration").startswith("0.15s")

    page.emulate_media(reduced_motion="reduce")
    page.reload()
    assert page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches")
    assert page.evaluate("getComputedStyle(document.documentElement).scrollBehavior") == "auto"
    durations = page.evaluate(
        """() => [...document.querySelectorAll('.btn, .drop, .progress i, .report a')]
              .flatMap(e => getComputedStyle(e).transitionDuration.split(',').map(parseFloat))"""
    )
    assert durations and max(durations) <= 0.001, durations
