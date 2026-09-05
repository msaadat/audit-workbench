#!/usr/bin/env python3
"""Generate the artboards for the sources, planning and reporting redesign.

One function per artboard, sharing the primitives of the fieldwork system
(page header, review bar, list rows, verdict bar, section labels). Every value
is a literal hex or px so the markup answers "what size is that" by itself;
the token map in the redesign document says what to write in the components.

Run from this directory:  python3 gen_views.py
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
    teal="#0f766e", teal_strong="#0b625c", teal_soft="#e7f7f4", teal_line="#a7ded8",
    accent="#7c3aed", accent_soft="#f5f0ff", accent_line="#d9ccf5",
    ok="#147d55", ok_soft="#e5f6ee", ok_line="#9fd9be",
    warn="#b45309", warn_ink="#8a4308", warn_soft="#fdf1e3", warn_line="#f0cf9f",
    danger="#b42318", danger_ink="#7f1d1d", danger_soft="#fdecea", danger_line="#f2b8b2",
    info="#1d4ed8", info_soft="#eff6ff", info_line="#bfdbfe",
    low="#eab308", low_ink="#854d0e", low_soft="#fefce8",
    high="#d97706",
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
    "chev_up": '<path d="m18 15-6-6-6 6"></path>',
    "file": '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"></path><path d="M14 3v6h6"></path>',
    "table": '<rect x="3" y="4" width="18" height="16" rx="2"></rect><path d="M3 10h18"></path><path d="M9 4v16"></path>',
    "link": '<path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1"></path><path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"></path>',
    "map": '<path d="M9 4 3 6v14l6-2 6 2 6-2V4l-6 2z"></path><path d="M9 4v14"></path><path d="M15 6v14"></path>',
    "check": '<path d="M20 6 9 17l-5-5"></path>',
    "warning": '<path d="M12 3 2 20h20z"></path><path d="M12 10v4"></path><path d="M12 17h.01"></path>',
    "eye": '<path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12z"></path><circle cx="12" cy="12" r="3"></circle>',
    "download": '<path d="M12 4v12"></path><path d="m6 10 6 6 6-6"></path><path d="M4 20h16"></path>',
    "upload": '<path d="M12 16V4"></path><path d="m6 10 6-6 6 6"></path><path d="M4 20h16"></path>',
    "pencil": '<path d="M12 20h9"></path><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"></path>',
    "refresh": '<path d="M21 12a9 9 0 1 1-2.6-6.4"></path><path d="M21 3v6h-6"></path>',
    "paperclip": '<path d="m21 11-8.5 8.5a5 5 0 0 1-7-7L14 4a3.3 3.3 0 0 1 4.7 4.7L10.5 17a1.7 1.7 0 0 1-2.4-2.4L16 6.7"></path>',
    "external": '<path d="M14 4h6v6"></path><path d="M20 4 10 14"></path><path d="M18 13v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h5"></path>',
    "flag": '<path d="M4 21V4"></path><path d="M4 4h12l-2 4 2 4H4"></path>',
    "chart": '<path d="M4 20V10"></path><path d="M10 20V4"></path><path d="M16 20v-7"></path><path d="M22 20H2"></path>',
    "clock": '<circle cx="12" cy="12" r="9"></circle><path d="M12 7v5l3 2"></path>',
    "book": '<path d="M4 4h6a3 3 0 0 1 3 3v13a2 2 0 0 0-2-2H4z"></path><path d="M20 4h-6a3 3 0 0 0-3 3v13a2 2 0 0 1 2-2h7z"></path>',
    "grid": '<rect x="3" y="3" width="8" height="8" rx="1"></rect><rect x="13" y="3" width="8" height="8" rx="1"></rect><rect x="3" y="13" width="8" height="8" rx="1"></rect><rect x="13" y="13" width="8" height="8" rx="1"></rect>',
    "settings": '<circle cx="12" cy="12" r="4"></circle><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"></path>',
    "code": '<path d="m8 8-4 4 4 4"></path><path d="m16 8 4 4-4 4"></path>',
    "info": '<circle cx="12" cy="12" r="9"></circle><path d="M12 11v5"></path><path d="M12 8h.01"></path>',
    "back": '<path d="M19 12H5"></path><path d="m11 18-6-6 6-6"></path>',
    "copy": '<rect x="9" y="9" width="12" height="12" rx="2"></rect><path d="M5 15V5a2 2 0 0 1 2-2h10"></path>',
    "shield": '<path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6z"></path>',
    "list": '<path d="M8 6h13"></path><path d="M8 12h13"></path><path d="M8 18h13"></path><path d="M3 6h.01"></path><path d="M3 12h.01"></path><path d="M3 18h.01"></path>',
}

def ic(name: str, size: int = 14, stroke: str = "currentColor", width: float = 2) -> str:
    return svg(ICON[name], size, stroke, width=width)

def kebab_icon() -> str:
    return ('<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" stroke="none">'
            '<circle cx="12" cy="5" r="1.8"></circle><circle cx="12" cy="12" r="1.8"></circle>'
            '<circle cx="12" cy="19" r="1.8"></circle></svg>')

# --- Primitives ---------------------------------------------------------------
def frame(body: str, height: int) -> str:
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
    .clamp2 {{ display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }}
    .ell {{ white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  </style>
</helmet>
<div style="width: 1440px; min-height: {height}px; background: {C['canvas']}; display: flex; flex-direction: column;">
{body}
</div>
</x-dc>
</body>
</html>
'''

def header_bar(assistant: str | None = None, switcher: bool = True) -> str:
    """The navy bar. `assistant` names the panel state the toggle shows:
    None keeps today's Record | Assistant switcher; 'closed', 'docked' or
    'expanded' drops the switcher and draws the toggle in the right cluster."""
    nav_icons = "".join(
        f'<span style="display: inline-flex;">{ic(n, 18)}</span>'
        for n in ("search", "settings", "code", "info", "grid"))
    if switcher and assistant is None:
        middle = (f'<div style="display: flex; align-items: center; gap: 2px; padding: 3px; border-radius: 999px; background: rgba(255,255,255,0.10);">'
                  f'<span style="display: inline-flex; align-items: center; gap: 6px; padding: 6px 14px; border-radius: 999px; background: #ffffff; color: #0d2340; font-size: 13px; font-weight: 600;">{ic("clock")}Record</span>'
                  f'<span style="display: inline-flex; align-items: center; gap: 6px; padding: 6px 14px; border-radius: 999px; color: #c3d2e4; font-size: 13px; font-weight: 600;">{ic("sparkles")}Assistant</span></div>')
        toggle = ""
    else:
        middle = ""
        on = assistant in ("docked", "expanded")
        fill = "background: #0d9488; color: #ffffff; border-color: #0d9488;" if on else "border: 1px solid rgba(255,255,255,0.22); color: #ffffff;"
        live = f'<span style="width: 7px; height: 7px; border-radius: 50%; background: {"#ffffff" if on else "#2dd4bf"}; box-shadow: 0 0 0 2px rgba(45,212,191,0.35);"></span>'
        toggle = (f'<span style="display: inline-flex; align-items: center; gap: 8px; padding: 7px 14px; border-radius: 8px; font-size: 13px; font-weight: 600; {fill}">'
                  f'{ic("sparkles", 15)}Assistant{live}</span>')
    return f'''
  <div style="display: flex; align-items: center; gap: 14px; height: 56px; padding: 0 24px; background: linear-gradient(180deg, #0d2340 0%, #07162b 100%); color: #ffffff;">
    <div style="display: flex; align-items: center; gap: 10px;">
      <div style="display: grid; place-items: center; width: 32px; height: 32px; border-radius: 8px; background: linear-gradient(135deg, #5eead4 0%, #2dd4bf 100%); color: #07162b;">{ic("check", 18, width=2.5)}</div>
      <strong style="font-size: 15.2px; font-weight: 700; letter-spacing: -0.01em;">Audit Workbench</strong>
    </div>
    <div style="width: 1px; height: 28px; background: rgba(255,255,255,0.14);"></div>
    <div style="display: flex; flex-direction: column; line-height: 1.2;">
      <span style="font-size: 11.5px; font-weight: 600; color: #8fa6c2;">Engagement</span>
      <span style="font-size: 15.2px; font-weight: 700;">Procurement</span>
    </div>
    {middle}
    <span style="flex: 1;"></span>
    <span style="display: inline-flex; align-items: center; gap: 8px; padding: 7px 14px; border: 1px solid rgba(255,255,255,0.22); border-radius: 8px; font-size: 13px; font-weight: 600;">{ic("upload", 15)}Import</span>
    {toggle}
    <div style="display: flex; align-items: center; gap: 18px; color: #e6edf6; margin-left: 6px;">{nav_icons}</div>
  </div>'''

def crumb_bar(crumb: str) -> str:
    return f'''
  <div style="display: flex; align-items: center; gap: 10px; height: 44px; padding: 0 24px; border-bottom: 1px solid {C['border']}; background: {C['panel']}; flex: 0 0 auto;">
    {ic("back", 18, C['teal'])}
    <a href="#" style="color: {C['teal']}; font-size: 14px; font-weight: 500;">Engagement record</a>
    <span style="color: {C['border_strong']};">/</span>
    <span style="color: {C['ink_strong']}; font-size: 14px; font-weight: 600;">{crumb}</span>
  </div>'''

def shell(crumb: str, content: str, height: int, surface: str = "Record",
          assistant: str | None = None, panel: str = "") -> str:
    """The app chrome the fieldwork artboards draw: navy bar, breadcrumb, page.

    With `panel`, the page and the docked assistant sit side by side under the
    bar, the page taking what the panel leaves."""
    page = (f'<div style="display: flex; flex-direction: column; flex: 1; min-width: 0;">{crumb_bar(crumb)}'
            f'<div style="display: flex; flex-direction: column; gap: 12px; padding: 16px 24px 24px; flex: 1;">{content}</div></div>')
    body = header_bar(assistant)
    if panel:
        body += f'<div style="display: flex; flex: 1; min-height: 0; align-items: stretch;">{page}{panel}</div>'
    else:
        body += page
    return frame(body, height)

def btn(label: str, kind: str = "secondary", icon: str | None = None, caret: bool = False, icon_size: int = 13) -> str:
    if kind == "primary":
        style = f"padding: 6px 14px; border-radius: 8px; background: {C['teal']}; color: #ffffff;"
    elif kind == "warn":
        style = f"padding: 6px 14px; border-radius: 8px; background: {C['warn']}; color: #ffffff;"
    else:
        style = f"padding: 6px 12px; border: 1px solid {C['border_strong']}; border-radius: 8px; color: {C['ink_soft']}; background: {C['panel']};"
    parts = [ic(icon, icon_size) if icon else "", label, ic("chev_down", 12, width=2.5) if caret else ""]
    inner = "".join(p for p in parts if p)
    return (f'<a href="#" style="display: inline-flex; align-items: center; gap: 6px; {style} '
            f'font-size: 12.8px; font-weight: 600; white-space: nowrap;">{inner}</a>')

def kebab() -> str:
    return (f'<span style="display: inline-grid; place-items: center; width: 30px; height: 30px; border: 1px solid {C["border_strong"]}; '
            f'border-radius: 8px; color: {C["ink_soft"]}; background: {C["panel"]};">{kebab_icon()}</span>')

def page_header(title: str, count: str, actions: list[str]) -> str:
    return f'''
    <div style="display: flex; align-items: center; gap: 12px; height: 36px;">
      <div style="display: flex; align-items: baseline; gap: 12px;">
        <h1 style="margin: 0; color: {C['ink_strong']}; font-size: 21.6px; font-weight: 700; letter-spacing: -0.01em;">{title}</h1>
        <span class="num" style="color: {C['muted']}; font-size: 12.8px;">{count}</span>
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
    """segments: (tone, percent). Remainder paints the border colour."""
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

def review_bar(chips: list[str], meters: list[str], settle: str = "", wrap: bool = False) -> str:
    flow = "flex-wrap: wrap; row-gap: 8px;" if wrap else ""
    return f'''
    <div style="display: flex; align-items: center; gap: 8px; {flow} padding: 10px 14px; border: 1px solid {C['border']}; border-radius: 12px; background: {C['panel']};">
      {" ".join(chips)}{settle}
      <span style="flex: 1;"></span>
      <div style="display: flex; align-items: center; gap: 18px;">{"".join(meters)}</div>
    </div>'''

def settle_button(label: str) -> str:
    return (f'<a href="#" style="display: inline-flex; align-items: center; padding: 4px 10px; border: 1px solid {C["border_strong"]}; '
            f'border-radius: 8px; color: {C["ink_soft"]}; font-size: 11.5px; font-weight: 600; white-space: nowrap;">{label}</a>')

def search_box(placeholder: str) -> str:
    return (f'<span style="display: flex; align-items: center; gap: 8px; flex: 1; padding: 6px 10px; border: 1px solid {C["border_strong"]}; '
            f'border-radius: 8px; color: {C["muted"]}; font-size: 12.8px;">{ic("search")}{placeholder}</span>')

def list_header(search: str, links: list[str]) -> str:
    extra = "".join(f'<a href="#" style="display: inline-flex; align-items: center; gap: 3px; color: {C["ink_soft"]}; font-size: 12px; font-weight: 600; white-space: nowrap;">{l}</a>' for l in links)
    return (f'<div style="display: flex; align-items: center; gap: 10px; padding: 10px 12px; border-bottom: 1px solid {C["border"]};">'
            f'{search_box(search)}{extra}</div>')

def group_head(name: str, sentence: str, open_: bool = True) -> str:
    return (f'<div style="display: flex; align-items: center; gap: 8px; padding: 7px 12px; background: {C["canvas"]}; border-top: 1px solid {C["border"]};">'
            f'{ic("chev_down" if open_ else "chev_right", 12, C["muted"], 2.5)}'
            f'<span style="color: {C["ink_strong"]}; font-size: 12.8px; font-weight: 600;">{name}</span>'
            f'<span class="num" style="color: {C["muted"]}; font-size: 11.5px;">{sentence}</span></div>')

DOT = {"ok": C["ok"], "warn": C["warn"], "bad": C["danger"], "neutral": C["border_strong"], "low": C["low"], "critical": C["danger_ink"], "accent": C["accent"], "info": C["info"]}

def dot(tone: str, size: int = 9) -> str:
    return f'<span style="width: {size}px; height: {size}px; flex: 0 0 auto; border-radius: 50%; background: {DOT[tone]};"></span>'

def list_row(tone: str, title: str, meta: str, active: bool = False, title_mono: bool = False) -> str:
    bg = f"background: {C['teal_soft']}; border-left: 3px solid {C['teal']};" if active else f"border-left: 3px solid transparent; border-top: 1px solid {C['border']};"
    font = f"font-family: {MONO}; font-size: 12px;" if title_mono else "font-size: 13px;"
    weight = 600 if active else 500
    color = C["ink_strong"] if active else C["ink"]
    return (f'<div style="display: flex; align-items: center; gap: 10px; padding: 10px 12px; {bg}">{dot(tone)}'
            f'<div style="display: flex; flex-direction: column; gap: 2px; min-width: 0;">'
            f'<span class="ell" style="color: {color}; {font} font-weight: {weight};">{title}</span>'
            f'<span class="ell" style="color: {C["muted"]}; font-size: 11.5px;">{meta}</span></div></div>')

def list_panel(inner: str) -> str:
    return (f'<div style="display: flex; flex-direction: column; border: 1px solid {C["border"]}; border-radius: 12px; '
            f'background: {C["panel"]}; overflow: hidden; align-self: start;">{inner}</div>')

def detail_panel(inner: str, gap: int = 16, pad: str = "18px 22px") -> str:
    return (f'<div style="display: flex; flex-direction: column; gap: {gap}px; padding: {pad}; border: 1px solid {C["border"]}; '
            f'border-radius: 12px; background: {C["panel"]}; min-width: 0;">{inner}</div>')

def master_detail(list_html: str, detail_html: str) -> str:
    return (f'<div style="display: grid; grid-template-columns: 300px minmax(0, 1fr); gap: 14px; flex: 1; min-height: 0; align-items: start;">'
            f'{list_html}{detail_html}</div>')

def eyebrow(text: str) -> str:
    return f'<span style="color: {C["muted"]}; font-size: 11px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;">{text}</span>'

def mono(text: str, size: float = 11.5, color: str | None = None) -> str:
    col = f"color: {color};" if color else ""
    return f'<span style="font-family: {MONO}; font-size: {size}px; letter-spacing: 0; text-transform: none; {col}">{text}</span>'

def detail_header(eye: str, title: str, sub: str, right: list[str], title_mono: bool = False) -> str:
    font = f"font-family: {MONO}; font-size: 16px;" if title_mono else "font-size: 17.6px;"
    return f'''
        <div style="display: flex; align-items: flex-start; gap: 10px;">
          <div style="display: flex; flex-direction: column; gap: 4px; min-width: 0; flex: 1;">
            {eyebrow(eye)}
            <h2 style="margin: 0; color: {C['ink_strong']}; {font} font-weight: 600; letter-spacing: -0.01em; line-height: 1.3;">{title}</h2>
            <span style="color: {C['ink_soft']}; font-size: 13px;">{sub}</span>
          </div>
          {" ".join(right)}
        </div>'''

def verdict_bar(tone: str, found: str, recorded: str, actions: list[str], stale: str = "", stale_tone: str = "warn") -> str:
    act = f'<div style="display: flex; align-items: center; gap: 8px; flex: 0 0 auto;">{" ".join(actions)}</div>' if actions else ""
    strip = ""
    if stale:
        line = C["warn_line"] if stale_tone == "warn" else C["danger_line"]
        fill = C["warn_soft"] if stale_tone == "warn" else C["danger_soft"]
        text = C["warn_ink"] if stale_tone == "warn" else C["danger_ink"]
        strip = (f'<div style="display: flex; align-items: center; gap: 8px; padding: 8px 16px; border-top: 1px solid {line}; '
                 f'background: {fill}; color: {text}; font-size: 12.8px; line-height: 1.4;">{ic("warning", 13)}<span style="flex: 1;">{stale}</span></div>')
    return f'''
        <div style="display: flex; flex-direction: column; border: 1px solid {C['border']}; border-radius: 12px; background: {C['raised']}; overflow: hidden;">
          <div style="display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 16px; align-items: center; padding: 12px 16px;">
            <div style="display: flex; flex-direction: column; gap: 4px; min-width: 0;">
              <span style="display: inline-flex; align-items: center; gap: 8px; color: {C['ink_strong']}; font-size: 14px; font-weight: 600; line-height: 1.35;">{dot(tone)}<span>{found}</span></span>
              <span style="color: {C['ink_soft']}; font-size: 12.8px; line-height: 1.45;">{recorded}</span>
            </div>
            {act}
          </div>
          {strip}
        </div>'''

def section(label: str, body: str, right: str = "", gap: int = 8) -> str:
    head = eyebrow(label)
    if right:
        head = f'<div style="display: flex; align-items: center;">{eyebrow(label)}<span style="flex: 1;"></span>{right}</div>'
    return f'<div style="display: flex; flex-direction: column; gap: {gap}px;">{head}{body}</div>'

def chevron_link(text: str, color: str | None = None) -> str:
    col = color or C["muted"]
    return f'<a href="#" style="display: inline-flex; align-items: center; gap: 6px; color: {col}; font-size: 12.8px; font-weight: 600;">{ic("chev_right", 13)}{text}</a>'

def link(text: str, color: str | None = None, icon: str | None = None) -> str:
    col = color or C["teal"]
    i = ic(icon, 13) if icon else ""
    return f'<a href="#" style="display: inline-flex; align-items: center; gap: 5px; color: {col}; font-size: 12.8px; font-weight: 600;">{i}{text}</a>'

def pill(text: str, tone: str = "neutral", size: float = 11.5) -> str:
    line, fill, col = TONES[tone]
    return (f'<span style="display: inline-flex; align-items: center; padding: 2px 9px; border: 1px solid {line}; border-radius: 999px; '
            f'background: {fill}; color: {col}; font-size: {size}px; font-weight: 600; white-space: nowrap;">{text}</span>')

def tag_chip(text: str, tone: str = "neutral", icon: str | None = None, mono_text: bool = True) -> str:
    """The 8px-radius reference chip (RCM row, finding, test)."""
    line, fill, col = TONES[tone]
    if tone == "neutral":
        line, fill, col = C["teal_line"], C["panel"], C["teal"]
    i = ic(icon, 13) if icon else ""
    font = f"font-family: {MONO}; font-size: 11.5px;" if mono_text else "font-size: 12.8px;"
    return (f'<a href="#" style="display: inline-flex; align-items: center; gap: 6px; padding: 4px 9px; border: 1px solid {line}; border-radius: 8px; '
            f'background: {fill}; color: {col}; {font} font-weight: 600; white-space: nowrap;">{i}{text}</a>')

def segmented(options: list[tuple[str, str, bool]]) -> str:
    """options: (label, colour, active)."""
    cells = []
    for i, (label, col, active) in enumerate(options):
        border = f"border-left: 1px solid {C['border_strong']};" if i else ""
        fill = f"background: {C['raised']};" if active else ""
        cells.append(f'<span style="display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px; {border}{fill} color: {col}; font-size: 12.8px; font-weight: 600;">{label}</span>')
    return (f'<span style="display: inline-flex; border: 1px solid {C["border_strong"]}; border-radius: 8px; overflow: hidden; background: {C["panel"]};">'
            f'{"".join(cells)}</span>')

def select_control(label: str, value: str) -> str:
    return (f'<span style="display: inline-flex; align-items: center; gap: 8px; padding: 5px 10px; border: 1px solid {C["border_strong"]}; border-radius: 8px; '
            f'font-size: 12.8px; white-space: nowrap;"><span style="color: {C["muted"]};">{label}</span><span style="color: {C["ink"]}; font-weight: 600;">{value}</span>'
            f'{ic("chev_down", 12, C["muted"], 2.5)}</span>')

def tabs(items: list[tuple[str, str, bool]], right: str = "") -> str:
    """items: (label, badge_html, active). One row under the verdict bar."""
    cells = []
    for label, badge, active in items:
        color = C["teal_strong"] if active else C["ink_soft"]
        under = C["teal"] if active else "transparent"
        cells.append(f'<span style="display: inline-flex; align-items: center; gap: 6px; padding: 8px 12px; border-bottom: 2px solid {under}; color: {color}; font-size: 13px; font-weight: 600;">{label}{badge}</span>')
    return (f'<div style="display: flex; align-items: center; gap: 2px; border-bottom: 1px solid {C["border"]};">{"".join(cells)}'
            f'<span style="flex: 1;"></span>{right}</div>')

def count_badge(text: str, tone: str = "neutral") -> str:
    fill, col = (C["raised"], C["muted"]) if tone == "neutral" else (C["danger_soft"], C["danger_ink"]) if tone == "bad" else (C["warn_soft"], C["warn_ink"])
    return f'<span class="num" style="padding: 0 6px; border-radius: 999px; background: {fill}; color: {col}; font-size: 11px;">{text}</span>'

def para(text: str, size: float = 14, color: str | None = None) -> str:
    return f'<p style="margin: 0; color: {color or C["ink"]}; font-size: {size}px; line-height: 1.55;">{text}</p>'

def card(inner: str, pad: str = "12px 14px", fill: str | None = None, border: str | None = None, gap: int = 8) -> str:
    return (f'<div style="display: flex; flex-direction: column; gap: {gap}px; padding: {pad}; border: 1px solid {border or C["border"]}; '
            f'border-radius: 8px; background: {fill or C["panel"]};">{inner}</div>')

def outline(label: str, entries: list[tuple[str, bool, str]]) -> str:
    """Sticky 'On this …' list. entries: (text, current, marker_html)."""
    rows = []
    for text, current, marker in entries:
        rule = C["teal"] if current else "transparent"
        col = C["teal_strong"] if current else C["ink_soft"]
        w = 600 if current else 500
        rows.append(f'<a href="#" style="display: flex; align-items: center; gap: 6px; padding: 6px 10px; border-left: 2px solid {rule}; color: {col}; font-size: 12.8px; font-weight: {w};"><span class="ell" style="flex: 1;">{text}</span>{marker}</a>')
    return (f'<span style="padding: 0 10px 8px; color: {C["muted"]}; font-size: 11px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;">{label}</span>'
            + "".join(rows))

def doc_card(inner: str, width: int = 760) -> str:
    return (f'<div style="max-width: {width}px; display: flex; flex-direction: column; gap: 18px; padding: 32px 40px; border: 1px solid {C["border"]}; '
            f'border-radius: 12px; background: {C["panel"]};">{inner}</div>')

def h3(text: str) -> str:
    return f'<h3 style="margin: 0; color: {C["ink_strong"]}; font-size: 15.2px; font-weight: 600;">{text}</h3>'

def doc_p(text: str) -> str:
    return f'<p style="margin: 0; color: {C["ink"]}; font-size: 14px; line-height: 1.6;">{text}</p>'

def doc_section(title: str, *parts: str) -> str:
    return f'<div style="display: flex; flex-direction: column; gap: 6px;">{h3(title)}{"".join(parts)}</div>'

def side_card(title: str, inner: str, count: str = "") -> str:
    head = f'<div style="display: flex; align-items: center; gap: 8px;"><span style="color: {C["muted"]}; font-size: 11px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase;">{title}</span><span style="flex: 1;"></span><span class="num" style="color: {C["muted"]}; font-size: 11.5px;">{count}</span></div>'
    return (f'<div style="display: flex; flex-direction: column; gap: 8px; padding: 12px 14px; border: 1px solid {C["border"]}; '
            f'border-radius: 8px; background: {C["panel"]};">{head}{inner}</div>')

def kv_row(label: str, value: str, value_color: str | None = None, mono_value: bool = False) -> str:
    font = f"font-family: {MONO}; font-size: 11.5px;" if mono_value else "font-size: 12.8px;"
    return (f'<div style="display: flex; align-items: baseline; justify-content: space-between; gap: 12px; padding: 4px 0; border-bottom: 1px solid {C["border"]};">'
            f'<span style="color: {C["muted"]}; font-size: 12px;">{label}</span>'
            f'<span class="num" style="color: {value_color or C["ink"]}; {font} font-weight: 600; text-align: right;">{value}</span></div>')

def warn_strip(text: str, action: str = "") -> str:
    return (f'<div style="display: flex; align-items: center; gap: 10px; padding: 8px 12px; border-left: 3px solid {C["warn"]}; border-radius: 0 8px 8px 0; '
            f'background: {C["warn_soft"]}; color: {C["warn_ink"]}; font-size: 13px; line-height: 1.45;"><span style="flex: 1;">{text}</span>{action}</div>')

def danger_strip(text: str, action: str = "") -> str:
    return (f'<div style="display: flex; align-items: center; gap: 10px; padding: 8px 12px; border-left: 3px solid {C["danger"]}; border-radius: 0 8px 8px 0; '
            f'background: {C["danger_soft"]}; color: {C["danger_ink"]}; font-size: 13px; line-height: 1.45;"><span style="flex: 1;">{text}</span>{action}</div>')

def footer_row(cols: list[str]) -> str:
    n = len(cols)
    return (f'<div style="display: grid; grid-template-columns: repeat({n}, minmax(0, 1fr)); gap: 14px; padding-top: 14px; border-top: 1px solid {C["border"]};">'
            + "".join(f'<div style="display: flex; align-items: center; gap: 10px;">{c}</div>' for c in cols) + '</div>')

def accent(text: str) -> str:
    return f'<span style="color: {C["accent"]};">{text}</span>'

def warn_text(text: str) -> str:
    return f'<span style="color: {C["warn_ink"]}; font-weight: 600;">{text}</span>'

def bad_text(text: str) -> str:
    return f'<span style="color: {C["danger"]}; font-weight: 600;">{text}</span>'

def sep() -> str:
    return f'<span style="color: {C["border_strong"]};">·</span>'


# =============================================================================
# Documents
# =============================================================================
DOC_ROWS = [
    ("policy", "2 documents · both analysed · analyses to review", [
        ("ok", "Procurement SOP Extracts.docx", f"1 page · analysed · {warn_text('to review')}", False),
        ("ok", "Financial Approval Matrix.docx", f"1 page · analysed · {warn_text('to review')}", False),
    ]),
    ("minutes", "1 document · analysed", [
        ("ok", "Minutes of Meeting - Procurement Planning.docx", f"1 page · analysed · {warn_text('to review')}", False),
    ]),
    ("evidence", "5 documents · 5 types · typed by the model", [
        ("ok", "GRN2024004_Signed_Receipt.pdf", f"1 page · goods receipt · {warn_text('to review')}", False),
        ("ok", "INV2024004_Signed_Payment_Voucher.pdf", f"1 page · payment voucher · {warn_text('to review')}", False),
        ("ok", "PO2024004_Purchase_Order.pdf", f"1 page · purchase order · {warn_text('to review')}", False),
        ("ok", "REQ2024009_Purchase_Requisition.pdf", f"1 page · purchase requisition · {warn_text('to review')}", False),
        ("ok", "VINV001-202404_Invoice.pdf", f"1 page · vendor invoice · {warn_text('to review')}", True),
    ]),
]

def documents_list() -> str:
    inner = list_header("Search documents", [f"Group{ic('chev_down', 11, width=2.5)}"])
    for name, sentence, rows in DOC_ROWS:
        inner += group_head(name, sentence)
        inner += "".join(list_row(t, title, meta, active) for t, title, meta, active in rows)
    return list_panel(inner)

def documents_header() -> str:
    return page_header("Documents", "8 documents · 5 evidence · all analysed · 8 analyses to review", [
        btn("Analyse", icon="sparkles", caret=True),
        btn("Add documents", "primary", icon="plus"),
        kebab(),
    ])

def documents_review_bar() -> str:
    return review_bar(
        [chip(8, "All documents", pressed=True),
         chip(8, "Analysis to review", "warn"),
         chip(5, "Thin vocabulary", "warn"),
         chip(8, "Typed by the model", "agent")],
        [meter("Read", "8/8", [("ok", 100)]),
         meter("Analysed", "8/8", [("ok", 100)]),
         meter("Reviewed", "0/8", [])],
    )

def documents_detail_head() -> str:
    """One 32px row: what the file is, its name, and the two acts on it.

    The category and type sit as a pill before the name and the page count
    after it; the imported path moves under Technical details. What the viewer
    below shows deserves the height this row used to take."""
    return f'''
        <div style="display: flex; align-items: center; gap: 10px; min-height: 32px;">
          {pill("Evidence · vendor invoice", "neutral", 11)}
          <h2 class="ell" style="margin: 0; color: {C['ink_strong']}; font-size: 15.2px; font-weight: 600; letter-spacing: -0.01em; min-width: 0;">VINV001-202404_Invoice.pdf</h2>
          <span style="flex: 1;"></span>
          {select_control("Held as", "Evidence")}{btn("Add to assistant", icon="paperclip")}{btn("Mark reviewed", "primary", icon="check")}{kebab()}
        </div>'''

def documents_verdict() -> str:
    """The verdict bar at one line: the run and the record, side by side.

    A document's two facts are short, so they share a line and the strip drops
    from 60px to 36px; the fieldwork bar's two-line form stays for the pages
    whose sentences need it."""
    return f'''
        <div style="display: flex; align-items: center; gap: 12px; padding: 7px 14px; border: 1px solid {C['border']}; border-radius: 10px; background: {C['raised']};">
          <span style="display: inline-flex; align-items: center; gap: 8px; color: {C['ink_strong']}; font-size: 13px; font-weight: 600; white-space: nowrap;">{dot("ok")}Analysed 1 Sep 17:19</span>
          <span class="num" style="color: {C['muted']}; font-size: 12px; white-space: nowrap;">complete · current</span>
          <span style="color: {C['border_strong']};">|</span>
          <span class="ell" style="color: {C['ink_soft']}; font-size: 12.5px; min-width: 0;">Read as <b style="font-weight: 600; color: {C['ink']};">vendor invoice</b> by the model · {accent("not reviewed by an auditor")}</span>
          <span style="flex: 1;"></span>
          {btn("Re-analyse", icon="refresh", caret=True)}{btn("Mark reviewed", "primary", icon="check")}
        </div>'''

def documents_tabs(active: str) -> str:
    right = ""
    if active == "Preview":
        right = (f'<div style="display: flex; align-items: center; gap: 12px; padding-bottom: 6px;">'
                 f'{segmented([("Original", C["teal_strong"], True), ("Extracted text", C["ink_soft"], False)])}'
                 f'{link("Find", C["ink_soft"], "search")}{link("Open original", C["ink_soft"], "external")}</div>')
    return tabs([("Preview", "", active == "Preview"),
                 ("Analysis", "", active == "Analysis"),
                 ("Activity", count_badge("3"), active == "Activity")], right)

def invoice_page() -> str:
    """A stand-in for the browser's PDF viewer: the invoice as it renders."""
    row = lambda k, v: (f'<div style="display: grid; grid-template-columns: 170px minmax(0, 1fr); gap: 12px; padding: 5px 0; font-size: 12.5px;">'
                        f'<span style="color: {C["ink_soft"]};">{k}</span><span style="color: {C["ink"]};">{v}</span></div>')
    band = lambda t: f'<div style="padding: 5px 10px; background: {C["raised"]}; color: {C["ink_strong"]}; font-size: 12.5px; font-weight: 700;">{t}</div>'
    page = f'''
      <div style="width: 640px; display: flex; flex-direction: column; background: #ffffff; box-shadow: 0 2px 6px rgb(13 35 64 / 7%), 0 10px 24px rgb(13 35 64 / 6%);">
        <div style="padding: 20px 26px; background: #0d2340; color: #ffffff;">
          <div style="font-size: 18px; font-weight: 700; letter-spacing: 0.02em;">TAX INVOICE</div>
          <div style="font-size: 11.5px; color: #c3d2e4; margin-top: 5px;">Vendor invoice number: VINV001-202404</div>
        </div>
        <div style="display: flex; flex-direction: column; gap: 10px; padding: 18px 26px;">
          {band("Supplier")}{row("Vendor", "OfficeSupply Co.")}{row("Vendor reference", "V1022")}{row("Address", "Plot 18, Block A, Gulshan-e-Iqbal, Karachi, Pakistan")}{row("Bank", "United Bank / 9736250611")}
          {band("Invoice details")}{row("Internal invoice ID", "INV2024004")}{row("Invoice date", "09 Apr 2024")}{row("Date received", "10 Apr 2024")}{row("Purchase reference", "PO2024004")}{row("GRN reference", "GRN2024004")}
          {band("Charges")}{row("Description", "New Hire Onboarding Kits")}{row("Invoice amount (PKR)", "2,000,000.00")}{row("Due date", "10 May 2024")}
        </div>
      </div>'''
    toolbar = (f'<div style="display: flex; align-items: center; gap: 14px; padding: 5px 12px; background: {C["ink_strong"]}; color: #e6edf6; font-size: 11px; border-radius: 8px 8px 0 0;">'
               f'<span>{ic("list", 14)}</span><span style="flex: 1;">file</span><span class="num">1 / 1</span><span>{ic("search", 14)}</span><span>{ic("download", 14)}</span></div>')
    return (f'<div style="display: flex; flex-direction: column; border: 1px solid {C["border"]}; border-radius: 8px; overflow: hidden;">{toolbar}'
            f'<div style="display: flex; justify-content: center; padding: 16px; background: #525659;">{page}</div></div>')

