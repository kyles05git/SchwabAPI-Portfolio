"""Tests for the HTML email builder (pure string building; no network)."""

from __future__ import annotations

from schwab_trader import emailfmt


def test_esc_escapes_markup() -> None:
    assert emailfmt.esc('<b>&"') == "&lt;b&gt;&amp;&quot;"


def test_colored_wraps_by_tone_and_escapes() -> None:
    assert emailfmt.GREEN in emailfmt.colored("+1%", "up")
    assert emailfmt.RED in emailfmt.colored("-1%", "down")
    # No tone -> plain escaped text, no span.
    assert emailfmt.colored("<x>", "") == "&lt;x&gt;"


def test_tone_for_maps_sign() -> None:
    assert emailfmt.tone_for(1.0) == "up"
    assert emailfmt.tone_for(-1.0) == "down"
    assert emailfmt.tone_for(0.0) == "muted"


def test_document_wraps_and_escapes_heading() -> None:
    doc = emailfmt.document(
        heading="Report <x>", subheading="2026-07-20", inner_html="<p>body</p>", footer="foot"
    )
    assert doc.startswith("<!doctype html>")
    assert "Report &lt;x&gt;" in doc  # heading escaped
    assert "<p>body</p>" in doc  # inner html preserved
    assert "2026-07-20" in doc and "foot" in doc


def test_data_table_escapes_headers_but_trusts_cells() -> None:
    html = emailfmt.data_table(["a<b>"], [["<span>1</span>"]], ["left"])
    assert "a&lt;b&gt;" in html  # header escaped
    assert "<span>1</span>" in html  # cell HTML preserved (caller-rendered)


def test_kv_table_escapes_keys_keeps_value_html() -> None:
    html = emailfmt.kv_table([("k<e>y", "<em>v</em>")])
    assert "k&lt;e&gt;y" in html
    assert "<em>v</em>" in html


def test_button_row_escapes_command() -> None:
    html = emailfmt.button_row("Run:", "schwab-trader agent approve <tok>")
    assert "schwab-trader agent approve &lt;tok&gt;" in html
