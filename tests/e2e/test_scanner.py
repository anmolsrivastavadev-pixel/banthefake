"""The scanner UI: uploads through the real file input, rendering of each report, error states and XSS safety."""

import json

import pytest
from playwright.sync_api import Page, expect

from tests import media
from tests.e2e.conftest import FIXTURES, fixture_path, hf_cache_has_pinned_models, HF_CACHE

pytestmark = pytest.mark.e2e

FIXTURE_FILES = sorted(p.name for p in FIXTURES.iterdir() if p.suffix.lower() in {".jpg", ".png", ".mp4", ".mov"})
VERDICT_CLASS = {"verified_capture": "verified", "verified_ai": "warn", "ai": "warn", "edited": "caution",
                 "camera": "neutral", "unknown": ""}
XSS = "<img src=x onerror=window.__xss=1>"


def api_scan(page: Page, name: str, data: bytes, mime: str) -> dict:
    res = page.request.post("/api/scan", multipart={"file": {"name": name, "mimeType": mime, "buffer": data}})
    assert res.ok, res.text()
    return res.json()


def wait_for_result(page: Page) -> None:
    expect(page.locator("#scanStatus")).to_contain_text("Scan finished.", timeout=60_000)


def assert_ela_rendered(page: Page) -> None:
    expect(page.locator("#elaWrap")).to_be_visible()
    w, h, alpha = page.eval_on_selector(
        "#ela", "c => [c.width, c.height, c.getContext('2d').getImageData(0, 0, 1, 1).data[3]]"
    )
    assert w > 0 and h > 0 and alpha == 255, (w, h, alpha)


@pytest.mark.parametrize("name", FIXTURE_FILES)
def test_upload_fixture_renders_report(page: Page, watch, name):
    path = fixture_path(name)
    mime = {".jpg": "image/jpeg", ".png": "image/png", ".mp4": "video/mp4", ".mov": "video/quicktime"}[path.suffix]
    page.goto("/")
    expected = api_scan(page, name, path.read_bytes(), mime)

    page.set_input_files("#file", path)
    wait_for_result(page)

    verdict = page.locator("#verdict")
    expect(verdict.locator("b")).to_have_text(expected["verdict_title"])
    classes = verdict.evaluate("v => [...v.classList]")
    cls = VERDICT_CLASS[expected["verdict"]]
    assert classes == (["verdict", cls] if cls else ["verdict"]), classes
    items = page.locator("#signals li")
    expect(items).to_have_count(len(expected["signals"]))
    for i, s in enumerate(expected["signals"]):
        expect(items.nth(i).locator("span").last).to_have_text(s["text"])

    expect(page.locator("#details .block h3", has_text="File")).to_be_visible()
    if expected.get("c2pa"):
        expect(page.locator("#details .block h3", has_text="Content credentials (C2PA)")).to_be_visible()

    # The input stays labelled once the preview replaces the visible text.
    expect(page.locator("#file")).to_have_attribute("aria-label", "Choose another image or video to scan")
    if mime.startswith("image/"):
        preview = page.locator("#dropInner img")
        expect(preview).to_have_attribute("alt", f"Preview of {name}")
        page.wait_for_function("document.querySelector('#dropInner img').complete")
        assert page.eval_on_selector("#dropInner img", "i => i.naturalWidth") > 0
        assert_ela_rendered(page)
    else:
        expect(page.locator("#dropInner video")).to_have_count(1)
        expect(page.locator("#elaWrap")).to_be_hidden()


def test_pixel10_shows_verified_capture_and_c2pa_block(page: Page, watch):
    page.goto("/")
    page.set_input_files("#file", fixture_path("pixel10_verified.jpg"))
    wait_for_result(page)
    expect(page.locator("#verdict")).to_have_class("verdict verified")
    expect(page.locator("#verdict b")).to_have_text("Verified camera capture")
    c2pa = page.locator("#details .block").filter(has=page.locator("h3", has_text="Content credentials (C2PA)"))
    expect(c2pa).to_have_count(1)
    expect(c2pa.locator("dt", has_text="Signature").locator("xpath=following-sibling::dd[1]")).to_have_text(
        "Valid, signer is on the trust list"
    )
    expect(c2pa.locator("dt", has_text="Capture evidence").locator("xpath=following-sibling::dd[1]")).to_have_text(
        "Yes, declared as a camera capture"
    )
    expect(c2pa.locator("dt", has_text="AI-generated").locator("xpath=following-sibling::dd[1]")).to_have_text(
        "Not declared"
    )
    expect(page.locator("#signals .tag.c2pa").first).to_be_visible()
    assert_ela_rendered(page)