def documents_preview_detail() -> str:
    inner = documents_detail_head() + documents_tabs("Preview") + invoice_page()
    inner += f'<div style="display: flex; gap: 18px;">{chevron_link("Technical details")}{chevron_link("Where this came from")}</div>'
    return detail_panel(inner, gap=10, pad="12px 16px")

def artboard_documents() -> str:
    content = documents_header() + documents_review_bar() + master_detail(documents_list(), documents_preview_detail())
    return shell("Documents", content, 1000)

# --- Analysis tab --------------------------------------------------------------
RECORD = [
    ("supplier", [
        ("name", "OfficeSupply Co."), ("reference", "V1022"),
        ("address", "Plot 18, Block A, Gulshan-e-Iqbal, Karachi, Pakistan"),
        ("bank_name", "United Bank"), ("bank_account_number", "9736250611"),
    ]),
    ("invoice", [
        ("internal_invoice_id", "INV2024004"), ("invoice_number", "VINV001-202404"),
        ("invoice_date", "2024-04-09"), ("date_received", "2024-04-10"),
        ("purchase_reference", "PO2024004"), ("grn_reference", "GRN2024004"), ("due_date", "2024-05-10"),
    ]),
    ("charges", [
        ("item_description", "New Hire Onboarding Kits"), ("invoice_amount", "2,000,000.00"), ("currency", "PKR"),
    ]),
]

