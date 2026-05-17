"""CloudLens themes — Rosé Pine Moon and Dawn.

The dark variant (Moon) is the canonical CloudLens look: a quiet plum-black
canvas with rose, foam, and iris accents that read as a stable identity
across panels, severity dots, and trace markers. Dawn is the daylight
counterpart — warm parchment with deepened ink for terminals in sunlight.

Both themes expose the same semantic slots so widget CSS resolves correctly
under either. The three surface levels (sunken / base / raised) come from
the Pencil mockups: `surface` is the status-bar / search-input depression,
`background` is the table canvas, `panel` is the header / footer / modal
chrome that sits on top.
"""

from __future__ import annotations

from textual.theme import Theme


CLOUDLENS_MOON = Theme(
    name="rose-pine-moon",
    dark=True,
    background="#232136",   # base — table canvas
    surface="#1f1d2e",      # overlay — status bar, search input, message box
    panel="#2a273f",        # surface — header, footer, modal chrome
    boost="#312e48",        # highlight-high — selected / hovered rows
    foreground="#e0def4",   # text
    primary="#eb6f92",      # love — brand, error, anchor marker
    secondary="#c4a7e7",    # iris — trace marker, secondary accent
    accent="#9ccfd8",       # foam — LIVE indicator, active border
    success="#9ccfd8",      # foam (Rosé Pine has no green; foam reads as positive)
    warning="#f6c177",      # gold — WARN dot, unseen counter
    error="#eb6f92",        # love
)


CLOUDLENS_DAWN = Theme(
    name="rose-pine-dawn",
    dark=False,
    background="#faf4ed",   # base
    surface="#f2e9e1",      # overlay — sunken
    panel="#fffaf3",        # surface — raised
    boost="#cecacd",        # highlight-high — selected
    foreground="#575279",   # text
    primary="#b4637a",      # love
    secondary="#907aa9",    # iris
    accent="#56949f",       # foam
    success="#56949f",
    warning="#ea9d34",      # gold
    error="#b4637a",
)


THEMES = (CLOUDLENS_MOON, CLOUDLENS_DAWN)
DEFAULT_THEME = CLOUDLENS_MOON.name
