"""Public scanner-mistake reporting and correction guidance."""

import re


def test_corrections_page_is_public_cautious_and_does_not_collect_reports(client):
    res = client.get("/corrections.html")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert "default-src 'self'" in res.headers["content-security-policy"]
    assert res.headers["x-content-type-options"] == "nosniff"
    html = res.text
    assert "Report a scanner mistake" in html
    assert "A result is not an accusation" in html
    assert "no tool can prove" in html
    assert "false-result issue form" in html
    assert "false_result.yml" in html
    assert "your GitHub username and all comments will be public" in html
    assert "retained by GitHub under its policies" in html
    assert "Sensitive correction to a statement controlled by BanTheFake" in html
    assert "SECURITY.md" in html
    assert "security/advisories/new" in html
    assert 'href="/#report"' in html
    assert 'href="/privacy.html"' in html
    assert "Do not attach or link intimate or private media" in html
    assert "does not need the private media" in html
    assert "safe trusted adult or safeguarding lead" in html
    assert "copy the verdict and textual signals" in html
    assert "Do not attach a screenshot" in html
    assert "not a BanTheFake result (supported by evidence)" in html
    assert "the exact reported file remains unresolved" in html
    assert "forged screenshot" not in html
    assert "Report template" in html
    assert "What happens next" in html
    assert "When we escalate" in html
    assert "Correction:" in html
    assert '<a class="skip" href="#main">' in html and 'id="main"' in html
    assert len(re.findall(r"<h1[\s>]", html)) == 1
    assert "<form" not in html  # canonical report routes are links; this page stores nothing


def test_corrections_page_needs_no_inline_code_and_is_linked_from_home(client):
    html = client.get("/corrections.html").text
    assert "<script" not in html
    assert "<style" not in html
    assert not re.search(r"\sstyle=", html)
    assert not re.search(r"\son[a-z]+=", html)
    assert '<link rel="stylesheet" href="/styles.css"' in html
    home = client.get("/").text
    footer = re.search(r"<footer>.*?</footer>", home, re.S).group(0)
    assert 'href="/corrections.html"' in footer
    result_panel = re.search(r'<div class="panel" id="resultPanel">.*?<p class="result-help">.*?</p>', home, re.S).group(0)
    assert 'href="/corrections.html"' in result_panel


def test_under_18_boundary_and_safe_adult_rule_are_explicit(client):
    home = client.get("/").text
    report = re.search(r'<section id="report">.*?</section>', home, re.S).group(0)
    for text in ("under 18", "do not download", "screenshot", "forward", "email", "upload"):
        assert text in report
    assert "Report Remove" in report
    assert "safe trusted adult or safeguarding lead" in report

    from pathlib import Path

    drill = (Path(__file__).resolve().parents[1] / "docs" / "FALSE-POSITIVE-DRILL.md").read_text()
    assert "safe trusted adult" in drill
    assert "safeguarding lead" in drill
    assert "Do not request private or intimate media" in drill
    assert "Never declare the file or depicted scene real" in drill
    assert "parent is unsafe" in drill
    assert "accuse a named person" in drill
    assert "public issue contains doxxing or harmful media" in drill

    issue_form = (Path(__file__).resolve().parents[1] / ".github" / "ISSUE_TEMPLATE" / "false_result.yml").read_text()
    assert "This creates a public GitHub issue" in issue_form
    assert "any image of someone under 18" in issue_form
    assert "I have not attached or linked an image or video of anyone under 18" in issue_form


def test_private_contact_discloses_sensitive_correction_scope_and_provider(client):
    privacy = client.get("/privacy.html").text
    assert "privately report a sensitive correction" in privacy
    assert "send text only and do not attach or link the media" in privacy
    assert "provider handles what you send under its own privacy policy" in privacy