def evidence_sheet() -> str:
    """The record as a page, with the JSON drawn faintly around the words.

    It sits in the same viewer frame as the file it was read from, at the same
    width, so the two read as the document and the machine's reading of it.
    Keys are mono and muted, values are the page's own type; the braces,
    quotes and commas are in the border colour, present enough to say this is
    a record and faint enough not to be read."""
    punct = lambda t: f'<span style="color: {C["border_strong"]};">{t}</span>'
    key = lambda k: f'<span style="font-family: {MONO}; font-size: 12px; color: {C["muted"]};">{punct("&quot;")}{k}{punct("&quot;")}</span>'
    lines = [f'<div style="font-family: {MONO}; font-size: 13px; color: {C["border_strong"]};">{{</div>']
    for gi, (group, fields) in enumerate(RECORD):
        lines.append(f'<div style="display: flex; align-items: baseline; gap: 6px; padding: 10px 0 4px 20px;">'
                     f'<span style="font-family: {MONO}; font-size: 12.5px; font-weight: 600; color: {C["ink_strong"]};">{punct("&quot;")}{group}{punct("&quot;")}</span>{punct(": {")}</div>')
        for fi, (k, v) in enumerate(fields):
            comma = "," if fi < len(fields) - 1 else ""
            lines.append(f'<div style="display: grid; grid-template-columns: 200px minmax(0, 1fr) auto; gap: 12px; align-items: baseline; padding: 4px 0 4px 40px; border-top: 1px solid {C["canvas"]};">'
                         f'<span>{key(k)}{punct(":")}</span>'
                         f'<span style="color: {C["ink"]}; font-size: 13.5px; line-height: 1.45;">{punct("&quot;")}{v}{punct("&quot;" + comma)}</span>'
                         f'{tag_chip("p.1", "neutral")}</div>')
        close = "}," if gi < len(RECORD) - 1 else "}"
        lines.append(f'<div style="font-family: {MONO}; font-size: 13px; color: {C["border_strong"]}; padding: 4px 0 0 20px;">{close}</div>')
    lines.append(f'<div style="font-family: {MONO}; font-size: 13px; color: {C["border_strong"]}; padding-top: 6px;">}}</div>')
    sheet = (f'<div style="width: 640px; display: flex; flex-direction: column; background: #ffffff; box-shadow: 0 2px 6px rgb(13 35 64 / 7%), 0 10px 24px rgb(13 35 64 / 6%);">'
             f'<div style="display: flex; align-items: center; gap: 10px; padding: 14px 26px; border-bottom: 1px solid {C["border"]};">'
             f'<span style="font-family: {MONO}; font-size: 10.5px; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; color: {C["muted"]};">Record 1 of 1 · vendor_invoice</span>'
             f'<span style="flex: 1;"></span>{pill("validated", "ok", 11)}{accent("<span style=\"font-size: 11.5px; font-weight: 600;\">read by the model</span>")}</div>'
             f'<div style="display: flex; flex-direction: column; padding: 14px 26px 20px;">{"".join(lines)}</div></div>')
    return (f'<div style="display: flex; justify-content: center; padding: 16px; border: 1px solid {C["border"]}; border-radius: 8px; background: {C["raised"]};">{sheet}</div>')

