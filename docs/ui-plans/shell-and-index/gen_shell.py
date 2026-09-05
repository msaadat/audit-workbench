#!/usr/bin/env python3
"""Generate the artboards for the shell header and engagements index redesign.

One function per artboard. Every value is a literal hex or px so the markup
answers "what size is that" by itself; the token map in the redesign document
says what to write in the components. The palette, the icon set and the page
primitives are those of ../sources-planning-reporting/gen_views.py, restated
here so this folder regenerates on its own.

Run from this directory:  python3 gen_shell.py
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).parent

# --- Palette (literal values of the tokens in frontend/src/style.css) -------
C = dict(
    canvas="#f6f8fb", panel="#ffffff", raised="#eef2f7",
    border="#dce5ee", border_strong="#c5d2e0",
    ink_strong="#07162b", ink="#0d2340", ink_soft="#46576d", muted="#5a6a81",
    navy_950="#07162b", navy_900="#0d2340", navy_800="#14294a",
    on_dark="#ffffff", on_navy="#e6edf6", on_navy_muted="#8fa6c2",
    teal="#0f766e", teal_600="#0d9488", teal_strong="#0b625c", teal_soft="#e7f7f4", teal_line="#a7ded8",
    mint="#5eead4", mint_600="#2dd4bf",
    accent="#7c3aed", accent_soft="#f5f0ff", accent_line="#d9ccf5",
    ok="#147d55", ok_soft="#e5f6ee", ok_line="#9fd9be",
    warn="#b45309", warn_ink="#8a4308", warn_soft="#fdf1e3", warn_line="#f0cf9f",
    danger="#b42318", danger_ink="#7f1d1d", danger_soft="#fdecea", danger_line="#f2b8b2",
    info="#1d4ed8", info_soft="#eff6ff", info_line="#bfdbfe",
)
MONO = "'JetBrains Mono', ui-monospace, monospace"
SANS = "'Inter', ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif"

# --- Icons (inline SVG stand-ins for PrimeIcons) ----------------------------
def svg(paths: str, size: int = 14, stroke: str = "currentColor", fill: str = "none", width: float = 2) -> str:
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="{fill}" stroke="{stroke}" '
            f'stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round">{paths}</svg>')

ICON = {
    "search": '<circle cx="11" cy="11" r="7"></circle><path d="m20 20-3.5-3.5"></path>',
    "plus": '<path d="M12 5v14"></path><path d="M5 12h14"></path>',
    "sparkles": '<path d="m12 3 1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"></path>',
    "chev_down": '<path d="m6 9 6 6 6-6"></path>',
    "chev_right": '<path d="m9 6 6 6-6 6"></path>',
    "check": '<path d="M20 6 9 17l-5-5"></path>',
    "warning": '<path d="M12 3 2 20h20z"></path><path d="M12 10v4"></path><path d="M12 17h.01"></path>',
    "upload": '<path d="M12 16V4"></path><path d="m6 10 6-6 6 6"></path><path d="M4 20h16"></path>',
    "grid": '<rect x="3" y="3" width="8" height="8" rx="1"></rect><rect x="13" y="3" width="8" height="8" rx="1"></rect><rect x="3" y="13" width="8" height="8" rx="1"></rect><rect x="13" y="13" width="8" height="8" rx="1"></rect>',
    "code": '<path d="m8 8-4 4 4 4"></path><path d="m16 8 4 4-4 4"></path>',
    "info": '<circle cx="12" cy="12" r="9"></circle><path d="M12 11v5"></path><path d="M12 8h.01"></path>',
    "briefcase": '<rect x="3" y="7" width="18" height="13" rx="2"></rect><path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path><path d="M3 12h18"></path>',
    "file": '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"></path><path d="M14 3v6h6"></path>',
    "table": '<rect x="3" y="4" width="18" height="16" rx="2"></rect><path d="M3 10h18"></path><path d="M9 4v16"></path>',
    "zoom": '<circle cx="11" cy="11" r="7"></circle><path d="m20 20-3.5-3.5"></path><path d="M11 8v6"></path><path d="M8 11h6"></path>',
    "monitor": '<rect x="3" y="4" width="18" height="12" rx="2"></rect><path d="M8 20h8"></path><path d="M12 16v4"></path>',
    "signout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"></path><path d="m16 17 5-5-5-5"></path><path d="M21 12H9"></path>',
    "folder": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"></path>',
    "play": '<path d="m7 5 12 7-12 7z"></path>',
    "external": '<path d="M14 4h6v6"></path><path d="M20 4 10 14"></path><path d="M18 13v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h5"></path>',
}

def ic(name: str, size: int = 14, stroke: str = "currentColor", width: float = 2) -> str:
    return svg(ICON[name], size, stroke, width=width)

def kebab_icon(size: int = 16) -> str:
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="currentColor" stroke="none">'
            '<circle cx="12" cy="5" r="1.8"></circle><circle cx="12" cy="12" r="1.8"></circle>'
            '<circle cx="12" cy="19" r="1.8"></circle></svg>')

# --- Frame ---------------------------------------------------------------------
def frame(body: str, height: int, width: int = 1440, bg: str | None = None) -> str:
    return f'''<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <script src="./support.js"></script>
</head>
<body>
<x-dc>
<helmet>
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap">
  <style>
    body {{ margin: 0; font-family: {SANS}; color: {C['ink']}; -webkit-font-smoothing: antialiased; }}
    a {{ color: {C['teal']}; text-decoration: none; }} a:hover {{ color: {C['teal_strong']}; }}
    svg {{ flex: 0 0 auto; }}
    .num {{ font-variant-numeric: tabular-nums; }}
    .ell {{ white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  </style>
</helmet>
<div style="width: {width}px; min-height: {height}px; background: {bg or C['canvas']}; display: flex; flex-direction: column;">
{body}
</div>
</x-dc>
</body>
</html>
'''

# --- The header ----------------------------------------------------------------
# 44 px, navy. Left: brand mark, then the trail. Right: at most two labelled
# buttons and one kebab. Everything else the two headers offer today (theme,
# presentation size, diagnostics, about, all workspaces, sign out) is in the
# kebab.

def brand_mark(size: int = 26) -> str:
    return (f'<a href="#" aria-label="All engagements" style="display: grid; place-items: center; width: {size}px; height: {size}px; flex: 0 0 auto; border-radius: 7px; '
            f'background: linear-gradient(135deg, {C["mint"]} 0%, {C["mint_600"]} 100%); color: {C["navy_950"]}; '
            f'box-shadow: 0 0 0 1px rgba(94,234,212,0.25);">{ic("check", 15, width=2.6)}</a>')

# Two palettes for the trail: on navy (the proposal) and on the panel colour
# (the light alternate). Same anatomy, different ink.
DARK = dict(link=C["on_navy"], current=C["on_dark"], sep="rgba(255,255,255,0.32)", chev=C["on_navy_muted"], open_bg="rgba(255,255,255,0.12)")
LIGHT = dict(link=C["ink_soft"], current=C["ink_strong"], sep=C["border_strong"], chev=C["muted"], open_bg=C["raised"])

def crumb_sep(p: dict = DARK) -> str:
    return f'<span style="display: inline-flex; color: {p["sep"]};">{ic("chev_right", 13, width=2.2)}</span>'

def crumb_link(label: str, mono: bool = False, p: dict = DARK) -> str:
    font = f"font-family: {MONO}; font-size: 12.5px;" if mono else "font-size: 13px;"
    return (f'<a href="#" class="ell" style="display: inline-flex; align-items: center; max-width: 320px; padding: 4px 8px; border-radius: 6px; '
            f'color: {p["link"]}; {font} font-weight: 600;">{label}</a>')

def crumb_current(label: str, mono: bool = False, p: dict = DARK) -> str:
    font = f"font-family: {MONO}; font-size: 12.5px;" if mono else "font-size: 13px;"
    return (f'<span class="ell" aria-current="page" style="display: inline-flex; align-items: center; max-width: 360px; padding: 4px 8px; '
            f'color: {p["current"]}; {font} font-weight: 600;">{label}</span>')

def crumb_engagement(name: str, current: bool = False, open_: bool = False, p: dict = DARK) -> str:
    """The engagement's name is a link to its record; the chevron beside it is
    the switcher. One control, two targets, drawn as a split so neither hides
    the other."""
    color = p["current"] if current else p["link"]
    bg = f"background: {p['open_bg']};" if open_ else ""
    return (f'<span style="display: inline-flex; align-items: center; border-radius: 6px; {bg}">'
            f'<a href="#" class="ell" style="display: inline-flex; align-items: center; max-width: 280px; padding: 4px 4px 4px 8px; border-radius: 6px 0 0 6px; color: {color}; font-size: 13px; font-weight: 600;">{name}</a>'
            f'<a href="#" aria-label="Switch engagement" style="display: inline-flex; align-items: center; padding: 6px 6px 6px 2px; border-radius: 0 6px 6px 0; color: {p["chev"]};">{ic("chev_down", 13, width=2.4)}</a>'
            f'</span>')

def wordmark() -> str:
    return f'<strong style="color: {C["on_dark"]}; font-size: 14px; font-weight: 700; letter-spacing: -0.01em; white-space: nowrap;">Audit Workbench</strong>'

def hbtn(label: str | None, icon: str, kind: str = "ghost", aria: str | None = None) -> str:
    if kind == "primary":
        style = f"background: {C['teal']}; border: 1px solid {C['teal']}; color: {C['on_dark']};"
    elif kind == "pressed":
        style = f"background: {C['teal_600']}; border: 1px solid {C['teal_600']}; color: {C['on_dark']};"
    elif kind == "attention":
        style = f"background: {C['warn']}; border: 1px solid {C['warn']}; color: {C['on_dark']};"
    else:
        style = f"background: rgba(255,255,255,0.09); border: 1px solid rgba(255,255,255,0.18); color: {C['on_dark']};"
    pad = "0 11px 0 9px" if label else "0"
    width = "" if label else "width: 30px;"
    text = f'<span>{label}</span>' if label else ""
    return (f'<a href="#" {f"aria-label=\"{aria}\"" if aria else ""} style="display: inline-flex; align-items: center; justify-content: center; gap: 6px; height: 30px; {width} padding: {pad}; '
            f'border-radius: 7px; {style} font-size: 12.8px; font-weight: 600; white-space: nowrap;">{ic(icon, 14)}{text}</a>')

def hkebab() -> str:
    return (f'<a href="#" aria-label="More" style="display: inline-grid; place-items: center; width: 30px; height: 30px; border-radius: 7px; '
            f'background: rgba(255,255,255,0.09); border: 1px solid rgba(255,255,255,0.18); color: {C["on_dark"]};">{kebab_icon(16)}</a>')

def header(trail: list[str], right: list[str], width: int = 1440, show_wordmark: bool = False) -> str:
    """trail: rendered crumb pieces after the brand mark. right: rendered buttons."""
    parts = [brand_mark()]
    if show_wordmark:
        parts.append(wordmark())
    for piece in trail:
        parts.append(crumb_sep())
        parts.append(piece)
    left = "".join(parts)
    return (f'<div style="display: flex; align-items: center; gap: 4px; height: 44px; flex: 0 0 auto; padding: 0 16px; width: {width}px; box-sizing: border-box; '
            f'background: linear-gradient(180deg, {C["navy_900"]} 0%, {C["navy_950"]} 100%); color: {C["on_dark"]}; box-shadow: 0 1px 2px rgba(13,35,64,0.06), 0 1px 3px rgba(13,35,64,0.04);">'
            f'<div style="display: flex; align-items: center; gap: 4px; min-width: 0; flex: 1;">{left}</div>'
            f'<div style="display: flex; align-items: center; gap: 8px; flex: 0 0 auto;">{"".join(right)}</div>'
            f'</div>')

RIGHT_WORKSPACE = [hbtn("Assistant", "sparkles", "primary"), hbtn("Import", "upload"), hkebab()]
RIGHT_INDEX = [hkebab()]

# --- Page primitives (the fieldwork system) -----------------------------------
def btn(label: str, kind: str = "secondary", icon: str | None = None, caret: bool = False) -> str:
    if kind == "primary":
        style = f"padding: 6px 14px; border-radius: 8px; background: {C['teal']}; color: #ffffff;"
    elif kind == "warn":
        style = f"padding: 6px 14px; border-radius: 8px; background: {C['warn']}; color: #ffffff;"
    else:
        style = f"padding: 6px 12px; border: 1px solid {C['border_strong']}; border-radius: 8px; color: {C['ink_soft']}; background: {C['panel']};"
    parts = [ic(icon, 13) if icon else "", label, ic("chev_down", 12, width=2.5) if caret else ""]
    inner = "".join(p for p in parts if p)
    return (f'<a href="#" style="display: inline-flex; align-items: center; gap: 6px; {style} '
            f'font-size: 12.8px; font-weight: 600; white-space: nowrap;">{inner}</a>')

def kebab() -> str:
    return (f'<span style="display: inline-grid; place-items: center; width: 30px; height: 30px; border: 1px solid {C["border_strong"]}; '
            f'border-radius: 8px; color: {C["ink_soft"]}; background: {C["panel"]};">{kebab_icon()}</span>')

def page_header(title: str, count: str, actions: list[str]) -> str:
    return f'''
    <div style="display: flex; align-items: center; gap: 12px; height: 36px;">
      <div style="display: flex; align-items: baseline; gap: 12px; min-width: 0;">
        <h1 style="margin: 0; color: {C['ink_strong']}; font-size: 21.6px; font-weight: 700; letter-spacing: -0.01em; white-space: nowrap;">{title}</h1>
        <span class="num ell" style="color: {C['muted']}; font-size: 12.8px;">{count}</span>
      </div>
      <span style="flex: 1;"></span>
      {" ".join(actions)}
    </div>'''

TONES = {
    "all": (C["teal"], C["teal_soft"], C["teal_strong"]),
    "bad": (C["danger_line"], C["danger_soft"], C["danger_ink"]),
    "warn": (C["warn_line"], C["warn_soft"], C["warn_ink"]),
    "ok": (C["ok_line"], C["ok_soft"], C["ok"]),
    "agent": (C["accent_line"], C["accent_soft"], C["accent"]),
    "neutral": (C["border"], C["panel"], C["ink_soft"]),
    "info": (C["info_line"], C["info_soft"], C["info"]),
}

def chip(count: int | str, label: str, tone: str = "neutral", pressed: bool = False) -> str:
    line, fill, text = TONES["all" if pressed else tone]
    count_color = C["ink_strong"] if tone == "neutral" and not pressed else text
    return (f'<span style="display: inline-flex; align-items: center; gap: 6px; padding: 4px 11px; border: 1px solid {line}; '
            f'border-radius: 999px; background: {fill}; color: {text}; font-size: 12.8px; font-weight: 600;">'
            f'<b class="num" style="color: {count_color};">{count}</b>{label}</span>')

def meter(label: str, value: str, segments: list[tuple[str, float]]) -> str:
    tone_hex = {"ok": C["ok"], "warn": C["warn"], "bad": C["danger"], "neutral": C["border_strong"]}
    stops, at = [], 0.0
    for tone, pct in segments:
        stops.append(f"{tone_hex[tone]} {at:.0f}% {at + pct:.0f}%")
        at += pct
    stops.append(f"{C['border']} {at:.0f}% 100%")
    bg = f"linear-gradient(90deg, {', '.join(stops)})"
    return (f'<div style="display: flex; flex-direction: column; gap: 4px;">'
            f'<span class="num" style="color: {C["muted"]}; font-size: 11px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase;">{label} <b style="color: {C["ink_strong"]};">{value}</b></span>'
            f'<span style="display: block; width: 64px; height: 4px; border-radius: 2px; background: {bg};"></span></div>')

def review_bar(chips: list[str], meters: list[str], settle: str = "") -> str:
    return f'''
    <div style="display: flex; align-items: center; gap: 8px; padding: 10px 14px; border: 1px solid {C['border']}; border-radius: 12px; background: {C['panel']};">
      {" ".join(chips)}{settle}
      <span style="flex: 1;"></span>
      <div style="display: flex; align-items: center; gap: 18px;">{"".join(meters)}</div>
    </div>'''

def settle_button(label: str) -> str:
    return (f'<a href="#" style="display: inline-flex; align-items: center; padding: 4px 10px; border: 1px solid {C["border_strong"]}; '
            f'border-radius: 8px; color: {C["ink_soft"]}; font-size: 11.5px; font-weight: 600; white-space: nowrap;">{label}</a>')

DOT = {"ok": C["ok"], "warn": C["warn"], "bad": C["danger"], "neutral": C["border_strong"], "high": "#d97706", "critical": C["danger_ink"], "medium": "#eab308"}

def dot(tone: str, size: int = 9) -> str:
    return f'<span style="width: {size}px; height: {size}px; flex: 0 0 auto; border-radius: 50%; background: {DOT[tone]};"></span>'

def eyebrow(text: str) -> str:
    return f'<span style="color: {C["muted"]}; font-size: 11px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;">{text}</span>'

def page(content: str, pad: str = "16px 24px 24px", gap: int = 12) -> str:
    return f'<div style="display: flex; flex-direction: column; gap: {gap}px; padding: {pad}; flex: 1;">{content}</div>'

# --- The RCM page under the new header (Main) ----------------------------------
def rcm_grid() -> str:
    cols = "grid-template-columns: 96px minmax(0, 2.2fr) minmax(0, 2fr) 120px 120px 80px 96px 28px;"
    def hcell(t: str, right: bool = False) -> str:
        return f'<span style="font-family: {MONO}; font-size: 10.5px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; color: {C["muted"]}; {"text-align: right;" if right else ""}">{t}</span>'
    head = (f'<div style="display: grid; {cols} gap: 12px; align-items: center; padding: 8px 14px; background: {C["raised"]}; border-bottom: 1px solid {C["border"]};">'
            + "".join(hcell(t) for t in ("Risk", "Statement", "Control", "Tests", "Conclusion", "Finding", "Review", "")) + "</div>")
    def group(name: str, n: int) -> str:
        return (f'<div style="display: flex; align-items: center; gap: 8px; padding: 7px 14px; background: {C["canvas"]}; border-top: 1px solid {C["border"]};">'
                f'{ic("chev_down", 12, C["muted"], 2.5)}<span style="color: {C["ink_strong"]}; font-size: 12.8px; font-weight: 600;">{name}</span>'
                f'<span class="num" style="color: {C["muted"]}; font-size: 11.5px;">{n} risks</span></div>')
    def row(rid: str, rating: str, statement: str, control: str) -> str:
        tone = {"High": "high", "Medium": "medium", "Critical": "critical"}[rating]
        return (f'<div style="display: grid; {cols} gap: 12px; align-items: center; padding: 9px 14px; border-top: 1px solid {C["border"]}; background: {C["panel"]};">'
                f'<div style="display: flex; flex-direction: column; gap: 3px;"><span style="font-family: {MONO}; font-size: 12px; font-weight: 600; color: {C["ink_strong"]};">{rid}</span>'
                f'<span style="display: inline-flex; align-items: center; gap: 5px; font-size: 11px; font-weight: 600; color: {C["ink_soft"]};">{dot(tone, 7)}{rating}</span></div>'
                f'<span style="font-size: 12.5px; color: {C["ink"]}; line-height: 1.4; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;">{statement}</span>'
                f'<span style="font-size: 12.5px; color: {C["ink_soft"]}; line-height: 1.4; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;">{control}</span>'
                f'<span class="num" style="font-family: {MONO}; font-size: 11.5px; color: {C["muted"]};">0 tests · 0 exc</span>'
                f'<span style="font-size: 12px; font-weight: 600; color: {C["ink_soft"]};">No conclusion</span>'
                f'<span style="color: {C["border_strong"]};">—</span>'
                f'<span style="display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: {C["muted"]};">{dot("neutral", 7)}Draft</span>'
                f'{ic("chev_right", 14, C["border_strong"])}</div>')
    rows = [
        ("F6F754", "High", "Purchase requisitions may be approved by officials below the required delegation level, or without any approval.",
         "The designated Financial Authority approves the committed value against the Financial Approval Matrix before a requisition proceeds."),
        ("8A7270", "High", "Purchase requisitions may be approved after the event or after the transaction has been initiated.", "No control identified"),
        ("3445BA", "Medium", "Purchase requisitions may be split into smaller transactions to circumvent approval limits.", "No control identified"),
        ("B537D9", "High", "Purchase requisitions may be raised for goods or services without a genuine business need, or without budget.", "No control identified"),
        ("899104", "Medium", "Requisition details such as quantity, estimated cost and vendor may be recorded inaccurately or incompletely.",
         "The Procurement Team reviews the requisition for completeness and records its verification."),
        ("3898AE", "High", "The same individual may initiate, verify and approve a requisition, or approve a requisition they raised.",
         "The procurement approver authorises the requisition; the approver must be a person other than the requester."),
    ]
    body = head + group("Requisition and approval", 8) + "".join(row(*r) for r in rows) + group("Cross-cutting control environment and monitoring", 4)
    return (f'<div style="display: flex; flex-direction: column; border: 1px solid {C["border"]}; border-radius: 12px; background: {C["panel"]}; overflow: hidden;">{body}</div>')

def rcm_page() -> str:
    hdr = page_header("Risk and control matrix", "32 risks · 8 controls · 0 tests · none reviewed",
                      [btn("Add risk", icon="plus"), btn("Run tests", caret=True, icon="play"), btn("Review 32 rows", "warn"), kebab()])
    bar = review_bar(
        [chip(32, "All rows", pressed=True), chip(32, "Unreviewed", "warn"), chip(24, "No control", "bad"), chip(32, "No tests")],
        [meter("Run", "0", [("neutral", 0)]), meter("Concluded", "0/32", [("neutral", 0)]), meter("Findings", "0", [("neutral", 0)])],
        settle_button("Mark 32 reviewed"),
    )
    return page(hdr + bar + rcm_grid())

def artboard_main() -> str:
    trail = [crumb_engagement("Procurement"), crumb_current("Risk and control matrix")]
    return frame(header(trail, RIGHT_WORKSPACE) + rcm_page(), 780)

# --- Header states -------------------------------------------------------------
def labelled(label: str, note: str, strip: str, width: int = 1440) -> str:
    return (f'<div style="display: flex; flex-direction: column; gap: 8px;">'
            f'<div style="display: flex; align-items: baseline; gap: 10px; padding: 0 2px;">'
            f'<span style="color: {C["ink_strong"]}; font-size: 13px; font-weight: 700;">{label}</span>'
            f'<span style="color: {C["muted"]}; font-size: 12px;">{note}</span></div>'
            f'<div style="width: {width}px; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 2px rgba(13,35,64,0.08);">{strip}</div></div>')

def menu(items: list[tuple[str, str, str]], width: int = 260, header_text: str = "") -> str:
    """items: (kind, icon-or-empty, label). kind: item | sep | head | active."""
    out = []
    if header_text:
        out.append(f'<div style="padding: 8px 12px 6px; color: {C["muted"]}; font-size: 11px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;">{header_text}</div>')
    for kind, icon, label in items:
        if kind == "sep":
            out.append(f'<div style="height: 1px; margin: 4px 0; background: {C["border"]};"></div>')
            continue
        active = kind == "active"
        bg = f"background: {C['teal_soft']}; color: {C['teal']};" if active else f"color: {C['ink']};"
        out.append(f'<div style="display: flex; align-items: center; gap: 10px; padding: 7px 12px; border-radius: 6px; {bg} font-size: 13px; font-weight: {600 if active else 500};">'
                   f'{ic(icon, 14, C["teal"] if active else C["ink_soft"]) if icon else "<span style=\"width: 14px;\"></span>"}<span style="flex: 1;">{label}</span></div>')
    return (f'<div style="width: {width}px; padding: 6px; border: 1px solid {C["border"]}; border-radius: 10px; background: {C["panel"]}; '
            f'box-shadow: 0 2px 6px rgba(13,35,64,0.07), 0 10px 24px rgba(13,35,64,0.06);">{"".join(out)}</div>')

def strip_mini(states: tuple[str, str, str, str]) -> str:
    color = {"complete": C["ok"], "attention": C["warn"], "not_started": C["border_strong"], "in_progress": None}
    segs = []
    for s in states:
        if s == "in_progress":
            bg = f"background: repeating-linear-gradient(90deg, {C['info']} 0 5px, transparent 5px 8px), {C['border_strong']};"
        else:
            bg = f"background: {color[s]};"
        segs.append(f'<span style="flex: 1; height: 3px; border-radius: 2px; {bg}"></span>')
    return f'<span style="display: flex; gap: 3px; width: 64px;">{"".join(segs)}</span>'

def switcher_menu() -> str:
    def entry(name: str, states, active=False):
        bg = f"background: {C['teal_soft']};" if active else ""
        return (f'<div style="display: flex; align-items: center; gap: 10px; padding: 7px 10px; border-radius: 6px; {bg}">'
                f'{ic("briefcase", 14, C["teal"] if active else C["ink_soft"])}<span style="flex: 1; color: {C["ink_strong"] if active else C["ink"]}; font-size: 13px; font-weight: {600 if active else 500};">{name}</span>{strip_mini(states)}</div>')
    body = (f'<div style="padding: 8px 10px 4px; color: {C["muted"]}; font-size: 11px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;">Engagements</div>'
            + entry("Procurement", ("complete", "in_progress", "attention", "attention"), True)
            + entry("Treasury", ("complete", "in_progress", "attention", "attention"))
            + entry("TreasuryFull", ("complete", "in_progress", "attention", "not_started"))
            + entry("Expenses", ("complete", "not_started", "not_started", "not_started"))
            + f'<div style="height: 1px; margin: 4px 0; background: {C["border"]};"></div>'
            + f'<div style="display: flex; align-items: center; gap: 10px; padding: 7px 10px; color: {C["ink"]}; font-size: 13px; font-weight: 500;">{ic("grid", 14, C["ink_soft"])}<span style="flex: 1;">All engagements</span></div>'
            + f'<div style="display: flex; align-items: center; gap: 10px; padding: 7px 10px; color: {C["teal"]}; font-size: 13px; font-weight: 600;">{ic("plus", 14, C["teal"])}<span style="flex: 1;">New engagement</span></div>')
    return (f'<div style="width: 300px; padding: 6px; border: 1px solid {C["border"]}; border-radius: 10px; background: {C["panel"]}; '
            f'box-shadow: 0 2px 6px rgba(13,35,64,0.07), 0 10px 24px rgba(13,35,64,0.06);">{body}</div>')

def overflow_menu() -> str:
    return menu([
        ("item", "zoom", "Presentation size"),
        ("item", "monitor", "Theme · System"),
        ("sep", "", ""),
        ("item", "code", "Diagnostics"),
        ("item", "info", "About Audit Workbench"),
        ("sep", "", ""),
        ("head", "", "Signed in as [email]"),
        ("item", "signout", "Sign out"),
    ], width=248)

def artboard_header_states() -> str:
    rows = []
    rows.append(labelled("Index", "Brand mark and wordmark; the wordmark appears only where there is no trail. Right: the kebab (theme, size, about, sign out).",
                         header([], RIGHT_INDEX, show_wordmark=True)))
    rows.append(labelled("Engagement record", "The engagement is the trail's first and only piece. Its name opens the record; the chevron opens the switcher.",
                         header([crumb_engagement("Procurement", current=True)], RIGHT_WORKSPACE)))
    rows.append(labelled("Work product", "What the crumb bar said, now in the header. The engagement crumb is the way back — the row the bar spent on `← Engagement record` goes.",
                         header([crumb_engagement("Procurement"), crumb_current("Risk and control matrix")], RIGHT_WORKSPACE)))
    rows.append(labelled("RCM row page", "Three pieces. Ids stay mono, as the row page draws them.",
                         header([crumb_engagement("Procurement"), crumb_link("Risk and control matrix"), crumb_current("F6F754", mono=True)], RIGHT_WORKSPACE)))
    rows.append(labelled("Assistant open, run needs the auditor", "The toggle reads pressed while the panel is open, amber while a run awaits approval, input or has failed — as the built toggle does today.",
                         header([crumb_engagement("Procurement"), crumb_current("Test programme")],
                                [hbtn("Assistant · needs you", "sparkles", "attention"), hbtn("Import", "upload"), hkebab()])))
    rows.append(labelled("Diagnostics", "Keeps the same header; its own 70 px band with `← Engagement | LOCAL DIAGNOSTICS` goes, and Live / Clear join the page header row.",
                         header([crumb_engagement("Procurement"), crumb_current("Diagnostics")], RIGHT_WORKSPACE)))
    # switcher + kebab open, drawn as a header with two popovers beneath
    open_hdr = header([crumb_engagement("Procurement", open_=True), crumb_current("Findings register")], RIGHT_WORKSPACE)
    popovers = (f'<div style="position: relative; height: 330px;">'
                f'<div style="position: absolute; left: 46px; top: 6px;">{switcher_menu()}</div>'
                f'<div style="position: absolute; right: 16px; top: 6px;">{overflow_menu()}</div></div>')
    rows.append(labelled("The two menus", "Left: the switcher under the engagement crumb, each entry with the index's strip. Right: the kebab, which holds everything the right cluster used to spell out as eight icons.",
                         f'<div style="background: {C["canvas"]};">{open_hdr}{popovers}</div>'))
    # narrow
    narrow = header([crumb_engagement("Procurement"), crumb_current("Risk and control matrix")],
                    [hbtn(None, "sparkles", "primary", aria="Assistant"), hbtn(None, "upload", aria="Import"), hkebab()], width=1180)
    rows.append(labelled("Under 1,280 px", "Button labels drop to icons; the trail truncates its middle piece first, the current one last. Nothing wraps and nothing is lost.",
                         narrow, width=1180))
    body = f'<div style="display: flex; flex-direction: column; gap: 26px; padding: 24px;">{"".join(rows)}</div>'
    return frame(body, 1220)

# --- Alternate: a light header -------------------------------------------------
def header_light(trail: list[str], right: list[str]) -> str:
    left = brand_mark() + "".join(crumb_sep(LIGHT) + t for t in trail)
    return (f'<div style="display: flex; align-items: center; gap: 4px; height: 44px; flex: 0 0 auto; padding: 0 16px; '
            f'background: {C["panel"]}; border-bottom: 1px solid {C["border"]}; color: {C["ink_strong"]};">'
            f'<div style="display: flex; align-items: center; gap: 4px; min-width: 0; flex: 1;">{left}</div>'
            f'<div style="display: flex; align-items: center; gap: 8px; flex: 0 0 auto;">{"".join(right)}</div></div>')

def artboard_header_light() -> str:
    trail = [crumb_engagement("Procurement", p=LIGHT), crumb_current("Risk and control matrix", p=LIGHT)]
    hdr = header_light(trail, [btn("Assistant", "primary", icon="sparkles"), btn("Import", icon="upload"), kebab()])
    content = page(page_header("Risk and control matrix", "32 risks · 8 controls · 0 tests · none reviewed",
                               [btn("Add risk", icon="plus"), btn("Run tests", caret=True, icon="play"), btn("Review 32 rows", "warn"), kebab()])
                   + review_bar([chip(32, "All rows", pressed=True), chip(32, "Unreviewed", "warn"), chip(24, "No control", "bad"), chip(32, "No tests")],
                                [meter("Run", "0", [("neutral", 0)]), meter("Concluded", "0/32", [("neutral", 0)]), meter("Findings", "0", [("neutral", 0)])],
                                settle_button("Mark 32 reviewed")))
    return frame(hdr + content, 260)

# --- The engagements index -----------------------------------------------------
STATE_LABEL = {"complete": "Complete", "in_progress": "In progress", "attention": "Needs attention", "not_started": "Not started"}

def strip(states: dict[str, str], tables: int) -> str:
    segs = [("Data", "complete" if tables else "not_started"), ("Planning", states["planning"]), ("Fieldwork", states["fieldwork"]), ("Report", states["report"])]
    out = []
    for label, s in segs:
        if s == "in_progress":
            bg = f"background: repeating-linear-gradient(90deg, {C['info']} 0 7px, transparent 7px 11px), {C['border_strong']};"
        else:
            bg = f"background: {dict(complete=C['ok'], attention=C['warn'], not_started=C['border_strong'])[s]};"
        lc = C["warn_ink"] if s == "attention" else C["muted"]
        out.append(f'<span title="{label}: {STATE_LABEL[s]}" style="display: flex; flex-direction: column; gap: 4px; min-width: 0;">'
                   f'<i style="display: block; height: 4px; border-radius: 999px; {bg}"></i>'
                   f'<small class="ell" style="color: {lc}; font-size: 10.9px; font-weight: 600;">{label}</small></span>')
    return f'<div style="display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 6px; width: 228px;">{"".join(out)}</div>'

ENGAGEMENTS = [
    # name, states, tables, documents, next (action, message, tone), open points, last activity, runs, elapsed
    dict(name="Procurement", states=dict(planning="in_progress", fieldwork="attention", report="attention"), tables=18, documents=8,
         next=("Add causes", "18 of 18 findings have no root cause or management response."), points=2,
         last="5 Sep, 05:28", runs="17 runs · 39 min"),
    dict(name="TreasuryFull", states=dict(planning="in_progress", fieldwork="attention", report="not_started"), tables=25, documents=84,
         next=("Open them", "29 of 65 conclusions were set by the assistant and never read."), points=2,
         last="5 Sep, 05:32", runs="21 runs · 1 h 8 min"),
    dict(name="Treasury", states=dict(planning="in_progress", fieldwork="attention", report="attention"), tables=3, documents=9,
         next=("Add causes", "6 of 6 findings have no root cause or management response."), points=2,
         last="4 Sep, 22:02", runs="20 runs · 30 min"),
    dict(name="Expenses", states=dict(planning="not_started", fieldwork="not_started", report="not_started"), tables=7, documents=14,
         next=("Run", "Analyse the imported documents"), points=0,
         last="5 Sep, 16:38", runs="1 run · 2 min"),
]

INDEX_COLS = "grid-template-columns: minmax(200px, 1.2fr) 228px minmax(260px, 2fr) 140px 150px 30px;"

def index_head() -> str:
    def h(t: str) -> str:
        return f'<span style="font-family: {MONO}; font-size: 10.5px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; color: {C["muted"]};">{t}</span>'
    return (f'<div style="display: grid; {INDEX_COLS} gap: 20px; align-items: center; padding: 9px 16px; background: {C["raised"]}; border-bottom: 1px solid {C["border"]};">'
            + "".join(h(t) for t in ("Engagement", "Where it stands", "Next", "Sources", "Last activity", "")) + "</div>")

def index_row(e: dict, first: bool = False) -> str:
    action, message = e["next"]
    attention = any(s == "attention" for s in e["states"].values())
    points = (f'<span class="num" style="display: inline-flex; align-items: center; gap: 5px; padding: 2px 8px; border: 1px solid {C["warn_line"]}; border-radius: 999px; '
              f'background: {C["warn_soft"]}; color: {C["warn_ink"]}; font-size: 11px; font-weight: 600; white-space: nowrap;">{e["points"]} open points</span>') if e["points"] else ""
    action_color = C["warn_ink"] if attention else C["teal"]
    next_cell = (f'<div style="display: flex; flex-direction: column; gap: 3px; min-width: 0;">'
                 f'<span class="ell" style="font-size: 12.8px; color: {C["ink"]};"><a href="#" style="color: {action_color}; font-weight: 600;">{action}</a> · {message}</span>'
                 f'<span style="display: flex; align-items: center; gap: 6px;">{points}</span></div>') if e["points"] else \
                (f'<div style="display: flex; flex-direction: column; gap: 3px; min-width: 0;">'
                 f'<span class="ell" style="font-size: 12.8px; color: {C["ink"]};"><a href="#" style="color: {action_color}; font-weight: 600;">{action}</a> · {message}</span>'
                 f'<span style="color: {C["muted"]}; font-size: 11px;">Nothing is blocking it</span></div>')
    name_cell = (f'<div style="display: flex; align-items: center; gap: 10px; min-width: 0;">'
                 f'<span style="display: grid; place-items: center; width: 28px; height: 28px; flex: 0 0 auto; border-radius: 7px; background: {C["teal_soft"]}; color: {C["teal"]};">{ic("briefcase", 14)}</span>'
                 f'<a href="#" class="ell" style="color: {C["ink_strong"]}; font-size: 14px; font-weight: 600;">{e["name"]}</a></div>')
    sources = (f'<div style="display: flex; flex-direction: column; gap: 3px;">'
               f'<span class="num" style="font-size: 12.5px; color: {C["ink"]};">{e["documents"]} documents</span>'
               f'<span class="num" style="font-size: 12.5px; color: {C["ink"]};">{e["tables"]} tables</span></div>')
    last = (f'<div style="display: flex; flex-direction: column; gap: 3px;">'
            f'<span class="num" style="font-size: 12.5px; color: {C["ink"]};">{e["last"]}</span>'
            f'<span class="num" style="font-size: 11px; color: {C["muted"]};">{e["runs"]}</span></div>')
    border = "" if first else f"border-top: 1px solid {C['border']};"
    return (f'<div style="display: grid; {INDEX_COLS} gap: 20px; align-items: center; padding: 12px 16px; {border} background: {C["panel"]};">'
            f'{name_cell}{strip(e["states"], e["tables"])}{next_cell}{sources}{last}{kebab()}</div>')

def index_page() -> str:
    hdr = page_header("Engagements", "4 engagements · 3 need attention · 1 not started", [btn("New engagement", "primary", icon="plus")])
    rows = index_head() + "".join(index_row(e, i == 0) for i, e in enumerate(ENGAGEMENTS))
    table = f'<div style="display: flex; flex-direction: column; border: 1px solid {C["border"]}; border-radius: 12px; background: {C["panel"]}; overflow: hidden;">{rows}</div>'
    note = (f'<span style="color: {C["muted"]}; font-size: 12px;">Engagements that need attention are listed first, then by last activity. '
            f'A search field joins the table head past eight engagements.</span>')
    return page(hdr + table + note, pad="20px 32px 28px")

def artboard_engagements() -> str:
    return frame(header([], RIGHT_INDEX, show_wordmark=True) + index_page(), 520)

def artboard_engagements_empty() -> str:
    hdr = page_header("Engagements", "", [])
    card = (f'<div style="display: flex; flex-direction: column; align-items: center; gap: 10px; padding: 56px 24px; border: 1px dashed {C["border_strong"]}; border-radius: 12px; background: {C["panel"]}; text-align: center;">'
            f'<span style="display: grid; place-items: center; width: 44px; height: 44px; border-radius: 10px; background: {C["teal_soft"]}; color: {C["teal"]};">{ic("folder", 22)}</span>'
            f'<h3 style="margin: 6px 0 0; color: {C["ink_strong"]}; font-size: 17.6px; font-weight: 600;">Start your first engagement</h3>'
            f'<p style="max-width: 52ch; margin: 0; color: {C["ink_soft"]}; font-size: 14px; line-height: 1.5;">An engagement holds the audit file — the planning memorandum, the risk and control matrix, the tests and their evidence, and the report they support. Name it, point it at the audit folder, and the assistant proposes the plan before it changes anything.</p>'
            f'<div style="margin-top: 8px;">{btn("New engagement", "primary", icon="plus")}</div>'
            f'<a href="#" style="display: inline-flex; align-items: center; gap: 4px; margin-top: 6px; color: {C["teal"]}; font-size: 12.8px;">Why this exists {ic("external", 11)}</a></div>')
    return frame(header([], RIGHT_INDEX, show_wordmark=True) + page(hdr + card, pad="20px 32px 28px"), 440)

# --- Alternate: compact cards --------------------------------------------------
def card(e: dict) -> str:
    action, message = e["next"]
    attention = any(s == "attention" for s in e["states"].values())
    action_color = C["warn_ink"] if attention else C["teal"]
    points = (f'<span class="num" style="display: inline-flex; padding: 2px 8px; border: 1px solid {C["warn_line"]}; border-radius: 999px; background: {C["warn_soft"]}; color: {C["warn_ink"]}; font-size: 11px; font-weight: 600;">{e["points"]} open points</span>'
              if e["points"] else f'<span style="color: {C["muted"]}; font-size: 11px;">Nothing blocking</span>')
    return (f'<div style="display: flex; flex-direction: column; gap: 12px; padding: 14px 16px; border: 1px solid {C["border"]}; border-radius: 12px; background: {C["panel"]};">'
            f'<div style="display: flex; align-items: center; gap: 10px;">'
            f'<span style="display: grid; place-items: center; width: 28px; height: 28px; border-radius: 7px; background: {C["teal_soft"]}; color: {C["teal"]};">{ic("briefcase", 14)}</span>'
            f'<a href="#" class="ell" style="flex: 1; color: {C["ink_strong"]}; font-size: 14.5px; font-weight: 600;">{e["name"]}</a>{kebab()}</div>'
            f'<p style="margin: 0; font-size: 12.8px; line-height: 1.45; color: {C["ink"]}; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;"><a href="#" style="color: {action_color}; font-weight: 600;">{action}</a> · {message}</p>'
            f'<div style="display: flex; flex-direction: column; gap: 10px;">{strip(e["states"], e["tables"])}<span>{points}</span></div>'
            f'<div class="num" style="display: flex; justify-content: space-between; padding-top: 10px; border-top: 1px solid {C["border"]}; color: {C["muted"]}; font-size: 11.5px;">'
            f'<span>{e["documents"]} documents · {e["tables"]} tables</span><span>{e["last"]}</span></div></div>')

def artboard_engagements_cards() -> str:
    hdr = page_header("Engagements", "4 engagements · 3 need attention · 1 not started", [btn("New engagement", "primary", icon="plus")])
    grid = f'<div style="display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px;">{"".join(card(e) for e in ENGAGEMENTS)}</div>'
    return frame(header([], RIGHT_INDEX, show_wordmark=True) + page(hdr + grid, pad="20px 32px 28px"), 440)

# --- Canvas ------------------------------------------------------------------------
ARTBOARDS = [
    ("Main.dc.html", "Header on a work product", artboard_main, 0, 0, 1440, 780),
    ("Engagements.dc.html", "Engagements index", artboard_engagements, 1560, 0, 1440, 520),
    ("EngagementsEmpty.dc.html", "Engagements index, empty", artboard_engagements_empty, 1560, 640, 1440, 440),
    ("HeaderStates.dc.html", "Header states", artboard_header_states, 0, 900, 1440, 1220),
    ("EngagementsCards.dc.html", "Alternate: compact cards", artboard_engagements_cards, 1560, 1200, 1440, 440),
    ("HeaderLight.dc.html", "Alternate: light header", artboard_header_light, 0, 2240, 1440, 260),
]

def main() -> None:
    for file, _title, fn, *_ in ARTBOARDS:
        (HERE / file).write_text(fn(), encoding="utf-8")
    manifest = {
        "artboards": [
            {"file": f, "title": t, "x": x, "y": y, "w": w, "h": h}
            for f, t, _fn, x, y, w, h in ARTBOARDS
        ],
        "annotations": [
            {"id": "note-cards", "x": 1560, "y": 1760, "w": 420,
             "text": "Alternate direction for the index: cards, 4-up. Same fields as the list. Reads well up to six engagements; past that the list's aligned columns win, and this is the one place the number of engagements is expected to grow."},
            {"id": "note-light", "x": 0, "y": 2600, "w": 420,
             "text": "Alternate direction for the header: the panel colour with a bottom rule instead of navy. The shell becomes one surface with the page and the crumb reads in ink. Trade: the brand anchor is weaker, and the header stops separating chrome from content at a glance, which the navy does for free."},
        ],
        "launch": {"view": "canvas"},
    }
    (HERE / "canvas.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(ARTBOARDS)} artboards and canvas.json to {HERE}")

if __name__ == "__main__":
    main()
