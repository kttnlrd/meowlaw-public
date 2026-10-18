# Branding

Everything visual is driven by `[branding]` in `meowlaw.toml`. No rebuild:
restart `meowlaw serve` and reload the page.

```toml
[branding]
name = "Acme Legal Research"
tagline = "Primary sources, linked"
logo = "brand/acme.svg"          # SVG, PNG, JPG or WebP; relative to meowlaw.toml
favicon = "brand/acme-icon.png"  # defaults to the logo
footer = "Acme Legal Research, internal use"   # optional; nothing by default
font_body = "Inter, system-ui, sans-serif"
font_heading = "'Source Serif 4', Georgia, serif"

[branding.colors]                # light mode
primary = "#0b5cad"
accent = "#d9822b"
bg = "#ffffff"

[branding.dark]                  # dark mode overrides
primary = "#6aa8ff"
bg = "#0f1115"
```

How it works:

- `GET /theme.css` is generated from the config: every key under `colors`
  becomes a CSS custom property (`primary` → `--primary`), and `dark` is
  applied under `prefers-color-scheme: dark` and when the reader picks dark in
  Settings. Light is the default; readers can choose dark or follow the system,
  change the accent colour and the text size, all saved in their browser.
- `GET /api/branding` gives the UI its name, tagline, footer and logo URL.
- `GET /brand/logo` and `/brand/favicon` serve the configured files.
- Keys left out fall back to the defaults, so you can change only `primary`.
- Values containing `;`, `{`, `}`, `<`, `>` or `url(` are dropped.

Variables the UI uses: `bg`, `surface`, `surface-2`, `surface-sunken`, `text`,
`muted`, `border`, `line-strong` (control borders), `primary` (buttons, the
search bar, links in legal text), `primary-contrast` (text on `primary`),
`accent` (case law, hover shadows), `warn` (guidance badges), `highlight`
(search matches, the open tree row), `link`, `text-link` (citations and
defined terms inside legal text; keep it readable on `bg`), plus `--font-body`,
`--font-heading`, `--font-mono`. Add your own and use them in a custom
stylesheet.

Fonts: the defaults are the system font stacks (San Francisco, Segoe UI,
Roboto, and so on), so nothing is downloaded and the UI looks native
everywhere. Point `font_body`, `font_heading` or `font_mono` at any installed
or self-hosted face; the UI loads nothing from third parties.

For deeper changes, edit `ui/app.css` (layout) or point `server.ui_dir` at
your own copy of `ui/`. The UI is plain HTML, CSS and JavaScript against the
REST API, so it can also be replaced entirely.

## Source groups (left drawer)

The drawer groups sources under headings you choose:

```toml
[[source_groups]]
label = "Legislation"
kinds = ["legislation", "instrument"]

[[source_groups]]
label = "Case law"
kinds = ["case"]

[[source_groups]]
label = "Regulatory resources"
kinds = ["guidance", "treaty", "rule_set", "secondary"]
```

- Order in the file is order in the drawer and the source dropdown.
- A source joins the first group whose `kinds` include its kind.
- To put one source in a particular group regardless of kind, set `group`
  in its metadata, e.g. front matter `[meta]` then `group = "Internal
  handbooks"`. A group named only this way is added after the configured ones.
- Sources matching no group appear under "Other".
- Without any `[[source_groups]]` the three groups above are used.
