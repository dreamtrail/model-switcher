"""Exact CSS and head fragments shared by the statistics and control pages."""

from urllib.parse import quote

_FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
    "<rect width='32' height='32' rx='7' fill='#2a78d6'/>"
    # Top arrow points right, bottom arrow points left (a swap symbol).
    "<path d='M9 12h14M19 8l4 4-4 4M23 21H9M13 17l-4 4 4 4' fill='none'"
    " stroke='#ffffff' stroke-width='2.5' stroke-linecap='round'"
    " stroke-linejoin='round'/>"
    "</svg>"
)

# Tab icon for both pages. The SVG is inlined as a data URI, so no route or
# file is served. quote() encodes every special character (#, <, >, ", :).
FAVICON_LINK = (
    '<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,'
    + quote(_FAVICON_SVG, safe="")
    + '">\n'
)

LIGHT_PALETTE_START_CSS = """  :root {
    color-scheme: light;
    --plane: #f9f9f7;
    --surface: #fcfcfb;
    --ink: #0b0b0b;
    --ink-2: #52514e;
    --muted: #898781;
    --hairline: #e1e0d9;
    --baseline: #c3c2b7;
    --ring: rgba(11, 11, 11, 0.10);
    --wash: rgba(11, 11, 11, 0.03);
    --accent: #2a78d6;
"""

PAGE_BASE_REST_CSS = """    --good: #0ca30c;
    --serious: #ec835a;
    --critical: #d03b3b;
    --good-tint: rgba(12, 163, 12, 0.10);
    --critical-tint: rgba(208, 59, 59, 0.08);
    --shadow: 0 1px 2px rgba(11, 11, 11, 0.04), 0 2px 6px rgba(11, 11, 11, 0.04);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      color-scheme: dark;
      --plane: #0d0d0d;
      --surface: #1a1a19;
      --ink: #ffffff;
      --ink-2: #c3c2b7;
      --muted: #898781;
      --hairline: #2c2c2a;
      --baseline: #383835;
      --ring: rgba(255, 255, 255, 0.10);
      --wash: rgba(255, 255, 255, 0.04);
      --accent: #3987e5;
      --good-tint: rgba(12, 163, 12, 0.18);
      --critical-tint: rgba(208, 59, 59, 0.16);
      --shadow: none;
    }
  }
  *, *::before, *::after { box-sizing: border-box; }
  [hidden] { display: none !important; }
  html { background: var(--plane); }
  body {
    margin: 0; background: var(--plane); color: var(--ink);
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    font-size: 15px; line-height: 1.45;
    -webkit-font-smoothing: antialiased;
  }
  main { max-width: 80rem; margin: 0 auto; padding: 2rem 1.5rem 2.5rem; }
"""

HEADINGS_CSS = """  h1 { margin: 0; font-size: 1.375rem; font-weight: 650; letter-spacing: -0.01em; }
  .subtitle { margin: 0.1rem 0 0; color: var(--ink-2); font-size: 0.9rem; }
"""

STATUS_DOTS_CSS = """  .dot {
    display: inline-block; width: 0.5rem; height: 0.5rem; border-radius: 50%;
    flex: none; background: var(--muted);
  }
  .dot.good { background: var(--good); }
  .dot.serious { background: var(--serious); }
  .dot.critical { background: var(--critical); }
"""

CARDS_AND_KPI_GRID_CSS = """  .card {
    background: var(--surface); border: 1px solid var(--ring);
    border-radius: 12px; box-shadow: var(--shadow); min-width: 0;
  }
  .kpis {
    display: grid; gap: 0.75rem; margin-bottom: 1rem;
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
"""

FOUR_COLUMN_KPIS_CSS = """  @media (min-width: 40rem) { .kpis { grid-template-columns: repeat(4, minmax(0, 1fr)); } }
"""

KPI_TILES_CSS = """  .tile { padding: 0.85rem 1rem 0.8rem; }
  .tile .label { color: var(--ink-2); font-size: 0.8rem; }
  .tile .value {
    margin-top: 0.3rem; font-size: 1.6rem; font-weight: 600; line-height: 1.15;
    letter-spacing: -0.015em; font-variant-numeric: proportional-nums;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
"""

KPI_SUBTEXT_CSS = """  .tile .sub {
    margin-top: 0.3rem; color: var(--muted); font-size: 0.78rem; line-height: 1.35;
  }
"""

KPI_SUBDOT_CSS = """  .tile .sub .dot { margin-right: 0.35rem; vertical-align: 0.05em; }
"""

SECTIONS_CSS = """  .section { margin-bottom: 1rem; }
  .section h2 {
    margin: 0; padding: 0.9rem 1.25rem 0.6rem;
    font-size: 0.95rem; font-weight: 600;
  }
"""

NARROW_LAYOUT_CSS = """  @media (max-width: 34rem) {
    main { padding: 1.25rem 0.875rem 2rem; }
    .tile .value { font-size: 1.35rem; }
  }
"""
