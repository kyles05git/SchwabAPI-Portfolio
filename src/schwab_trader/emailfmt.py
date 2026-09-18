"""Small, dependency-free HTML email builder.

Produces a clean, self-contained HTML document for notification emails using only
inline styles and tables - the subset that renders consistently across email
clients (most strip ``<style>`` blocks and don't support flexbox/grid). Callers
compose the inner HTML from the helpers here (:func:`kv_table`, :func:`data_table`,
:func:`colored`) and wrap it with :func:`document`.

Pure string building: no network, no I/O. Dynamic text must be passed through
:func:`esc`; the table helpers escape header text but treat row-cell content as
already-rendered HTML (so callers can colour numbers with :func:`colored`).
"""

from __future__ import annotations

import html

FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
INK = "#1a1d21"
MUTED = "#6b7580"
LINE = "#e6e9ec"
PAGE_BG = "#f4f5f7"
CARD_BG = "#ffffff"
HEADER_BG = "#11161c"
GREEN = "#1e8e4e"
RED = "#d1342f"


def esc(text: object) -> str:
    """HTML-escape any value for safe inclusion in the document."""
    return html.escape(str(text), quote=True)


def colored(text: object, tone: str = "") -> str:
    """Wrap ``text`` (escaped) in a colour span. ``tone`` is up | down | muted | ''."""
    color = {"up": GREEN, "down": RED, "muted": MUTED}.get(tone)
    safe = esc(text)
    return f'<span style="color:{color}">{safe}</span>' if color else safe


def tone_for(value: float) -> str:
    """Map a signed number to a colour tone: positive up, negative down, else muted."""
    if value > 0:
        return "up"
    if value < 0:
        return "down"
    return "muted"


def kv_table(rows: list[tuple[str, str]]) -> str:
    """A borderless two-column key/value table. Keys escaped; values are HTML."""
    cells = "".join(
        f"<tr>"
        f'<td style="padding:4px 16px 4px 0;color:{MUTED};white-space:nowrap">{esc(k)}</td>'
        f'<td style="padding:4px 0;text-align:right;font-variant-numeric:tabular-nums">{v}</td>'
        f"</tr>"
        for k, v in rows
    )
    return f'<table role="presentation" style="border-collapse:collapse;width:100%">{cells}</table>'


def data_table(headers: list[str], rows: list[list[str]], aligns: list[str] | None = None) -> str:
    """A bordered data table. Header text is escaped; row cells are trusted HTML."""
    aligns = aligns or ["left"] * len(headers)
    head = "".join(
        f'<th style="text-align:{a};padding:8px 10px;border-bottom:2px solid {LINE};'
        f'color:{MUTED};font-size:12px;font-weight:600;white-space:nowrap">{esc(h)}</th>'
        for h, a in zip(headers, aligns, strict=False)
    )
    body_rows = []
    for row in rows:
        tds = "".join(
            f'<td style="text-align:{a};padding:7px 10px;border-bottom:1px solid {LINE};'
            f'white-space:nowrap;font-variant-numeric:tabular-nums">{cell}</td>'
            for cell, a in zip(row, aligns, strict=False)
        )
        body_rows.append(f"<tr>{tds}</tr>")
    return (
        '<table role="presentation" style="border-collapse:collapse;width:100%;'
        f'font-size:13px;color:{INK}"><thead><tr>{head}</tr></thead>'
        f"<tbody>{''.join(body_rows)}</tbody></table>"
    )


def note(text_html: str) -> str:
    """A muted note paragraph (caller supplies already-safe/rendered HTML)."""
    return (
        f'<p style="margin:14px 0 0;color:{MUTED};font-size:13px;line-height:1.5">{text_html}</p>'
    )


def button_row(label: str, command: str) -> str:
    """A labelled, copy-pasteable command block (email clients can't run links)."""
    return (
        f'<div style="margin:16px 0">'
        f'<div style="color:{MUTED};font-size:12px;margin-bottom:6px">{esc(label)}</div>'
        f'<div style="font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;'
        f"font-size:13px;background:{PAGE_BG};border:1px solid {LINE};border-radius:6px;"
        f'padding:10px 12px;word-break:break-all;color:{INK}">{esc(command)}</div></div>'
    )


def document(*, heading: str, subheading: str = "", inner_html: str, footer: str = "") -> str:
    """Wrap inner HTML in a centered card with a header bar and optional footer."""
    sub = (
        f'<div style="color:#aeb6bf;font-size:13px;margin-top:3px">{esc(subheading)}</div>'
        if subheading
        else ""
    )
    foot = (
        f'<div style="text-align:center;color:{MUTED};font-size:12px;padding:16px 0 4px">'
        f"{esc(footer)}</div>"
        if footer
        else ""
    )
    return (
        f'<!doctype html><html><body style="margin:0;padding:0;background:{PAGE_BG}">'
        f'<div style="font-family:{FONT};color:{INK};background:{PAGE_BG};padding:24px 12px">'
        f'<table role="presentation" style="max-width:640px;margin:0 auto;width:100%;'
        f'border-collapse:collapse"><tr><td>'
        f'<div style="background:{HEADER_BG};border-radius:10px 10px 0 0;padding:18px 22px">'
        f'<div style="color:#e9edf1;font-size:16px;font-weight:650">{esc(heading)}</div>{sub}</div>'
        f'<div style="background:{CARD_BG};border:1px solid {LINE};border-top:none;'
        f'border-radius:0 0 10px 10px;padding:20px 22px">{inner_html}</div>'
        f"{foot}"
        f"</td></tr></table></div></body></html>"
    )
