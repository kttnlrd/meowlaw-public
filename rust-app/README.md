# LawKitty for Windows (native)

A native Windows reader for a LawKitty data folder, written in Rust. It draws its own window:
there is no browser engine, no WebView, no HTML or JavaScript, no local web server and no
listening port. It makes no network calls.

It lives in `rust-app/` and uses the data engine in `local-app/` (`lawkitty-local`:
`catalog.rs`, `tree.rs`, `section.rs`, `search.rs`, `state.rs`) as a library, built without
its `server` feature, so neither axum nor tokio is compiled in.

## Toolkit: egui / eframe 0.37

egui was chosen over Slint and iced because it could reproduce the web app most closely:

- Its text engine (harfrust shaping, skrifa outlines) applies the fonts' kerning, so line
  widths match Chrome's to a hundredth of a pixel and paragraphs wrap at the same words.
- A paragraph can mix fonts, weights, colours and line heights in one layout, which the section
  reader needs (`(1)` markers in JetBrains Mono inside Figtree text, links, note labels).
- Every rectangle, border and glyph is placed by the program, so the web app's CSS
  measurements carry over one for one. Slint has no rich inline text; iced has it but gives
  less control over where a line box sits.
- It starts in about 0.2 seconds with the OpenGL backend.

## What is in it

| Screen | What it does |
| --- | --- |
| Sidebar | Sources in the web app's groups (Pinned, Core legislation, Federal tax, State tax, New Zealand legislation, Other legislation, then any collection groups). A star on each row pins it; pins are saved. Opening a source swaps the list for its contents tree. The sidebar can be dragged between 280 and 600 wide. |
| Home | The lockup, the ball of yarn (a port of `yarn.js`; click it), the search box, and the version, which opens the changelog. |
| Contents | A source's parts, divisions and sections. Large Acts load shallow: a part's sections are read when it is opened. |
| Section | Breadcrumb, Pin, the section text with markers, indents, notes, tables, lists and links, and the licence line with its details. Text can be selected and copied. A defined-term link opens the dictionary section at that term. |
| Search | Full text from `search_index.db` when the data folder has one; otherwise section numbers and titles across every source. `/` opens it. |
| Settings | The "Local user" button: dark or light theme, and where the data is. |

Alt+Left and Alt+Right (or the mouse's back and forward buttons) step through history.

Left out, because they need the online service or were out of scope: accounts, MCP tokens,
admin, bug reports, comments, maps, interpretive chains, the definitions browser and
definition pop-overs (the Definitions button opens the Act's dictionary section instead),
related cases and rulings under a section, search filters (practice areas, Options) and the
other fifteen themes. Rulings, ASIC regulatory guides, AFSA guidance and treaties are not
shown: the engine lists only sources that have a `tree.json`, and the web app run against the
same data folder does not show them either.

## Data folder and settings

The data folder is taken from, in order: `--data <dir>`, the `LAWKITTY_DATA` environment
variable, then `data = <dir>` in `lawkitty.ini` beside `LawKitty.exe` (a relative path there is taken from the program's folder), and finally a `lawkitty-data` folder beside the program.

Pinned sources and the theme are kept in `%LOCALAPPDATA%\LawKitty\state.json`; pinned sections
and the sidebar width in `%LOCALAPPDATA%\LawKitty\desktop.json`.

Other switches: `--open itaa-1997/8-1` (or `itaa-1997`, or `search:words`), `--theme light`,
`--zoom 1`, `--state-dir <dir>`, `--bench <file>` (writes start-up and loading times, then quits).

## Build

```sh
cd rust-app
cargo build --release        # target/release/LawKitty.exe
cargo build --profile fast   # quicker to build while working on it
```

Nothing but `LawKitty.exe` and `lawkitty.ini` is needed to run it: fonts, images and the icon
are inside the exe.

## Fonts

`assets/fonts/` holds the font files Google Fonts serves for the families in
`frontend/index.html`, with their SIL Open Font License texts: Figtree (400, 500, 600, 700),
JetBrains Mono (400, 600) and Bagel Fat One. Source Sans 3 is not bundled: only the "Modern
(AustLII)" theme uses it, and that theme is not in this app. Characters outside the web
fonts' range (`←`, `★`) are drawn in Arial, Segoe UI or Segoe UI Symbol from the Windows fonts
folder, the same fonts the browser falls back to.

## Checking it against the web app

`tools/ref-shot.mjs` screenshots the web app (served by `lawkitty-local`) with headless Edge;
`tools/all-shots.ps1` screenshots this app's window and writes side-by-side sheets with
`tools/compare.py`. The pairs are in `screenshots/`.

Known differences:

- Text is antialiased in greyscale; Chrome on Windows uses ClearType colour fringes. Letter
  positions and line breaks are the same, the edges of letters are not pixel-identical.
- A long hyphenated word wraps whole; Chrome may break it after the hyphen.
- The sidebar has no Bugs button, and the search screen has no practice-area chips or Options
  button, because those features are left out.
- The yarn is the same simulation but not in step with the page, so the ball is rarely in the
  same place in two pictures. One of its shading gradients is approximated.
