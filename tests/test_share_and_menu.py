"""Favicon, link-preview metadata, the mobile menu and petition sharing markup."""

import re


def test_favicon_served_as_svg(client):
    res = client.get("/favicon.svg")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("image/svg+xml")
    assert "<script" not in res.text


def test_every_page_links_the_favicon_and_has_a_description(client):
    for path in ["/", "/privacy.html", "/corrections.html"]:
        html = client.get(path).text
        assert '<link rel="icon" href="/favicon.svg"' in html, path
        assert re.search(r'<meta name="description" content="[^"]{40,}"', html), path


def test_index_has_link_preview_tags(client):
    html = client.get("/").text
    for prop in ["og:title", "og:description", "og:type", "og:site_name"]:
        assert f'property="{prop}"' in html
    assert 'name="twitter:card"' in html


def test_menu_button_controls_nav_links(client):
    html = client.get("/").text
    btn = re.search(r'<button class="menu-btn"[^>]*>', html).group(0)
    assert 'type="button"' in btn
    assert 'aria-expanded="false"' in btn
    assert 'aria-controls="navLinks"' in btn
    assert '<ul class="nav-links" id="navLinks">' in html


def test_share_panel_needs_no_third_party_script(client):
    html = client.get("/").text
    share = re.search(r'<div class="share" id="share">.*?</div>\s*</div>', html, re.S).group(0)
    assert 'id="copyLink"' in share
    assert 'href="https://wa.me/' in share and 'rel="noopener"' in share
    assert "<script" not in share
    assert 'role="status"' in html.split('id="share"', 1)[1]