def documents_analysis_detail() -> str:
    vocab = card(
        f'<div style="display: flex; align-items: center; gap: 10px;"><span style="color: {C["ink_strong"]}; font-size: 13px; font-weight: 600;">Read as vendor invoice</span>'
        f'<span class="num" style="color: {C["muted"]}; font-size: 12px;">14 fields from 1 document · none stated by two</span><span style="flex: 1;"></span>{chevron_link("14 fields")}</div>'
        + warn_strip("Read from one document, so nothing corroborates its field names. Re-reading every vendor invoice would rebuild the vocabulary.",
                     link("Revise vocabulary", C["warn_ink"])),
        fill=C["panel"],
    )
    summary = card(
        para("Tax invoice VINV001-202404 from OfficeSupply Co. (V1022), dated 9 April 2024 and received 10 April 2024, for PKR 2,000,000.00 of new hire onboarding kits against purchase order PO2024004 and goods receipt GRN2024004. Payment is due 10 May 2024.", 13.5)
        + f'<span style="color: {C["muted"]}; font-size: 12px;">Derived from the structured evidence above; edit the fields, not the sentence.</span>',
        fill=C["raised"], border=C["border"],
    )
    notes = (f'<div style="min-height: 84px; padding: 10px 12px; border: 1px solid {C["border_strong"]}; border-radius: 8px; color: {C["muted"]}; font-size: 13px;">'
             f'Observations about this document. Notes are not evidence that a control operated.</div>')
    cite = lambda ref, text: (f'<a href="#" style="display: flex; gap: 10px; padding: 8px 10px; border: 1px solid {C["border"]}; border-radius: 8px; color: {C["ink"]}; font-size: 12.8px; line-height: 1.45;">'
                              f'{mono(ref, 11.5, C["teal"])}<span style="flex: 1;">{text}</span></a>')
    sources = (f'<div style="display: flex; flex-direction: column; gap: 6px;">'
               f'{cite("[C1] p.1", "TAX INVOICE — Vendor invoice number: VINV001-202404 · Vendor: OfficeSupply Co. · Vendor reference: V1022")}'
               f'{cite("[C2] p.1", "Purchase reference PO2024004 · GRN reference GRN2024004 · Invoice amount (PKR) 2,000,000.00 · Due date 10 May 2024")}</div>')
    body = (documents_detail_head() + documents_tabs("Analysis")
            + section("Vocabulary", vocab)
            + section("Structured evidence", evidence_sheet(), f'<span class="num" style="color: {C["muted"]}; font-size: 12px;">What the model read from the page, checked against the vendor invoice schema</span>')
            + section("Summary", summary)
            + section("Audit notes", notes)
            + section("Sources", sources)
            + f'<div style="display: flex; align-items: center; gap: 18px; padding-top: 14px; border-top: 1px solid {C["border"]};">'
              f'{chevron_link("Technical provenance")}{chevron_link("Where this came from")}<span style="flex: 1;"></span>'
              f'{btn("Save notes")}{btn("Save and mark reviewed", "primary", icon="check")}</div>')
    return detail_panel(body)

def artboard_document_analysis() -> str:
    content = documents_header() + documents_review_bar() + master_detail(documents_list(), documents_analysis_detail())
    return shell("Documents", content, 1240)


# =============================================================================
# Source tables
# =============================================================================
FILE_TABLES = [
    ("ok", "financial_approval_matrix", "4 rows · 2 columns · all tested", False),
    ("warn", "requisitions", f"55 rows · 14 columns · {warn_text('1 column untested')}", False),
    ("warn", "invoice_data", f"52 rows · 15 columns · {warn_text('1 column untested')}", True),
    ("warn", "po_data", f"52 rows · 11 columns · {warn_text('5 columns untested')}", False),
    ("warn", "staff_details", f"20 rows · 4 columns · {warn_text('3 columns untested')}", False),
    ("ok", "vendor_master_file", "12 rows · 3 columns · all tested", False),
]
JOIN_TABLES = [
    ("invoice_data_po_data_joined", "invoice_data ⋈ po_data on PO_NUMBER_LINK = PO_NUMBER"),
    ("invoice_data_requisitions_joined", "invoice_data ⋈ requisitions on PO_NUMBER_LINK = PO_NUMBER"),
    ("invoice_data_staff_details_joined", "invoice_data ⋈ staff_details on SUPERVISOR_APPROVAL_ID = STAFF_ID"),
    ("invoice_data_vendor_master_file_joined", "invoice_data ⋈ vendor_master_file on VENDOR_ID"),
    ("po_data_requisitions_joined", "po_data ⋈ requisitions on REQUISITION_ID"),
    ("po_data_vendor_master_file_joined", "po_data ⋈ vendor_master_file on VENDOR_ID"),
    ("requisitions_staff_details_joined", "requisitions ⋈ staff_details on FIN_APPROVED_BY_ID = STAFF_ID"),
]

def tables_list() -> str:
    inner = list_header("Filter tables", [])
    inner += group_head("Files", "6 tables · 195 rows · 4 with untested columns")
    inner += "".join(list_row(t, name, meta, active, title_mono=True) for t, name, meta, active in FILE_TABLES)
    inner += group_head("Joins", "12 joins · all built by the assistant")
    inner += "".join(list_row("ok", name, f"{meta} · {accent('assistant')}", False, title_mono=True) for name, meta in JOIN_TABLES)
    inner += f'<div style="padding: 8px 12px; border-top: 1px solid {C["border"]};">{chevron_link("5 more joins")}</div>'
    return list_panel(inner)

def tables_header() -> str:
    return page_header("Source tables", "6 files · 12 joins · 10 columns no test evaluates", [
        btn("Add join", icon="link"),
        btn("Add files", "primary", icon="upload"),
        kebab(),
    ])

def tables_review_bar() -> str:
    return review_bar(
        [chip(18, "All tables", pressed=True),
         chip(4, "Columns untested", "warn"),
         chip(6, "No validation rules", "neutral"),
         chip(12, "Built by the assistant", "agent")],
        [meter("Profiled", "18/18", [("ok", 100)]),
         meter("Tested", "2/6", [("ok", 33), ("warn", 67)]),
         meter("Validated", "0/6", [])],
    )

COLUMNS = [
    ("INVOICE_ID", "String", "id", 0.0, "52 (100%)", "INV2024004 – INV2024154", "3 tests"),
    ("VENDOR_INVOICE_NUMBER", "String", "id", 0.0, "52 (100%)", "VINV001-202404 – VINV154-2024", "1 test"),
    ("INVOICE_DATE", "Date", "date", 0.0, "48 (92%)", "2024-01-30 – 2025-01-21", "4 tests"),
    ("DATE_RECEIVED", "Date", "date", 0.0, "44 (85%)", "2024-01-27 – 2025-01-25", "2 tests"),
    ("VENDOR_ID", "String", "categorical", 0.0, "12 (23%)", "V1001 – V1034", "5 tests"),
    ("PO_NUMBER_LINK", "String", "id", 0.0, "52 (100%)", "PO2024004 – PO2024154", "6 tests"),
    ("GRN_ID_LINK", "String", "id", 5.8, "49 (100%)", "GRN2024004 – GRN2024154", "3 tests"),
    ("INVOICE_AMOUNT", "Int64", "numeric", 0.0, "48 (92%)", "36,000 – 85,000,000 (mean 11,971,842)", "4 tests"),
    ("DUE_DATE", "Date", "date", 0.0, "48 (92%)", "2024-02-29 – 2025-02-20", None),
    ("VERIFIED_BY_ID", "Int64", "numeric", 0.0, "4 (8%)", "1010 – 1013 (mean 1,011)", "2 tests"),
    ("VERIFICATION_DATE", "Date", "date", 0.0, "45 (87%)", "2024-02-04 – 2025-01-28", "2 tests"),
    ("SUPERVISOR_APPROVAL_ID", "Int64", "numeric", 5.8, "4 (8%)", "1001 – 1004 (mean 1,003)", "2 tests"),
    ("SUPERVISOR_APPROVAL_DATE", "Date", "date", 5.8, "46 (94%)", "2024-02-07 – 2025-01-29", "1 test"),
    ("PAYMENT_STATUS", "String", "categorical", 0.0, "1 (2%)", "Paid", "1 test"),
    ("PAYMENT_DATE", "Date", "date", 0.0, "49 (94%)", "2024-02-16 – 2025-02-10", "5 tests"),
]
TYPE_TONE = {"id": "neutral", "numeric": "ok", "date": "info", "categorical": "warn"}

def column_table() -> str:
    grid = "minmax(0, 1.4fr) 100px 120px 90px minmax(0, 1.4fr) 90px"
    head = (f'<div style="display: grid; grid-template-columns: {grid}; gap: 0 12px; padding: 7px 12px; background: {C["raised"]}; '
            f'color: {C["muted"]}; font-family: {MONO}; font-size: 10.5px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase;">'
            f'<span>Column</span><span>Type</span><span>Blank</span><span>Distinct</span><span>Range / mean</span><span>Tested</span></div>')
    rows = []
    for name, dtype, kind, blank, distinct, rng, tested in COLUMNS:
        bar = (f'<span style="display: flex; align-items: center; gap: 6px;"><span style="display: block; width: 64px; height: 6px; border-radius: 3px; '
               f'background: linear-gradient(90deg, {C["teal"]} 0 {blank * 4:.0f}%, {C["border"]} {blank * 4:.0f}% 100%);"></span>'
               f'<span class="num" style="color: {C["muted"]}; font-size: 11.5px;">{blank:g}%</span></span>')
        if tested:
            tcell = f'<span class="num" style="display: inline-flex; align-items: center; gap: 4px; color: {C["ok"]}; font-size: 12px; font-weight: 600;">{ic("check", 12, width=2.5)}{tested}</span>'
            name_html = mono(name, 12, C["ink_strong"])
        else:
            tcell = f'<span style="color: {C["warn_ink"]}; font-size: 12px; font-weight: 600;">None</span>'
            name_html = mono(name, 12, C["warn_ink"])
        rows.append(f'<div style="display: grid; grid-template-columns: {grid}; gap: 0 12px; align-items: center; padding: 8px 12px; border-top: 1px solid {C["border"]}; font-size: 12.5px; color: {C["ink"]};">'
                    f'<span style="display: flex; align-items: center; gap: 8px;">{ic("chev_right", 12, C["border_strong"], 2.5)}{name_html}<span style="color: {C["muted"]}; font-size: 11px;">{dtype}</span></span>'
                    f'<span>{pill(kind, TYPE_TONE[kind], 11)}</span>{bar}<span class="num">{distinct}</span>'
                    f'<span class="num ell" style="font-family: {MONO}; font-size: 11.5px; color: {C["ink_soft"]};">{rng}</span>{tcell}</div>')
    return f'<div style="border: 1px solid {C["border"]}; border-radius: 8px; overflow: hidden;">{head}{"".join(rows)}</div>'

def tables_detail() -> str:
    head = detail_header(
        f"File · Invoice data.xlsx · imported 1 Sep 17:06",
        "invoice_data", "Joined into 6 tables · profiled 5 Sep",
        [btn("Replace data", icon="upload"), kebab()], title_mono=True,
    )
    verdict = verdict_bar(
        "ok",
        f'52 rows · 15 columns <span class="num" style="color: {C["muted"]}; font-size: 12.8px; font-weight: 500;">· no duplicate rows · 5.1 KB in memory · profiled 5 Sep</span>',
        f'14 of 15 columns are evaluated by a data test. {warn_text("DUE_DATE is evaluated by none")} — no test asks whether anything was paid late.',
        [f'<span style="color: {C["muted"]}; font-size: 12.8px;">No validation rules</span>', btn("New rule set", icon="plus")],
    )
    t = tabs([("Profile", "", True), ("Preview", count_badge("100 rows"), False), ("Validation", count_badge("0"), False), ("Relationships", count_badge("6"), False)])
    note = f'<span class="num" style="color: {C["muted"]}; font-size: 12px;">Statistics are computed on all 52 rows. Expand a column for its most common values.</span>'
    return detail_panel(head + verdict + t + note + column_table()
                        + f'<div style="display: flex; gap: 18px;">{chevron_link("Where this came from")}{chevron_link("Technical details")}</div>')

def artboard_tables() -> str:
    content = tables_header() + tables_review_bar() + master_detail(tables_list(), tables_detail())
    return shell("Source tables", content, 1140)


# =============================================================================
# Findings
# =============================================================================
FINDING_ROWS = [
    ("Critical", "1 finding", [
        ("critical", "Requisitions approved above the authority's approval limit", "F-0571DE", True),
    ]),
    ("High", "8 findings", [
        ("bad", "Invoices exceeding purchase order totals", "F-43AEAB", False),
        ("bad", "Payment released to a vendor with Inactive master status", "F-59EBEB", False),
        ("bad", "Payment released before goods receipt recorded", "F-6C94CF", False),
        ("bad", "Payments released to vendors not recorded as active", "F-9EFDB0", False),
        ("bad", "Payments released before the corresponding goods receipt was recorded", "F-AC137E", False),
        ("bad", "Invoices paid in excess of the linked purchase order total", "F-CF1784", False),
        ("bad", "Invoices paid without recorded approval or in excess of the approval limit", "F-DE068C", False),
        ("bad", "Requisition linked to a vendor recorded as Inactive in the vendor master", "F-F0C84A", False),
    ]),
]

def findings_list() -> str:
    inner = list_header("Search findings", [f"Severity{ic('chev_down', 11, width=2.5)}"])
    for name, sentence, rows in FINDING_ROWS:
        inner += group_head(name, sentence)
        for tone, title, fid, active in rows:
            meta = f"{mono(fid, 11)} · {bad_text('no risk')} · {warn_text('cause pending')}"
            inner += list_row(tone, title, meta, active)
    inner += group_head("Medium", "9 findings", open_=False)
    return list_panel(inner)

def findings_header() -> str:
    return page_header("Findings", "18 findings · 1 critical · 8 high · 9 medium · none in the report", [
        btn("Draft from the RCM", icon="sparkles"),
        btn("Add finding", "primary", icon="plus"),
        kebab(),
    ])