def test_unsupported_file_shows_415_message(page: Page, watch):
    watch.allow(r"status of 415")
    page.goto("/")
    page.set_input_files("#file", files=[{"name": "notes.txt", "mimeType": "text/plain", "buffer": b"just text\n"}])
    expect(page.locator("#verdict b")).to_have_text("Unsupported file")
    expect(page.locator("#verdict")).to_have_class("verdict caution")
    expect(page.locator("#verdict .muted")).to_contain_text("Unsupported file type")
    expect(page.locator("#signals li")).to_have_count(0)
    expect(page.locator("#elaWrap")).to_be_hidden()
    expect(page.locator("#scanStatus")).to_contain_text("Unsupported file.")
    assert any("415" in p for p in watch.problems)


@pytest.fixture(scope="module")
def small_server(start_server):
    return start_server(BTF_MAX_UPLOAD_MB="1")


def test_too_large_file_shows_413_message(page: Page, watch, small_server):
    watch.allow(r"status of 413")
    page.goto(small_server.url + "/")
    big = fixture_path("real_bridge.jpg").read_bytes()
    big += b"\0" * max(0, 2 * 1024 * 1024 - len(big))  # still sniffs as JPEG, but over 1 MB
    page.set_input_files("#file", files=[{"name": "big.jpg", "mimeType": "image/jpeg", "buffer": big}])
    expect(page.locator("#verdict b")).to_have_text("File too large")
    expect(page.locator("#verdict .muted")).to_have_text(
        "This file is over the upload limit. Try a smaller file or a shorter clip."
    )


def test_png_text_chunk_is_rendered_as_text_not_html(page: Page, watch):
    """Attacker-controlled metadata (and file name) must never be parsed as HTML."""
    page.goto("/")
    png = media.png({"parameters": f"{XSS}\nSteps: 30, Sampler: Euler, CFG scale: 7, Seed: 1"})
    name = '<img src=x onerror="window.__xss=2">.png'
    page.set_input_files("#file", files=[{"name": name, "mimeType": "image/png", "buffer": png}])
    wait_for_result(page)
    page.wait_for_timeout(300)  # give any injected onerror handler a chance to fire

    expect(page.locator("#signals li").first).to_contain_text(XSS)
    assert page.evaluate("typeof window.__xss") == "undefined"
    # The only <img> elements in the scanner are our own preview.
    assert page.locator("#resultPanel img").count() == 0
    assert page.locator("#dropInner img").count() == 1
    expect(page.locator("#dropInner img")).to_have_attribute("alt", f"Preview of {name}")
    assert_ela_rendered(page)


def test_new_upload_replaces_previous_result(page: Page, watch):
    page.goto("/")
    page.set_input_files("#file", fixture_path("pixel10_verified.jpg"))
    wait_for_result(page)
    expect(page.locator("#verdict b")).to_have_text("Verified camera capture")
    page.set_input_files("#file", fixture_path("test_clip.mp4"))
    expect(page.locator("#verdict b")).to_have_text("No conclusive signals found")
    expect(page.locator("#details .block h3", has_text="Content credentials")).to_have_count(0)
    expect(page.locator("#elaWrap")).to_be_hidden()
    expect(page.locator("#dropInner img")).to_have_count(0)


@pytest.mark.skipif(not hf_cache_has_pinned_models(),
                    reason="needs the ml extra and both pinned detectors in the local HF cache "
                           "(research/detect/hf_cache or BTF_E2E_HF_HOME)")
def test_real_models_flag_ai_image(page: Page, watch, start_server):
    ml_server = start_server(
        BTF_MODEL=None, HF_HOME=str(HF_CACHE), HF_HUB_OFFLINE="1",
        ready_timeout=600, ready_until=lambda body: json.loads(body)["model"] == "loaded",
    )
    page.goto(ml_server.url + "/")
    page.set_input_files("#file", fixture_path("ai_dalle_mars.jpg"))
    wait_for_result(page)
    expect(page.locator("#verdict")).to_have_class("verdict warn")
    models = page.locator("#details .block").filter(has=page.locator("h3", has_text="AI detector models"))
    expect(models).to_have_count(1)
    expect(models.locator("dd", has_text="Likely AI-generated")).to_have_count(1)
    expect(models.locator("[role=meter]")).to_have_count(2)
