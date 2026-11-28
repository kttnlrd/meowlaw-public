//! The little that a single local user can change: pinned sources and display preferences,
//! kept in one small JSON file. There are no accounts; every request is the same local user.

use serde_json::{json, Map, Value};
use std::fs;
use std::path::PathBuf;
use std::sync::Mutex;

pub const LOCAL_EMAIL: &str = "local@localhost";
pub const LOCAL_NAME: &str = "Local user";

/// `DEFAULT_PREFS` in backend/services/user_prefs.py, in its order.
const DEFAULT_PREFS: [(&str, &str); 8] = [
    ("display_name", ""),
    ("default_act", "itaa-1997"),
    ("theme", "dark"),
    ("accent_color", "#c6f432"),
    ("text_color", "#aebec2"),
    ("bg_color", "#0a1214"),
    ("heading_font", "Montserrat"),
    ("body_font", "Lora"),
];
/// The fields `PUT /api/user/prefs` accepts (`PrefsUpdate`).
const UPDATABLE: [&str; 6] = ["display_name", "default_act", "theme", "accent_color", "heading_font", "body_font"];

#[derive(Default)]
struct Data {
    loaded: bool,
    pinned: Vec<String>,
    prefs: Option<Map<String, Value>>,
}

pub struct LocalState {
    path: PathBuf,
    data: Mutex<Data>,
}

impl LocalState {
    pub fn new(path: PathBuf) -> LocalState {
        LocalState { path, data: Mutex::new(Data::default()) }
    }

    /// Read on first use, not at startup.
    fn with<T>(&self, f: impl FnOnce(&mut Data) -> T) -> T {
        let mut d = self.data.lock().unwrap();
        if !d.loaded {
            d.loaded = true;
            if let Some(v) = fs::read(&self.path).ok().and_then(|b| serde_json::from_slice::<Value>(&b).ok()) {
                d.pinned = v["pinned_sources"]
                    .as_array()
                    .map(|a| a.iter().filter_map(|s| s.as_str().map(String::from)).collect())
                    .unwrap_or_default();
                d.prefs = v["prefs"].as_object().cloned();
            }
        }
        f(&mut d)
    }

    fn save(&self, d: &Data) {
        let body = json!({"pinned_sources": d.pinned, "prefs": d.prefs});
        if let Some(dir) = self.path.parent() {
            let _ = fs::create_dir_all(dir);
        }
        if let Err(e) = fs::write(&self.path, serde_json::to_vec_pretty(&body).unwrap_or_default()) {
            eprintln!("lawkitty-local: could not save {}: {e}", self.path.display());
        }
    }

    pub fn pinned(&self) -> Value {
        self.with(|d| json!({"sources": d.pinned}))
    }

    /// Same cleaning as the Python route: lower-case ids, no duplicates, at most 50.
    pub fn set_pinned(&self, sources: &[Value]) -> Value {
        let mut ids: Vec<String> = Vec::new();
        for s in sources {
            let s = s.as_str().unwrap_or("").trim().to_lowercase();
            let ok = (1..=80).contains(&s.len())
                && s.starts_with(|c: char| c.is_ascii_lowercase() || c.is_ascii_digit())
                && s.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == '-');
            if ok && !ids.contains(&s) {
                ids.push(s);
            }
        }
        ids.truncate(50);
        self.with(|d| {
            d.pinned = ids;
            self.save(d);
            json!({"sources": d.pinned})
        })
    }

    fn prefs_of(d: &Data) -> Value {
        let mut out = Map::new();
        match &d.prefs {
            // A saved row reads email first, and an empty value falls back to its default.
            Some(saved) => {
                out.insert("email".into(), json!(LOCAL_EMAIL));
                for (k, default) in DEFAULT_PREFS {
                    let v = saved.get(k).and_then(Value::as_str).filter(|s| !s.is_empty()).unwrap_or(default);
                    out.insert(k.into(), json!(v));
                }
            }
            None => {
                for (k, default) in DEFAULT_PREFS {
                    out.insert(k.into(), json!(default));
                }
                out.insert("email".into(), json!(LOCAL_EMAIL));
            }
        }
        Value::Object(out)
    }

    pub fn prefs(&self) -> Value {
        self.with(|d| Self::prefs_of(d))
    }

    pub fn update_prefs(&self, updates: &Value) -> Value {
        self.with(|d| {
            let mut current = Self::prefs_of(d).as_object().cloned().unwrap_or_default();
            for k in UPDATABLE {
                if let Some(v) = updates.get(k).and_then(Value::as_str) {
                    current.insert(k.into(), json!(v));
                }
            }
            current.remove("email");
            d.prefs = Some(current);
            self.save(d);
            Self::prefs_of(d)
        })
    }

    pub fn reset_prefs(&self) -> Value {
        self.with(|d| {
            d.prefs = Some(DEFAULT_PREFS.iter().map(|(k, v)| (k.to_string(), json!(v))).collect());
            self.save(d);
            Self::prefs_of(d)
        })
    }
}