def findings_review_bar(wrap: bool = False) -> str:
    return review_bar(wrap=wrap, chips=[chip(18, "All findings", pressed=True),
         chip(18, "Not linked to a risk", "bad"),
         chip(18, "Evidence moved", "bad"),
         chip(17, "Root cause pending", "warn"),
         chip(18, "No management response", "warn"),
         chip(18, "Drafted by the assistant", "agent")],
        meters=[meter("Confirmed", "18/18", [("ok", 100)]),
         meter("Supported", "0/18", []),
         meter("Settled", "0/18", [])],
    )

def narrative_table() -> str:
    grid = "minmax(0, 1fr) 140px 220px"
    head = (f'<div style="display: grid; grid-template-columns: {grid}; gap: 0 12px; padding: 6px 12px; background: {C["raised"]}; '
            f'color: {C["muted"]}; font-size: 11px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase;">'
            f'<span>Authority</span><span>Approval limit</span><span>Highest approved requisition</span></div>')
    row = lambda a, b, c: (f'<div style="display: grid; grid-template-columns: {grid}; gap: 0 12px; padding: 7px 12px; border-top: 1px solid {C["border"]}; font-size: 13px; color: {C["ink"]};">'
                           f'<span>{a}</span><span class="num">{b}</span><span class="num">{c}</span></div>')
    return (f'<div style="border: 1px solid {C["border"]}; border-radius: 8px; overflow: hidden; margin: 4px 0;">{head}'
            f'{row("Head of Treasury", "1,000,000", "4,200,000")}{row("CFO", "10,000,000", "64,000,000")}</div>')

def findings_detail(narrow: bool = False) -> str:
    head = detail_header(
        f"{mono('F-0571DE')} · {accent('drafted by the assistant')} · 1 Sep 19:02",
        "Requisitions approved above the authority's approval limit",
        "Drafted from the exceptions of one data test. The narrative is copied into the report unchanged.",
        [pill("Critical", "bad", 12.8) if False else
         f'<span style="display: inline-flex; align-items: center; gap: 6px; padding: 5px 10px; border: 1px solid {C["danger_line"]}; border-radius: 8px; background: {C["danger_soft"]}; color: {C["danger_ink"]}; font-size: 12.8px; font-weight: 600;">Critical{ic("chev_down", 12, width=2.5)}</span>',
         btn("Save", "primary"), kebab()],
    )
    verdict = verdict_bar(
        "ok",
        f'Confirmed for reporting <span class="num" style="color: {C["muted"]}; font-size: 12.8px; font-weight: 500;">· by you, 3 Sep 11:54</span>',
        f'Left out of the report until it is supported: {bad_text("not linked to a risk")} · {bad_text("evidence changed since drafting")} · {warn_text("root cause pending")} · {warn_text("no management response")}',
        [link("Withdraw confirmation", C["ink_soft"]), btn("Link to a risk", "primary", icon="map", caret=True)],
        stale=f'The test result this finding cites ({mono("DAT-7A08FCD758", 11.5)}, current run) has changed since the narrative was drafted. Re-read the condition against the run, then re-affirm the evidence.'
              + f'&nbsp;&nbsp;{link("Re-affirm", C["warn_ink"])}',
    )
    narrative = (
        doc_section("Condition",
                    doc_p("Two financial approval matrix entries carried approval limits below the maximum value of the requisitions approved by those authorities. The exceptions were:"),
                    narrative_table(),
                    doc_p("The Head of Treasury approved requisitions up to 4,200,000 against a limit of 1,000,000, and the CFO approved requisitions up to 64,000,000 against a limit of 10,000,000."))
        + doc_section("Criteria", doc_p("The Delegation of Authority requires a commitment to be approved by an authority whose financial approval limit is equal to or above the committed value; approval by an authority below the committed value breaches the delegation."))
        + doc_section("Root cause", warn_strip("Pending auditor follow-up. The report will carry this section empty until a cause is recorded.", link("Record the cause", C["warn_ink"])))
        + doc_section("Risk", doc_p("Approvals exceeding the limits delegated to the approving officer expose the entity to commitments made outside the authority granted by the financial approval matrix."))
        + doc_section("Recommendation", doc_p("Correct the financial approval matrix so that each authority's approval limit is set at or above the value of the requisitions that authority is permitted to approve, and review the requisitions already approved above the recorded limits to confirm they were approved under a validly delegated authority."))
    )
    left = (section("Narrative", f'<div style="display: flex; flex-direction: column; gap: 16px;">{narrative}</div>', link("Edit", C["ink_soft"], "pencil"))
            + section("Management response", card(f'<span style="color: {C["muted"]}; font-size: 13px;">None received.</span>', fill=C["raised"]) , link("Record as received", C["teal"], "plus")))
    risk_card = (f'<div style="display: flex; flex-direction: column; gap: 6px; padding: 12px 14px; border: 1px dashed {C["danger_line"]}; border-radius: 8px; background: {C["danger_soft"]};">'
                 f'<span style="color: {C["danger_ink"]}; font-size: 13px; font-weight: 600;">Not linked to a risk</span>'
                 f'<span style="color: {C["danger_ink"]}; font-size: 12.5px; line-height: 1.45;">The report cannot place this finding in a process until it names the row it answers.</span>'
                 f'{link("Choose a row", C["danger_ink"], "map")}</div>')
    test_card = card(f'<div style="display: flex; align-items: center; gap: 8px;">{tag_chip("DAT-7A08FCD758", "neutral", "chart")}{pill("2 exceptions", "bad", 11)}</div>'
                     f'<span style="color: {C["ink"]}; font-size: 12.8px; line-height: 1.45;">Financial Approval Matrix limits are sufficient for all requisitions approved under them</span>'
                     f'<span class="num" style="color: {C["muted"]}; font-size: 11.5px;">2 exceptions on the current run · 55 requisitions</span>')
    ev_card = card(f'<div style="display: flex; align-items: center; gap: 8px;">{mono("EV-FD1C88A213", 11.5, C["ink_strong"])}{pill("changed", "warn", 11)}</div>'
                   f'<span style="color: {C["ink_soft"]}; font-size: 12.5px;">Data test result · {mono("DAT-7A08FCD758", 11)} · current run</span>'
                   f'<span class="num" style="color: {C["muted"]}; font-size: 11.5px;">Drafted against b20d295d · the run has moved since</span>')
    right = (f'<div style="display: flex; flex-direction: column; gap: 14px;">'
             + section("Risk", risk_card)
             + section("Tests", test_card, link("Add", C["teal"], "plus"))
             + section("Evidence", ev_card, link("Add", C["teal"], "plus"))
             + f'<div style="display: flex; flex-direction: column; gap: 6px;">{chevron_link("Where this came from")}{chevron_link("Run 20260901-190012-ac9972")}</div></div>')
    columns = "minmax(0, 1fr)" if narrow else "minmax(0, 1fr) 320px"
    body = f'<div style="display: grid; grid-template-columns: {columns}; gap: 22px; align-items: start;"><div style="display: flex; flex-direction: column; gap: 18px;">{left}</div>{right}</div>'
    return detail_panel(head + verdict + body)

def artboard_findings() -> str:
    content = findings_header() + findings_review_bar() + master_detail(findings_list(), findings_detail())
    return shell("Findings register", content, 1200)


# =============================================================================
# Audit planning memorandum
# =============================================================================
APM_SECTIONS = ["Engagement", "Introduction and background", "Process flow and understanding", "Prior audit findings",
                "Data analytics performed", "Fraud risk and management override", "Key risks and planned response",
                "Planning assumptions and matters reported"]

def source_row(badge: str, name: str, meta: str, open_link: bool = True) -> str:
    b = f'<span style="padding: 1px 5px; border: 1px solid {C["border_strong"]}; border-radius: 4px; font-family: {MONO}; font-size: 9.5px; font-weight: 600; color: {C["muted"]};">{badge}</span>'
    o = link("Open", C["teal"]) if open_link else ""
    return (f'<div style="display: flex; align-items: flex-start; gap: 8px; padding: 6px 0; border-top: 1px solid {C["border"]};">{b}'
            f'<div style="display: flex; flex-direction: column; gap: 2px; min-width: 0; flex: 1;"><span class="ell" style="color: {C["ink"]}; font-size: 12.5px; font-weight: 500;">{name}</span>'
            f'<span class="num" style="color: {C["muted"]}; font-size: 11px;">{meta}</span></div>{o}</div>')

def provenance_rail() -> str:
    sources = (f'<span style="color: {C["muted"]}; font-size: 10.5px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;">Documents · 3</span>'
               + source_row("DOC", "Minutes of Meeting - Procurement Planning.docx", "3,456 chars · ~864 tokens · summary")
               + source_row("DOC", "Procurement SOP Extracts.docx", "6,960 chars · ~1,740 tokens · summary")
               + source_row("DOC", "Financial Approval Matrix.docx", "3,027 chars · ~757 tokens · summary")
               + f'<div style="display: flex; flex-direction: column; gap: 4px; padding-top: 6px;">{chevron_link("7 tables")}{chevron_link("4 other sources")}</div>'
               + f'<span class="num" style="color: {C["muted"]}; font-size: 11.5px; padding-top: 4px; border-top: 1px solid {C["border"]};">Total supplied · 36,400 chars · ~9,608 tokens</span>')
    withheld = (f'<div style="display: flex; flex-direction: column; gap: 4px;">'
                f'<span style="display: flex; justify-content: space-between; font-size: 12.5px; color: {C["ink"]};"><span>5 documents</span><span style="color: {C["muted"]};">outside this step\'s scope</span></span>'
                f'<span style="display: flex; justify-content: space-between; font-size: 12.5px; color: {C["ink"]};"><span>1 other source</span><span style="color: {C["muted"]};">not available</span></span></div>')
    generation = (kv_row("Step", "Audit planning memorandum") + kv_row("Model", "deepseek-v4-flash-0731", mono_value=True)
                  + kv_row("Calls", "1 · 15,529 prompt tokens") + kv_row("Took", "1m 21s")
                  + kv_row("Committed", "revision 116", mono_value=True))
    feeds = (f'<div style="display: flex; flex-direction: column; gap: 6px;">'
             f'<a href="#" style="display: flex; align-items: center; gap: 8px; padding: 8px 10px; border: 1px solid {C["ok_line"]}; border-radius: 8px; background: {C["ok_soft"]};">'
             f'{ic("check", 13, C["ok"], 2.5)}<span style="display: flex; flex-direction: column; gap: 1px; flex: 1;"><span style="color: {C["ink_strong"]}; font-size: 12.8px; font-weight: 600;">Cycle · Procure-to-pay</span>'
             f'<span style="color: {C["ok"]}; font-size: 11.5px;">4 steps · derived 5 Sep from this version</span></span>{ic("chev_right", 13, C["ok"])}</a>'
             f'<a href="#" style="display: flex; align-items: center; gap: 8px; padding: 8px 10px; border: 1px solid {C["border"]}; border-radius: 8px; background: {C["panel"]};">'
             f'{ic("map", 13, C["teal"])}<span style="display: flex; flex-direction: column; gap: 1px; flex: 1;"><span style="color: {C["ink_strong"]}; font-size: 12.8px; font-weight: 600;">Risk and control matrix</span>'
             f'<span class="num" style="color: {C["muted"]}; font-size: 11.5px;">32 risks · 5 processes</span></span>{ic("chev_right", 13, C["muted"])}</a></div>')
    return (f'<div style="display: flex; flex-direction: column; gap: 12px;">'
            + side_card("Where this came from", sources, "28 sources")
            + side_card("Not supplied", withheld, "6")
            + side_card("Generation", generation, "1 Sep 17:22")
            + side_card("What this feeds", feeds)
            + "</div>")

def apm_document() -> str:
    bullets = "".join(f'<li style="margin: 0 0 4px;">{t}</li>' for t in [
        "<b style=\"font-weight: 600;\">Entity:</b> Global Bank",
        "<b style=\"font-weight: 600;\">Period:</b> January 2024 to January 2025",
        "<b style=\"font-weight: 600;\">Objective &amp; scope:</b> The objective of this audit is to review and assess the entity's performance against the established controls and procedures. The engagement is a risk-based review of the procure-to-pay cycle covering requisition and approval, purchase orders, goods receipt, and invoice processing and payment. Transaction records and supporting documentation are to be provided by Procurement.",
    ])
    steps = (f'<ol style="margin: 4px 0 0; padding-left: 22px; color: {C["ink"]}; font-size: 14px; line-height: 1.6;">'
             f'<li style="margin-bottom: 6px;"><b style="font-weight: 600;">Requisition initiation and approval</b> — The requisitioning department raises a Purchase Requisition in the ERP system stating goods or services, quantity, required delivery period, business justification, estimated cost and proposed vendor. The Procurement Team reviews for completeness and records verification. A procurement approver (other than the requester and the verifier) authorises the requirement. The designated Financial Authority approves the committed value against the Financial Approval Matrix.</li>'
             f'<li style="margin-bottom: 6px;"><b style="font-weight: 600;">Purchase Order</b> — A Purchase Order is raised only where an approved requisition exists and preserves the vendor, description, quantity and value approved.</li>'
             f'<li style="color: {C["muted"]};">Goods receipt · Invoice processing and payment …</li></ol>')
    inner = (f'<div style="display: flex; flex-direction: column; gap: 4px;">{eyebrow("Audit planning memorandum · Procurement")}'
             f'<h2 style="margin: 0; color: {C["ink_strong"]}; font-size: 21.6px; font-weight: 700; letter-spacing: -0.01em; line-height: 1.3;">Audit Planning Memorandum</h2></div>'
             + doc_section("Engagement", f'<ul style="margin: 0; padding-left: 20px; color: {C["ink"]}; font-size: 14px; line-height: 1.6;">{bullets}</ul>')
             + doc_section("Introduction and background",
                           doc_p("Global Bank's Board has adopted a growth strategy of expansion into new regions and diversification into digital banking. This has driven a marked increase in operational scale — new branch openings, technology infrastructure upgrades and recruitment — and procurement volume and complexity have risen accordingly across all departments. Procurement operations run through the ERP system, with approval limits applied from the Financial Approval Matrix."),
                           doc_p("Minutes of the procurement planning meeting (22 July 2025) record that approvals and supporting records have at times been completed after the event rather than before, and that the department has not carried out a systematic review of exceptions. The minutes also record the agreed action that Internal Audit will perform a risk-based review of the procure-to-pay cycle and that Procurement will provide transaction records and a sample of supporting documentation."),
                           doc_p("The governing documents referenced for the review are the Procurement SOP and the Financial Approval Matrix. Neither document's governance metadata is complete in the supplied extracts, so the authority of both documents for the period under review cannot be confirmed from the material supplied."))
             + doc_section("Process flow and understanding", doc_p("The Procurement SOP extract describes the following process sequence:"), steps))
    return doc_card(inner)

