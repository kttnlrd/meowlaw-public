//! Colours and fonts: the design tokens of the web app (frontend/src/tokens.css), as values.

use eframe::egui::{Color32, FontFamily, FontId};
use std::sync::LazyLock;

pub const fn rgb(hex: u32) -> Color32 {
    Color32::from_rgb((hex >> 16) as u8, (hex >> 8) as u8, hex as u8)
}

/// One built-in theme. Field names follow the CSS custom properties.
#[derive(Clone, Copy, PartialEq)]
pub struct Theme {
    pub dark: bool,
    pub bg: Color32,
    pub surface: Color32,
    pub surface_hover: Color32,
    pub border: Color32,
    pub text: Color32,
    pub text_muted: Color32,
    pub accent: Color32,
    pub accent_hover: Color32,
    pub heading: Color32,
    pub yarn: Color32,
    pub on_catnip: Color32,
    pub line_strong: Color32,
    pub catnip_soft: Color32,
    /// The colour of the hard offset shadow under the search boxes (`--shadow-pounce`).
    pub shadow_pounce: Color32,
    pub shadow_pounce_yarn: Color32,
}

/// `:root` and `[data-theme="dark"]`.
pub const DARK: Theme = Theme {
    dark: true,
    bg: rgb(0x120d17),
    surface: rgb(0x1e1626),
    surface_hover: rgb(0x291f33),
    border: rgb(0x3a2d45),
    text: rgb(0xf7eee2),
    text_muted: rgb(0xb8a9bf),
    accent: rgb(0xc6f432),
    accent_hover: rgb(0xd6ff5a),
    heading: rgb(0xf7eee2),
    yarn: rgb(0xff7ac8),
    on_catnip: rgb(0x120d17),
    line_strong: rgb(0x7a6788),
    catnip_soft: rgb(0x27310c),
    shadow_pounce: rgb(0xc6f432),
    shadow_pounce_yarn: rgb(0xff7ac8),
};

/// `[data-theme="light"]`.
pub const LIGHT: Theme = Theme {
    dark: false,
    bg: rgb(0xfbf6ee),
    surface: rgb(0xffffff),
    surface_hover: rgb(0xf6efe4),
    border: rgb(0xe6dccd),
    text: rgb(0x1f1726),
    text_muted: rgb(0x5e5166),
    accent: rgb(0xc6f432),
    accent_hover: rgb(0xd6ff5a),
    heading: rgb(0x1f1726),
    yarn: rgb(0xc2185b),
    on_catnip: rgb(0x120d17),
    line_strong: rgb(0x8a7a92),
    catnip_soft: rgb(0xeef5d6),
    shadow_pounce: rgb(0x1f1726),
    shadow_pounce_yarn: rgb(0xc2185b),
};

pub fn by_name(name: &str) -> Theme {
    if name == "light" {
        LIGHT
    } else {
        DARK
    }
}

/// Source-over blend of `top` at `alpha` on an opaque `under` (CSS `opacity`, `rgba()` on a known fill).
pub fn blend(under: Color32, top: Color32, alpha: f32) -> Color32 {
    let mix = |a: u8, b: u8| (a as f32 + (b as f32 - a as f32) * alpha).round() as u8;
    Color32::from_rgb(mix(under.r(), top.r()), mix(under.g(), top.g()), mix(under.b(), top.b()))
}

macro_rules! family {
    ($name:ident, $key:expr) => {
        pub static $name: LazyLock<FontFamily> = LazyLock::new(|| FontFamily::Name($key.into()));
    };
}
family!(SANS_400, "figtree-400");
family!(SANS_500, "figtree-500");
family!(SANS_600, "figtree-600");
family!(SANS_700, "figtree-700");
family!(MONO_400, "mono-400");
family!(MONO_600, "mono-600");
family!(DISPLAY, "display");
family!(SYSTEM, "system");

/// Figtree at a CSS weight.
pub fn sans(size: f32, weight: u16) -> FontId {
    let family = match weight {
        0..=449 => &SANS_400,
        450..=549 => &SANS_500,
        550..=649 => &SANS_600,
        _ => &SANS_700,
    };
    FontId::new(size, (**family).clone())
}

/// JetBrains Mono at a CSS weight.
pub fn mono(size: f32, weight: u16) -> FontId {
    FontId::new(size, (if weight >= 500 { &*MONO_600 } else { &*MONO_400 }).clone())
}

/// Bagel Fat One.
pub fn display(size: f32) -> FontId {
    FontId::new(size, DISPLAY.clone())
}

/// The system UI font, for the few buttons the web app leaves on the browser's default font.
pub fn system(size: f32) -> FontId {
    FontId::new(size, SYSTEM.clone())
}
