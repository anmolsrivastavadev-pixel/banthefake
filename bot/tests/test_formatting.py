from bot.formatting import NO_PROOF, format_help, format_report


def test_report_uses_server_wording_caveats_and_limits_signals():
    report = {
        "verdict": "verified_capture",
        "verdict_title": "Verified capture origin",
        "verdict_detail": "Trusted signed capture credentials are valid.",
        "signals": [{"kind": "c2pa", "text": f"signal {number}"} for number in range(12)],
    }
    embed = format_report(report, "https://scan.example.org")
    assert embed.title == report["verdict_title"]
    assert report["verdict_detail"] in embed.description
    assert "signed origin" in embed.description
    assert "staged scene" in embed.description
    assert NO_PROOF in embed.description
    assert embed.fields[0].value.count("**C2PA:**") == 7
    assert "5 more signal" in embed.fields[0].value
    assert len(embed.fields[0].value.splitlines()) == 8
    assert "https://scan.example.org/#report" in embed.fields[1].value


def test_camera_result_is_neutral_and_never_claims_verification():
    embed = format_report(
        {
            "verdict": "camera",
            "verdict_title": "Camera metadata found",
            "verdict_detail": "Camera EXIF was present.",
            "signals": [],
        },
        "https://scan.example.org",
    )
    assert embed.colour.value == 0x95A5A6
    assert "does not verify" in embed.description
    assert NO_PROOF in embed.description


def test_help_contains_requested_resources_and_bot_limits():
    embed = format_help("https://scan.example.org")
    names = {field.name for field in embed.fields}
    assert {"Report Remove", "Childline", "StopNCII", "CEOP", "Take It Down"} <= names
    assert "does not run detectors itself" in embed.description
    assert "does not" in embed.description