def artboard_apm() -> str:
    header = page_header("Audit planning memorandum", "8 sections · 2,900 words · drafted by the assistant 1 Sep · edited by an auditor", [
        btn("Edit", icon="pencil"),
        btn("Export", icon="download", caret=True),
        btn("Regenerate", "primary", icon="sparkles"),
        kebab(),
    ])
    verdict = verdict_bar(
        "ok",
        f'Drafted by the assistant 1 Sep 17:22 <span class="num" style="color: {C["muted"]}; font-size: 12.8px; font-weight: 500;">· from 3 documents and 7 tables · 1m 21s</span>',
        f'Edited by an auditor 1 Sep 17:25. The cycle and the 32 risks in the matrix were derived from this version, so a change here puts them out of date.',
        [],
    )
    left = (f'<div style="display: flex; flex-direction: column; gap: 2px; align-self: start; position: sticky; top: 20px;">'
            + outline("On this memorandum", [(s, i == 0, "") for i, s in enumerate(APM_SECTIONS)])
            + "</div>")
    body = (f'<div style="display: grid; grid-template-columns: 220px minmax(0, 1fr) 300px; gap: 28px; align-items: start; padding-top: 6px;">'
            f'{left}{apm_document()}{provenance_rail()}</div>')
    return shell("Audit planning memorandum", header + verdict + body, 1160)


# =============================================================================
# Draft audit report
# =============================================================================
def report_outline() -> str:
    red = dot("bad", 7)
    entries = [
        ("A. Executive summary", False, ""),
        ("1. Introduction", False, ""),
        ("2. Objective and scope", False, ""),
        ("3. Audit conclusion", True, red),
        ("4. Key findings", False, ""),
        ("5. Summary of findings", False, ""),
        ("B. Detailed findings · 18", False, count_badge("excluded", "warn")),
        ("1. Requisitions approved above the authority's limit", False, ""),
        ("2. Invoices exceeding purchase order totals", False, ""),
        ("3. Payment released to an inactive vendor", False, ""),
        ("… 15 more", False, ""),
    ]
    gen = (f'<div style="margin-top: 16px; padding: 10px 12px; border: 1px solid {C["border"]}; border-radius: 8px; background: {C["panel"]}; display: flex; flex-direction: column; gap: 4px;">'
           f'<span style="color: {C["muted"]}; font-size: 11px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase;">Generated</span>'
           f'<span class="num" style="color: {C["ink_soft"]}; font-size: 12px;">3 Sep 12:01 · by the assistant · from the register as it then stood</span>'
           f'<span style="color: {C["ink_soft"]}; font-size: 12px;">Not edited by a person</span></div>')
    return (f'<div style="display: flex; flex-direction: column; gap: 2px; align-self: start; position: sticky; top: 20px;">'
            + outline("On this report", entries) + gen + "</div>")

def key_findings_table() -> str:
    grid = "28px 130px minmax(0, 1fr) 70px"
    head = (f'<div style="display: grid; grid-template-columns: {grid}; gap: 0 10px; padding: 6px 10px; background: {C["raised"]}; '
            f'color: {C["muted"]}; font-size: 11px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase;">'
            f'<span>#</span><span>Process</span><span>Key finding</span><span>Risk</span></div>')
    row = lambda n, p, t, r, col: (f'<div style="display: grid; grid-template-columns: {grid}; gap: 0 10px; padding: 7px 10px; border-top: 1px solid {C["border"]}; font-size: 12.8px; color: {C["ink"]}; line-height: 1.4;">'
                                   f'<span class="num">{n}</span><span>{p}</span><span>{t}</span><span style="color: {col}; font-weight: 600;">{r}</span></div>')
    return (f'<div style="border: 1px solid {C["border"]}; border-radius: 8px; overflow: hidden;">{head}'
            + row("1", "Requisition initiation and approval", "Two cases totalling 68.2 million where commitments were approved above the approver's delegated financial authority limit.", "Critical", C["danger_ink"])
            + row("2", "Invoice processing and payment", "Three cases totalling 45.7 million where invoice amounts exceeded the related order totals and the excess was not referred back to Procurement before payment.", "High", C["danger"])
            + row("3", "", "One payment totalling 950,000 was released to a vendor whose status was not Active, bypassing the vendor eligibility control.", "High", C["danger"])
            + f'<div style="padding: 7px 10px; border-top: 1px solid {C["border"]}; color: {C["muted"]}; font-size: 12px;">… 6 more rows</div></div>')

def report_document() -> str:
    limits = "".join(f'<li style="margin: 0 0 4px;">{t}</li>' for t in [
        "Evidence that approvals and supporting records were completed before the transaction is not consistently available, preventing a conclusion on the timeliness of approvals.",
        "Supporting documentation for transactions and goods receipts may be incomplete, preventing a conclusion on the completeness and accuracy of the records reviewed.",
        "The authority of the governing documents — the Procurement SOP and the Financial Approval Matrix — could not be confirmed because their versions, effective dates, and owners are not stated.",
        "The department has not carried out a systematic review of exceptions, preventing a conclusion on how exceptions were identified and managed over the period.",
    ])
    conclusion = (danger_strip("Asserts a <b>Marginal</b> rating. No overall rating can be given while fieldwork, evidence or auditor judgment remains open; 32 risks have no test run.", link("Open check", C["danger_ink"]))
                  + doc_p("<b style=\"font-weight: 600;\">Marginal</b>. The overall procure-to-pay control environment is weak: 12 of the 24 controls assessed, including most high-risk controls over invoice processing and payment, are ineffective. A critical control breakdown was confirmed in requisition approval, and high-risk weaknesses cluster in the payment stage, exposing the Bank to unauthorized commitments, excess payments, and payments to inactive or unvetted vendors."))
    inner = (f'<div style="display: flex; flex-direction: column; gap: 6px;">{eyebrow("Draft audit report · Procurement")}'
             f'<h2 style="margin: 0; color: {C["ink_strong"]}; font-size: 21.6px; font-weight: 700; letter-spacing: -0.01em; line-height: 1.3;">Internal Audit Report</h2>'
             + danger_strip("Not labelled as a preliminary draft. Open fieldwork remains, so the title page must say so.", link("Add the label", C["danger_ink"])) + "</div>"
             + f'<h3 style="margin: 4px 0 0; color: {C["ink_strong"]}; font-size: 17px; font-weight: 700;">A. Executive Summary</h3>'
             + doc_section("1. Introduction", doc_p("Internal Audit conducted a risk-based review of Global Bank's Procurement function. The review covered the procure-to-pay cycle for the period January 2024 to January 2025. The process comprises requisition and approval, purchase orders, goods receipt, and invoice processing and payment, as operated through the Bank's ERP system."))
             + doc_section("2. Objective and Scope",
                           doc_p("The objective was to perform a risk-based review of the procure-to-pay cycle, assessing whether controls over requisition and approval, purchase orders, goods receipt, and invoice processing and payment operated as intended in supporting the Bank's expanded procurement volume and complexity."),
                           f'<p style="margin: 6px 0 0; color: {C["ink_strong"]}; font-size: 14px; font-weight: 600;">Scope limitations</p>'
                           f'<ul style="margin: 0; padding-left: 20px; color: {C["ink"]}; font-size: 14px; line-height: 1.6;">{limits}</ul>')
             + doc_section("3. Audit Conclusion", conclusion)
             + doc_section("4. Key Findings", key_findings_table()))
    return doc_card(inner)

def report_issues_rail() -> str:
    issue = lambda text, tone, where: (f'<div style="display: flex; align-items: flex-start; gap: 8px; padding: 6px 0; border-top: 1px solid {C["border"]};">'
                                       f'<span style="margin-top: 5px;">{dot(tone, 7)}</span><div style="display: flex; flex-direction: column; gap: 2px; flex: 1;">'
                                       f'<span style="color: {C["ink"]}; font-size: 12.5px; line-height: 1.4;">{text}</span>{link(where, C["teal"])}</div></div>')
    frow = lambda fid, why: (f'<div style="display: flex; align-items: center; gap: 8px; padding: 5px 0; border-top: 1px solid {C["border"]};">'
                             f'{mono(fid, 11.5, C["ink_strong"])}<span class="ell" style="flex: 1; color: {C["muted"]}; font-size: 11.5px;">{why}</span>{ic("chev_right", 12, C["muted"])}</div>')
    about = (f'<span style="color: {C["muted"]}; font-size: 10.5px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;">About the report · 2</span>'
             + issue("Not labelled as a preliminary draft", "bad", "Title")
             + issue("Asserts a rating nothing supports", "bad", "3. Audit conclusion"))
    findings = (f'<span style="color: {C["muted"]}; font-size: 10.5px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; padding-top: 8px;">Findings it cannot include · 18</span>'
                f'<span style="color: {C["ink_soft"]}; font-size: 12px; line-height: 1.45;">Every finding in the register is unsupported: none names a risk, and each cites a test result that has moved since it was drafted.</span>'
                + frow("F-0571DE", "no risk · evidence moved") + frow("F-43AEAB", "no risk · evidence moved") + frow("F-59EBEB", "no risk · evidence moved")
                + frow("F-9EAC80", "no risk · test missing") + f'<div style="padding-top: 6px;">{chevron_link("14 more · open the register")}</div>')
    issues = side_card("Issues", about + findings, "56 · checked 5 Sep 06:47")
    bands = [("Critical", 2, C["danger"]), ("High", 13, C["high"]), ("Medium", 16, C["low"]), ("Low", 1, C["ok"])]
    total = sum(n for _, n, _ in bands)
    bar = "".join(f'<i style="display: block; width: {n / total * 100:.1f}%; background: {col};"></i>' for _, n, col in bands)
    legend = "".join(f'<span style="display: inline-flex; align-items: center; gap: 4px;"><s style="width: 7px; height: 7px; border-radius: 2px; background: {col}; text-decoration: none;"></s>{n} {name.lower()}</span>' for name, n, col in bands)
    drawn = (f'<div style="display: flex; height: 8px; border-radius: 999px; overflow: hidden;">{bar}</div>'
             f'<div class="num" style="display: flex; flex-wrap: wrap; gap: 2px 10px; color: {C["muted"]}; font-size: 11px;">{legend}</div>'
             + kv_row("Risks in the matrix", "32") + kv_row("Tests run", "0", C["warn"]) + kv_row("Findings included", "0")
             + kv_row("Excluded until supported", "18", C["warn"]) + kv_row("Scope limitations recorded", "none"))
    notes = "".join(f'<p style="margin: 0; padding: 6px 0; border-top: 1px solid {C["border"]}; color: {C["ink_soft"]}; font-size: 12px; line-height: 1.45;">{t}</p>' for t in [
        "One control conclusion was limited by the evidence obtained rather than by what the tests found: no executed test names the subject of the accuracy requirement.",
        "No data test evaluates 10 imported columns: po_data (5 of 11), staff_details (3 of 4), invoice_data (1 of 15) and requisitions (1 of 14).",
        "Six exploratory procedures that flagged records were reviewed and judged not to evidence a control failure; each carries a recorded reason.",
    ])
    return (f'<div style="display: flex; flex-direction: column; gap: 12px;">{issues}'
            + side_card("Drawn from", drawn, "3 Sep 12:01") + side_card("Generation notes", notes, "3") + "</div>")

def artboard_report() -> str:
    header = page_header("Draft audit report", "18 findings drafted in · generated 3 Sep 12:01 · not edited since", [
        btn("Edit", icon="pencil"),
        btn("Check quality", icon="shield"),
        btn("Regenerate", "primary", icon="sparkles"),
        kebab(),
    ])
    verdict = verdict_bar(
        "bad",
        f'2 issues with the report and 18 findings it cannot include <span class="num" style="color: {C["muted"]}; font-size: 12.8px; font-weight: 500;">· checked 5 Sep 06:47</span>',
        f'Generated by the assistant 3 Sep 12:01 from the register as it then stood. {accent("No auditor has read or edited it.")}',
        [btn("Editorial review", icon="sparkles"), btn("Check again", icon="refresh")],
        stale="Fieldwork is still open: 32 risks have no test run and every finding has lost its evidence since generation. The draft must be labelled preliminary and cannot carry an overall rating.",
    )
    body = (f'<div style="display: grid; grid-template-columns: 220px minmax(0, 1fr) 320px; gap: 28px; align-items: start; padding-top: 6px;">'
            f'{report_outline()}{report_document()}{report_issues_rail()}</div>')
    return shell("Report", header + verdict + body, 1240)


