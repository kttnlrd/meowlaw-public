//! LawKitty for Windows: a native reader over a downloaded data folder.
//!
//! One window drawn by this program itself (egui): no browser engine, no web server, no
//! socket. The data engine is the `lawkitty-local` library, used directly.
#![windows_subsystem = "windows"]

mod app;
mod engine;
mod gfx;
mod md;
mod reader;
mod theme;
mod treeview;
mod yarn;

use eframe::egui::{self, FontData, FontDefinitions, FontFamily};
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Instant;

const USAGE: &str = "LawKitty

Usage: LawKitty [--data <dir>] [--open <where>] [--theme dark|light] [--state-dir <dir>] [--zoom <n>] [--bench <file>]

  --data <dir>       Data folder. Otherwise LAWKITTY_DATA, then `data = ...` in lawkitty.ini beside the program.
  --open <where>     Start at a source (itaa-1997), a section (itaa-1997/8-1) or a search (search:words).
  --theme <name>     dark or light, for this run.
  --state-dir <dir>  Where pinned sources and window settings are kept. Default: %LOCALAPPDATA%\\LawKitty.
  --zoom <n>         Pixels per point, overriding the display scale.
  --bench <file>     Write start-up and loading times to <file>, then quit.";

/// `key = value` lines of the settings file beside the program.
fn ini_value(key: &str) -> Option<String> {
    let exe = std::env::current_exe().ok()?;
    let text = std::fs::read_to_string(exe.parent()?.join("lawkitty.ini")).ok()?;
    text.lines().filter(|l| !l.trim_start().starts_with(['#', ';'])).find_map(|l| {
        let (k, v) = l.split_once('=')?;
        (k.trim().eq_ignore_ascii_case(key) && !v.trim().is_empty()).then(|| v.trim().trim_matches('"').to_string())
    })
}

fn fonts() -> FontDefinitions {
    let mut defs = FontDefinitions::empty();
    // The files Google Fonts serves for the families and weights in frontend/index.html. They
    // hold the Latin range only, as the web fonts a browser loads do, so the same characters
    // (arrows, stars) fall through to the system font here as there.
    let bundled: [(&str, &'static [u8]); 7] = [
        ("figtree-400", include_bytes!("../assets/fonts/Figtree-Regular.ttf")),
        ("figtree-500", include_bytes!("../assets/fonts/Figtree-Medium.ttf")),
        ("figtree-600", include_bytes!("../assets/fonts/Figtree-SemiBold.ttf")),
        ("figtree-700", include_bytes!("../assets/fonts/Figtree-Bold.ttf")),
        ("mono-400", include_bytes!("../assets/fonts/JetBrainsMono-Regular.ttf")),
        ("mono-600", include_bytes!("../assets/fonts/JetBrainsMono-SemiBold.ttf")),
        ("display", include_bytes!("../assets/fonts/BagelFatOne-Regular.ttf")),
    ];
    for (name, bytes) in bundled {
        defs.font_data.insert(name.to_string(), Arc::new(FontData::from_static(bytes)));
    }
    // Characters the bundled fonts lack fall back to fonts already on this computer, as a
    // browser falls back: read from the Windows fonts folder, never fetched.
    let windir = std::env::var_os("WINDIR").map(PathBuf::from).unwrap_or_else(|| PathBuf::from(r"C:\Windows"));
    let mut fallback = Vec::new();
    for (name, file) in [("segoe-ui", "segoeui.ttf"), ("segoe-symbol", "seguisym.ttf"), ("arial", "arial.ttf")] {
        if let Ok(bytes) = std::fs::read(windir.join("Fonts").join(file)) {
            defs.font_data.insert(name.to_string(), Arc::new(FontData::from_owned(bytes)));
            fallback.push(name.to_string());
        }
    }
    let chain = |first: &[&str]| -> Vec<String> {
        let mut names: Vec<String> = first.iter().map(|n| n.to_string()).filter(|n| defs.font_data.contains_key(n)).collect();
        names.extend(fallback.iter().filter(|f| !first.contains(&f.as_str())).cloned());
        names
    };
    let mut families = Vec::new();
    for (name, _) in bundled {
        families.push((name, chain(&[name, "segoe-ui"])));
    }
    // The few buttons the web app leaves on the browser's default font: Arial on Windows.
    families.push(("system", chain(&["arial"])));
    // Characters outside the web fonts' range: Arial where the stack ends in `sans-serif`,
    // Segoe UI where it names `system-ui`.
    families.push(("fallback-ui", chain(&["arial"])));
    families.push(("fallback-sans", chain(&["segoe-ui"])));
    for (name, list) in families {
        defs.families.insert(FontFamily::Name(name.into()), list);
    }
    defs.families.insert(FontFamily::Proportional, chain(&["figtree-400", "segoe-ui"]));
    defs.families.insert(FontFamily::Monospace, chain(&["mono-400", "segoe-ui"]));
    defs
}

fn icon() -> Option<egui::IconData> {
    let img = image::load_from_memory_with_format(include_bytes!("../assets/favicon-192.png"), image::ImageFormat::Png).ok()?.to_rgba8();
    Some(egui::IconData { width: img.width(), height: img.height(), rgba: img.into_raw() })
}

fn main() {
    let started = Instant::now();
    let env = |k: &str| std::env::var_os(k).filter(|v| !v.is_empty()).map(PathBuf::from);
    let mut data = env("LAWKITTY_DATA");
    let mut data_from_arg = false;
    let mut state_dir = None;
    let (mut open, mut theme, mut zoom, mut bench) = (None, None, None, None);
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        let mut value = || args.next().unwrap_or_default();
        match arg.as_str() {
            "--data" => {
                data = Some(value().into());
                data_from_arg = true;
            }
            "--state-dir" => state_dir = Some(PathBuf::from(value())),
            "--open" => open = Some(value()),
            "--theme" => theme = Some(value()),
            "--zoom" => zoom = value().parse().ok(),
            "--bench" => bench = Some(PathBuf::from(value())),
            _ => {
                // No console to print to: leave the usage text beside the settings.
                let _ = std::fs::write(std::env::temp_dir().join("LawKitty-usage.txt"), USAGE);
            }
        }
    }
    let _ = data_from_arg;
    let data = data
        // a relative path in lawkitty.ini is beside the program, wherever it was started from
        .or_else(|| ini_value("data").map(PathBuf::from).map(|p| match (p.is_relative(), std::env::current_exe()) {
            (true, Ok(exe)) => exe.parent().map(|d| d.join(&p)).unwrap_or(p),
            _ => p,
        }))
        .unwrap_or_else(|| std::env::current_exe().ok().and_then(|e| e.parent().map(|d| d.join("lawkitty-data"))).unwrap_or_else(|| PathBuf::from("lawkitty-data")));
    let state_dir = state_dir.unwrap_or_else(|| env("LOCALAPPDATA").or_else(|| env("APPDATA")).unwrap_or_else(std::env::temp_dir).join("LawKitty"));

    let mut viewport = egui::ViewportBuilder::default().with_title("lawkitty").with_inner_size([1440.0, 900.0]).with_min_inner_size([900.0, 560.0]);
    if let Some(icon) = icon() {
        viewport = viewport.with_icon(icon);
    }
    let options = eframe::NativeOptions { viewport, centered: true, ..Default::default() };
    let opts = app::Options { data, state_dir, open, theme, zoom, bench, started };
    let _ = eframe::run_native(
        "LawKitty",
        options,
        Box::new(move |cc| {
            cc.egui_ctx.set_fonts(fonts());
            Ok(Box::new(app::LawKitty::new(cc, opts)))
        }),
    );
}