# =============================================================================
# Assistant panel: one surface, three widths
# =============================================================================
def spinner(size: int = 14, color: str | None = None) -> str:
    col = color or C["info"]
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="{col}" stroke-width="2.5" stroke-linecap="round">'
            f'<circle cx="12" cy="12" r="9" stroke-opacity="0.25"></circle><path d="M21 12a9 9 0 0 0-9-9"></path></svg>')

def icon_button(name: str, tone: str | None = None) -> str:
    col = tone or C["ink_soft"]
    return f'<span style="display: inline-grid; place-items: center; width: 28px; height: 28px; border-radius: 8px; color: {col};">{ic(name, 15)}</span>'

EXPAND_ICON = '<path d="M15 3h6v6"></path><path d="M9 21H3v-6"></path><path d="M21 3l-7 7"></path><path d="M3 21l7-7"></path>'
DOCK_ICON = '<path d="M4 4v16"></path><path d="M20 12H9"></path><path d="m13 8-4 4 4 4"></path>'
CLOSE_ICON = '<path d="M18 6 6 18"></path><path d="m6 6 12 12"></path>'
ICON.update({"expand": EXPAND_ICON, "dock": DOCK_ICON, "close": CLOSE_ICON,
             "stop": '<circle cx="12" cy="12" r="9"></circle><rect x="9" y="9" width="6" height="6" rx="1"></rect>',
             "send": '<path d="m22 2-7 20-4-9-9-4z"></path><path d="M22 2 11 13"></path>',
             "pause": '<path d="M10 5v14"></path><path d="M14 5v14"></path>'})

def panel_header(title: str, status: str, mode: str) -> str:
    """Title with the chat menu, the run's state as one pill, the frame acts."""
    status_pill = pill(status, "info", 11) if status else ""
    frame_act = icon_button("expand") if mode == "docked" else icon_button("dock")
    return (f'<div style="display: flex; align-items: center; gap: 8px; height: 44px; padding: 0 8px 0 14px; border-bottom: 1px solid {C["border"]}; flex: 0 0 auto;">'
            f'{ic("sparkles", 15, C["teal"])}'
            f'<a href="#" class="ell" style="display: inline-flex; align-items: center; gap: 5px; min-width: 0; color: {C["ink_strong"]}; font-size: 13px; font-weight: 600;"><span class="ell">{title}</span>{ic("chev_down", 11, C["muted"], 2.5)}</a>'
            f'{status_pill}<span style="flex: 1;"></span>{icon_button("plus")}{frame_act}{icon_button("close")}</div>')

def plan_strip(stage: str, progress: str, elapsed: str, stages: str) -> str:
    """While a run works: the stage it is on, one line, the plan behind a chevron."""
    return (f'<div style="display: flex; align-items: center; gap: 10px; padding: 8px 14px; border-bottom: 1px solid {C["border"]}; background: {C["raised"]}; flex: 0 0 auto;">'
            f'{spinner(14)}<span class="ell" style="color: {C["ink_strong"]}; font-size: 12.5px; font-weight: 600;">{stage}</span>'
            f'<span class="num" style="color: {C["muted"]}; font-size: 12px; white-space: nowrap;">{progress} · {elapsed}</span><span style="flex: 1;"></span>'
            f'{chevron_link(stages)}</div>')

def user_bubble(text: str, when: str) -> str:
    return (f'<div style="display: flex; flex-direction: column; align-items: flex-end; gap: 3px; align-self: flex-end; max-width: 78%;">'
            f'<span style="padding: 8px 12px; border-radius: 12px 12px 3px 12px; background: {C["teal"]}; color: #ffffff; font-size: 13px; line-height: 1.45;">{text}</span>'
            f'<span class="num" style="color: {C["muted"]}; font-size: 11px;">{when}</span></div>')

def assistant_line(text: str) -> str:
    return f'<p style="margin: 0; color: {C["ink"]}; font-size: 13px; line-height: 1.5; max-width: 92%;">{text}</p>'

def milestone_card(status: str, headline: str, summary: str, metrics: list[tuple[str, str]], highlights: list[tuple[str, str]] = (), link: str = "") -> str:
    ok = status == "completed"
    icon = (f'<span style="display: grid; place-items: center; width: 22px; height: 22px; flex: 0 0 auto; border-radius: 6px; background: {C["ok_soft"] if ok else C["warn_soft"]}; color: {C["ok"] if ok else C["warn_ink"]};">'
            f'{ic("check" if ok else "warning", 13, width=2.5)}</span>')
    metric_row = f'<div class="num" style="display: flex; flex-wrap: wrap; gap: 4px 14px; color: {C["muted"]}; font-size: 11.5px;">' + "".join(
        f'<span>{label} <b style="color: {C["ink_strong"]}; font-weight: 600;">{value}</b></span>' for label, value in metrics) + '</div>'
    rows = "".join(
        f'<div style="display: flex; align-items: flex-start; gap: 8px; padding-top: 6px;"><span style="margin-top: 5px;">{dot("bad", 7)}</span>'
        f'<span style="display: flex; flex-direction: column; gap: 1px;"><span style="color: {C["danger_ink"]}; font-size: 12.5px; font-weight: 600; line-height: 1.4;">{label}</span>'
        f'<span style="color: {C["ink_soft"]}; font-size: 11.5px;">{detail}</span></span></div>' for label, detail in highlights)
    high = f'<div style="display: flex; flex-direction: column; border-top: 1px solid {C["border"]}; margin-top: 2px;">{rows}</div>' if highlights else ""
    link_html = f'<div style="padding-top: 8px;">{tag_chip(link, "neutral", "map", mono_text=False)}</div>' if link else ""
    return (f'<div style="display: flex; gap: 10px; padding: 10px 12px; border: 1px solid {C["border"]}; border-radius: 8px; background: {C["panel"]}; max-width: 92%;">{icon}'
            f'<div style="display: flex; flex-direction: column; gap: 4px; min-width: 0; flex: 1;">'
            f'<span style="color: {C["ink_strong"]}; font-size: 13.5px; font-weight: 600; line-height: 1.35;">{headline}</span>'
            f'<span style="color: {C["ink_soft"]}; font-size: 12.5px; line-height: 1.45;">{summary}</span>{metric_row}{high}{link_html}</div></div>')

def working_block(label: str, elapsed: str, items: list[tuple[str, bool]]) -> str:
    rows = "".join(
        f'<div style="display: flex; align-items: center; gap: 8px; color: {C["ink_soft"] if done else C["ink_strong"]}; font-size: 12.5px;">'
        f'{ic("check", 12, C["ok"], 2.5) if done else spinner(12)}<span class="ell">{text}</span></div>' for text, done in items)
    return (f'<div style="display: flex; flex-direction: column; gap: 6px; max-width: 92%;">'
            f'<div style="display: flex; align-items: center; gap: 8px;">{spinner(14)}<span style="color: {C["ink_strong"]}; font-size: 13px; font-weight: 600;">{label}</span>'
            f'<span class="num" style="color: {C["muted"]}; font-size: 12px;">{elapsed}</span></div>'
            f'<div style="display: flex; flex-direction: column; gap: 4px; padding-left: 22px;">{rows}</div></div>')

def run_receipt(title: str, subline: str, lines: list[tuple[str, str]], failed: bool = False) -> str:
    border = C["danger_line"] if failed else C["border"]
    icon = ic("warning", 12, C["danger"]) if failed else ic("sparkles", 12, C["teal"])
    narration = "".join(
        f'<div style="display: flex; align-items: center; gap: 8px; color: {C["muted"]}; font-size: 11.5px;">'
        f'{ic("check", 11, C["ok"], 2.5) if tone == "done" else ic("chev_right", 11, C["border_strong"], 2.5) if tone == "step" else ic("warning", 11, C["danger"])}<span class="ell">{text}</span></div>' for text, tone in lines)
    return (f'<div style="display: flex; flex-direction: column; gap: 6px; padding: 8px 10px; border: 1px solid {border}; border-radius: 8px; background: {C["canvas"]}; max-width: 92%;">'
            f'<div style="display: flex; align-items: center; gap: 8px;"><span style="display: grid; place-items: center; width: 20px; height: 20px; border-radius: 6px; background: {C["danger_soft"] if failed else C["teal_soft"]};">{icon}</span>'
            f'<span style="display: flex; flex-direction: column; min-width: 0;"><span class="ell" style="color: {C["ink"]}; font-size: 12px; font-weight: 500;">{title}</span>'
            f'<span class="num" style="color: {C["muted"]}; font-size: 11px;">{subline}</span></span></div>'
            f'<div style="display: flex; flex-direction: column; gap: 2px; padding-left: 28px;">{narration}</div></div>')

def composer(run_active: bool, width: int | None = None) -> str:
    stop = (f'<span style="display: inline-flex; align-items: center; gap: 5px; padding: 5px 10px; border: 1px solid {C["danger_line"]}; border-radius: 8px; color: {C["danger"]}; font-size: 12px; font-weight: 600;">{ic("stop", 13)}Stop</span>' if run_active else "")
    placeholder = "Send a message to steer the run…" if run_active else "Ask a question, or type / for commands…"
    w = f"max-width: {width}px; width: 100%; margin: 0 auto;" if width else ""
    return (f'<div style="padding: 10px 12px 12px; border-top: 1px solid {C["border"]}; background: {C["canvas"]}; flex: 0 0 auto;">'
            f'<div style="display: flex; flex-direction: column; gap: 8px; padding: 8px 10px 8px; border: 1px solid {C["border_strong"]}; border-radius: 8px; background: {C["panel"]}; {w}">'
            f'<span style="color: {C["muted"]}; font-size: 13px; padding: 2px 2px 4px;">{placeholder}</span>'
            f'<div style="display: flex; align-items: center; gap: 6px;">'
            f'<span style="display: inline-flex; align-items: center; gap: 5px; padding: 4px 9px; border: 1px solid {C["border_strong"]}; border-radius: 8px; color: {C["ink_soft"]}; font-size: 11.5px; font-weight: 600;">{ic("paperclip", 12)}Context</span>'
            f'<span style="display: inline-flex; align-items: center; gap: 5px; padding: 4px 9px; border: 1px solid {C["border_strong"]}; border-radius: 8px; color: {C["ink_soft"]}; font-size: 11.5px; font-weight: 600;">Auto{ic("chev_down", 11, width=2.5)}</span>'
            f'<span style="flex: 1;"></span>{stop}'
            f'<span style="display: inline-flex; align-items: center; gap: 6px; padding: 5px 12px; border-radius: 8px; background: {C["teal"]}; color: #ffffff; font-size: 12px; font-weight: 700;">Send{ic("send", 12)}</span>'
            f'</div></div></div>')

TRANSCRIPT = [
    ("user", ("Draft findings.", "2 Sep 00:00")),
    ("line", "I'll work through executable test specifications, analyses placed in the matrix, fieldwork execution, results and observations, then eligible finding drafts. Sources, document analysis, the planning context, the memorandum and the matrix are already done, so I'll reuse them rather than repeat the work."),
    ("milestone", ("completed_with_issues", "Results and observations updated",
                   "Rolled results into 24 RCM rows. Recorded 63 exceptions across 24 exception observations.",
                   [("RCM rows", "24"), ("Exceptions", "63"), ("Observations", "24")],
                   [("Payment may be released before goods receipt is recorded.", "9 exceptions across 1 completed test — control concluded ineffective."),
                    ("Procurement transactions may be omitted from the accounting records.", "9 exceptions across 1 completed test — control concluded ineffective."),
                    ("Invoices may be paid without the required verification and approval.", "9 exceptions across 2 completed tests — control concluded ineffective.")],
                   "View RCM")),
    ("line", "Results and observations is done — now working on eligible finding drafts."),
    ("working", ("Drafting evidence-linked findings", "9 of 24 · 2m 10s · taking a while",
                 [("Requisitions approved above the authority's approval limit", True),
                  ("Purchase orders placed with a vendor whose master status was Inactive", True),
                  ("Requisitions split by vendor and department to remain below the approval threshold", False)])),
]

def transcript(items=TRANSCRIPT, width: int | None = None, gap: int = 14, pad: str = "14px 14px") -> str:
    parts = []
    for kind, payload in items:
        if kind == "user": parts.append(user_bubble(*payload))
        elif kind == "line": parts.append(assistant_line(payload))
        elif kind == "milestone": parts.append(milestone_card(*payload))
        elif kind == "working": parts.append(working_block(*payload))
        elif kind == "receipt": parts.append(run_receipt(*payload))
    w = f"max-width: {width}px; width: 100%; margin: 0 auto;" if width else ""
    return (f'<div style="flex: 1; min-height: 0; overflow: hidden; display: flex; flex-direction: column;">'
            f'<div style="display: flex; flex-direction: column; gap: {gap}px; padding: {pad}; {w}">{"".join(parts)}</div></div>')

def assistant_docked(width: int = 440) -> str:
    inner = (panel_header("Generate planned test for 1 RCM row.", "running", "docked")
             + plan_strip("Eligible finding drafts", "stage 5 of 5", "2m 10s", "Plan")
             + transcript()
             + composer(run_active=True))
    return (f'<aside style="display: flex; flex-direction: column; flex: 0 0 {width}px; width: {width}px; min-height: 0; '
            f'border-left: 1px solid {C["border"]}; background: {C["panel"]}; overflow: hidden;">{inner}</aside>')

def chat_row(tone: str, title: str, meta: str, active: bool = False) -> str:
    return list_row(tone, title, meta, active)

def chats_column() -> str:
    head = (f'<div style="display: flex; align-items: center; gap: 8px; height: 44px; padding: 0 8px 0 14px; border-bottom: 1px solid {C["border"]};">'
            f'<span style="color: {C["ink_strong"]}; font-size: 13px; font-weight: 600;">Chats</span><span style="flex: 1;"></span>{icon_button("plus")}</div>')
    rows = (chat_row("info", "Generate planned test for 1 RCM row.", f"running · 2 Sep 00:00 · 5 messages", True)
            + chat_row("warn", "Analyse the imported tables.", "1 Sep 23:05 · 10 messages · 3 runs with failures"))
    search = f'<div style="padding: 10px 12px; border-bottom: 1px solid {C["border"]};">{search_box("Search chats")}</div>'
    return (f'<div style="display: flex; flex-direction: column; flex: 0 0 264px; min-height: 0; border-right: 1px solid {C["border"]}; background: {C["panel"]};">'
            f'{head}{search}{rows}</div>')

def plan_column() -> str:
    arc = (f'<div style="display: flex; gap: 3px;">'
           f'<span style="flex: 2; height: 4px; border-radius: 2px; background: {C["info"]};"></span>'
           f'<span style="flex: 3; height: 4px; border-radius: 2px; background: {C["warn"]};"></span>'
           f'<span style="flex: 2; height: 4px; border-radius: 2px; background: {C["warn"]};"></span></div>')
    standing = (arc + f'<span style="color: {C["ink"]}; font-size: 12.5px; line-height: 1.45;"><b style="font-weight: 600;">Planning</b> · 32 RCM rows have no test. Fieldwork and the report need attention.</span>'
                + link("Open the engagement record", C["teal"], "clock"))
    stage = lambda name, state, meta: (f'<div style="display: flex; align-items: center; gap: 8px; padding: 6px 0; border-top: 1px solid {C["border"]};">'
                                       f'{ic("check", 12, C["ok"], 2.5) if state == "done" else spinner(12) if state == "running" else dot("neutral", 8)}'
                                       f'<span class="ell" style="flex: 1; color: {C["ink_strong"] if state != "queued" else C["muted"]}; font-size: 12.5px; font-weight: {600 if state == "running" else 500};">{name}</span>'
                                       f'<span class="num" style="color: {C["muted"]}; font-size: 11px; white-space: nowrap;">{meta}</span></div>')
    plan = (stage("Executable test specifications", "done", "reused") + stage("Analyses placed in the matrix", "done", "reused")
            + stage("Fieldwork execution", "done", "0 to run") + stage("Results and observations", "done", "24 rows · 3s")
            + stage("Eligible finding drafts", "running", "9 of 24 · 2m 10s")
            + f'<div style="padding-top: 8px;">{chevron_link("Units and errors")}</div>')
    doc_row = lambda name, cat: (f'<div style="display: flex; align-items: center; gap: 8px; padding: 4px 0; border-top: 1px solid {C["border"]};">'
                                 f'{ic("file", 12, C["teal"])}<span class="ell" style="flex: 1; color: {C["ink"]}; font-size: 12px;">{name}</span>'
                                 f'<span style="color: {C["muted"]}; font-size: 11px;">{cat}</span></div>')
    read = (f'<span style="color: {C["ink_soft"]}; font-size: 12px; line-height: 1.45;">The target RCM row, the planning context, 12 table metadata items and 7 documents.</span>'
            + doc_row("Procurement SOP Extracts.docx", "policy") + doc_row("Financial Approval Matrix.docx", "policy")
            + doc_row("Minutes of Meeting - Procurement Planning.docx", "minutes") + doc_row("REQ2024009_Purchase_Requisition.pdf", "evidence")
            + f'<div style="padding-top: 6px;">{chevron_link("3 more documents")}</div>'
            + f'<span style="color: {C["warn_ink"]}; font-size: 11.5px; padding-top: 6px; border-top: 1px solid {C["border"]};">Held back: GRN2024004_Signed_Receipt.pdf — outside this step\'s scope.</span>')
    return (f'<div style="display: flex; flex-direction: column; gap: 12px; flex: 0 0 320px; min-height: 0; padding: 14px 14px; border-left: 1px solid {C["border"]}; background: {C["raised"]}; overflow: hidden;">'
            + side_card("Where the engagement stands", standing, "1 of 3")
            + side_card("Plan · Draft findings", plan, "5 stages")
            + side_card("Read for this run", read, "at 00:00") + "</div>")

def assistant_expanded() -> str:
    thread = (f'<div style="display: flex; flex-direction: column; flex: 1; min-width: 0; min-height: 0; background: {C["panel"]};">'
              + panel_header("Generate planned test for 1 RCM row.", "running", "expanded").replace("padding: 0 8px 0 14px", "padding: 0 16px 0 22px")
              + transcript(width=760, gap=16, pad="20px 24px")
              + composer(run_active=True, width=760) + "</div>")
    return f'<div style="display: flex; flex: 1; min-height: 0; align-items: stretch;">{chats_column()}{thread}{plan_column()}</div>'

def artboard_assistant_docked() -> str:
    content = (findings_header() + findings_review_bar(wrap=True)
               + master_detail(findings_list(), findings_detail(narrow=True)))
    return shell("Findings register", content, 1200, assistant="docked", panel=assistant_docked())

def artboard_assistant_expanded() -> str:
    return frame(header_bar("expanded") + assistant_expanded(), 900)

def artboard_assistant_states() -> str:
    """Three widths of one panel, as a diagram: what each shows and what moves between them."""
    def mini(label: str, sub: str, page: bool, panel_w: int, full: bool, notes: list[str]) -> str:
        head = f'<div style="height: 18px; background: #0d2340; border-radius: 6px 6px 0 0; display: flex; align-items: center; justify-content: flex-end; padding: 0 8px;"><span style="width: 26px; height: 8px; border-radius: 3px; background: {"#0d9488" if (panel_w or full) else "rgba(255,255,255,0.35)"};"></span></div>'
        if full:
            body = (f'<div style="display: flex; flex: 1;"><span style="flex: 0 0 22%; background: {C["panel"]}; border-right: 1px solid {C["border"]};"></span>'
                    f'<span style="flex: 1; background: {C["panel"]}; display: flex; justify-content: center;"><span style="width: 62%; margin: 12px 0; border-radius: 4px; background: {C["teal_soft"]};"></span></span>'
                    f'<span style="flex: 0 0 26%; background: {C["raised"]}; border-left: 1px solid {C["border"]};"></span></div>')
        else:
            page_block = f'<span style="flex: 1; background: {C["canvas"]}; display: flex; gap: 6px; padding: 10px;"><span style="flex: 0 0 30%; border-radius: 4px; background: {C["panel"]}; border: 1px solid {C["border"]};"></span><span style="flex: 1; border-radius: 4px; background: {C["panel"]}; border: 1px solid {C["border"]};"></span></span>'
            panel_block = f'<span style="flex: 0 0 {panel_w}%; background: {C["panel"]}; border-left: 1px solid {C["border"]}; display: flex; flex-direction: column;"><span style="flex: 1;"></span><span style="height: 22px; margin: 6px; border-radius: 4px; background: {C["teal_soft"]};"></span></span>' if panel_w else ""
            body = f'<div style="display: flex; flex: 1;">{page_block}{panel_block}</div>'
        frame_ = f'<div style="display: flex; flex-direction: column; width: 360px; height: 210px; border: 1px solid {C["border_strong"]}; border-radius: 6px; overflow: hidden; box-shadow: 0 2px 6px rgb(13 35 64 / 7%), 0 10px 24px rgb(13 35 64 / 6%);">{head}{body}</div>'
        bullets = "".join(f'<li style="margin: 0 0 4px;">{n}</li>' for n in notes)
        return (f'<div style="display: flex; flex-direction: column; gap: 12px; width: 360px;">{frame_}'
                f'<div style="display: flex; flex-direction: column; gap: 4px;"><span style="color: {C["ink_strong"]}; font-size: 15.2px; font-weight: 600;">{label}</span>'
                f'<span class="num" style="color: {C["muted"]}; font-size: 12px;">{sub}</span></div>'
                f'<ul style="margin: 0; padding-left: 18px; color: {C["ink"]}; font-size: 12.8px; line-height: 1.5;">{bullets}</ul></div>')
    arrow = lambda top, bottom: (f'<div style="display: flex; flex-direction: column; align-items: center; gap: 6px; width: 110px; padding-top: 90px; flex: 0 0 auto;">'
                                 f'<span style="color: {C["teal"]}; font-size: 11.5px; font-weight: 600; text-align: center; white-space: nowrap; line-height: 1.4;">{top}</span>'
                                 f'<svg width="80" height="24" viewBox="0 0 80 24" fill="none" stroke="{C["border_strong"]}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 8h64"></path><path d="m62 3 6 5-6 5"></path><path d="M76 16H12"></path><path d="m18 11-6 5 6 5"></path></svg>'
                                 f'<span style="color: {C["muted"]}; font-size: 11.5px; font-weight: 600; text-align: center; white-space: nowrap; line-height: 1.4;">{bottom}</span></div>')
    row = (mini("Closed", "0 px · the page has the whole width", True, 0, False,
                ["The header toggle is the only trace: a dot when a run is live, amber when it needs you.",
                 "Nothing is mounted; the 52 px collapsed rail is gone from every page."])
           + arrow("Assistant toggle<br>a run starting<br>a milestone link", "✕ or the toggle")
           + mini("Docked", "440 px · 360–720 · pushes the page", True, 32, False,
                  ["The default whenever the assistant opens: a question beside the work.",
                   "Chat title menu, one status pill, the live plan strip, transcript, composer.",
                   "Under 1,320 px of window the docked width would leave less than 880 px of page, so this state skips straight to Expanded."])
           + arrow("⤢ in the panel head<br>?assistant=full", "⇥ or Esc")
           + mini("Expanded", "the workspace below the header · the page stays mounted underneath", False, 0, True,
                  ["Chats on the left, the thread at 760 px, the run's plan and what it read on the right.",
                   "Replaces the console route; /console redirects here. Closing returns to the page exactly as it was."]))
    body = (f'<div style="display: flex; flex-direction: column; gap: 22px; padding: 28px 32px; flex: 1;">'
            f'<div style="display: flex; flex-direction: column; gap: 4px;"><span style="color: {C["ink_strong"]}; font-size: 21.6px; font-weight: 700; letter-spacing: -0.01em;">One assistant, three widths</span>'
            f'<span style="color: {C["ink_soft"]}; font-size: 13.5px;">The console route and the drawer are the same thread in two frames. They become one panel whose width is a state, not a place.</span></div>'
            f'<div style="display: flex; gap: 16px; align-items: flex-start;">{row}</div></div>')
    return frame(body, 560)

# =============================================================================
ARTBOARDS = {
    "Main.dc.html": ("Documents, preview", artboard_documents, 1000, "page-views"),
    "DocumentAnalysis.dc.html": ("Documents, analysis tab", artboard_document_analysis, 1240, "page-views"),
    "Tables.dc.html": ("Source tables", artboard_tables, 1140, "page-views"),
    "Findings.dc.html": ("Findings register", artboard_findings, 1200, "page-views"),
    "Apm.dc.html": ("Audit planning memorandum", artboard_apm, 1160, "page-views"),
    "Report.dc.html": ("Draft audit report", artboard_report, 1240, "page-views"),
    "AssistantStates.dc.html": ("One assistant, three widths", artboard_assistant_states, 560, "page-assistant"),
    "AssistantDocked.dc.html": ("Docked beside the findings", artboard_assistant_docked, 1200, "page-assistant"),
    "AssistantExpanded.dc.html": ("Expanded to the workspace", artboard_assistant_expanded, 900, "page-assistant"),
}

PAGES = [
    {"id": "page-views", "name": "Sources, planning, reporting"},
    {"id": "page-assistant", "name": "Assistant"},
]

def main() -> None:
    layout = []
    cursor: dict[str, tuple[int, int]] = {}
    for file, (title, fn, height, page) in ARTBOARDS.items():
        (HERE / file).write_text(fn(), encoding="utf-8")
        index, y = cursor.get(page, (0, 0))
        col = index % 2
        if col == 0 and index:
            y += 1400
        layout.append({"file": file, "title": title, "x": col * 1560, "y": y, "w": 1440, "h": height, "page": page})
        cursor[page] = (index + 1, y)
    canvas = {
        "pages": PAGES,
        "artboards": layout,
        "annotations": [
            {"id": "note-system", "x": 3160, "y": 0, "w": 320, "page": "page-views",
             "text": "Five pages on the fieldwork system: a 36px header with one count sentence and one primary, a review bar whose chips are the filters, a 300px list with a dot and a meta line, and a verdict bar that states what was done and what is recorded. Drawn with the Procurement workspace as of 5 September."},
            {"id": "note-assistant", "x": 3160, "y": 0, "w": 320, "page": "page-assistant",
             "text": "The Assistant tab and the sidebar are one thread in two frames. The proposal keeps one panel with three widths — closed, docked, expanded — so the console route goes and the page underneath never unmounts. Drawn with the Procurement workspace's chats as of 5 September; the run in flight is the Draft findings run as it stood at 00:02 on 2 September."},
        ],
        "launch": {"view": "canvas", "page": "page-assistant"},
    }
    (HERE / "canvas.json").write_text(json.dumps(canvas, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(ARTBOARDS)} artboards and canvas.json to {HERE}")

if __name__ == "__main__":
    main()
