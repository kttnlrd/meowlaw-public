//! JSON in and out, matching the Python app byte for byte.
//!
//! FastAPI responses are compact (`,` and `:`), which is serde_json's default. `/api/tree`
//! goes through `json.dumps(tree, ensure_ascii=False)`, whose separators are `, ` and `: `.
//!
//! `V` is the value a cached tree is held in. A tree is tens of thousands of three-key
//! objects, so objects are plain key/value lists with the common keys shared. That parses
//! faster and takes less memory than `serde_json::Value` with ordered maps.

use serde::de::{Deserialize, Deserializer, MapAccess, SeqAccess, Visitor};
use serde::ser::{Serialize, SerializeMap, SerializeSeq, Serializer};
use serde_json::{Number, Value};
use std::fmt;
use std::io;

pub struct PyFormatter;

impl serde_json::ser::Formatter for PyFormatter {
    fn begin_array_value<W: ?Sized + io::Write>(&mut self, w: &mut W, first: bool) -> io::Result<()> {
        if first {
            Ok(())
        } else {
            w.write_all(b", ")
        }
    }
    fn begin_object_key<W: ?Sized + io::Write>(&mut self, w: &mut W, first: bool) -> io::Result<()> {
        if first {
            Ok(())
        } else {
            w.write_all(b", ")
        }
    }
    fn begin_object_value<W: ?Sized + io::Write>(&mut self, w: &mut W) -> io::Result<()> {
        w.write_all(b": ")
    }
}

/// `json.dumps(v, ensure_ascii=False)`.
pub fn dumps<T: Serialize>(v: &T) -> Vec<u8> {
    let mut out = Vec::with_capacity(4096);
    let mut ser = serde_json::Serializer::with_formatter(&mut out, PyFormatter);
    v.serialize(&mut ser).expect("serialising JSON to memory");
    out
}

/// Compact JSON, as FastAPI's JSONResponse writes it.
pub fn compact<T: Serialize>(v: &T) -> Vec<u8> {
    serde_json::to_vec(v).expect("serialising JSON to memory")
}

// ---------------------------------------------------------------------------
// V: a light JSON value that keeps key order
// ---------------------------------------------------------------------------

pub enum Key {
    Known(&'static str),
    Other(Box<str>),
}

const KNOWN_KEYS: [&str; 6] = ["id", "title", "path", "sections", "divisions", "subdivisions"];

impl Key {
    fn new(k: &str) -> Key {
        match KNOWN_KEYS.iter().find(|known| **known == k) {
            Some(known) => Key::Known(known),
            None => Key::Other(k.into()),
        }
    }
    pub fn as_str(&self) -> &str {
        match self {
            Key::Known(k) => k,
            Key::Other(k) => k,
        }
    }
}

struct KeyVisitor;

impl Visitor<'_> for KeyVisitor {
    type Value = Key;
    fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
        f.write_str("an object key")
    }
    fn visit_str<E>(self, s: &str) -> Result<Key, E> {
        Ok(Key::new(s))
    }
}

impl<'de> Deserialize<'de> for Key {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Key, D::Error> {
        d.deserialize_str(KeyVisitor)
    }
}

pub enum V {
    Null,
    Bool(bool),
    Num(Number),
    Str(Box<str>),
    Arr(Vec<V>),
    Obj(Vec<(Key, V)>),
}

impl V {
    pub fn get(&self, key: &str) -> Option<&V> {
        match self {
            V::Obj(entries) => entries.iter().find(|(k, _)| k.as_str() == key).map(|(_, v)| v),
            _ => None,
        }
    }
    pub fn get_mut(&mut self, key: &str) -> Option<&mut V> {
        match self {
            V::Obj(entries) => entries.iter_mut().find(|(k, _)| k.as_str() == key).map(|(_, v)| v),
            _ => None,
        }
    }
    pub fn as_array(&self) -> Option<&Vec<V>> {
        match self {
            V::Arr(items) => Some(items),
            _ => None,
        }
    }
    pub fn as_array_mut(&mut self) -> Option<&mut Vec<V>> {
        match self {
            V::Arr(items) => Some(items),
            _ => None,
        }
    }
    pub fn as_str(&self) -> Option<&str> {
        match self {
            V::Str(s) => Some(s),
            _ => None,
        }
    }
    /// Appends a key (the caller has checked it is absent).
    pub fn push(&mut self, key: &str, value: V) {
        if let V::Obj(entries) = self {
            entries.push((Key::new(key), value));
        }
    }
}

struct VVisitor;

impl<'de> Visitor<'de> for VVisitor {
    type Value = V;
    fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
        f.write_str("a JSON value")
    }
    fn visit_bool<E>(self, b: bool) -> Result<V, E> {
        Ok(V::Bool(b))
    }
    fn visit_i64<E>(self, n: i64) -> Result<V, E> {
        Ok(V::Num(n.into()))
    }
    fn visit_u64<E>(self, n: u64) -> Result<V, E> {
        Ok(V::Num(n.into()))
    }
    fn visit_f64<E>(self, n: f64) -> Result<V, E> {
        Ok(Number::from_f64(n).map_or(V::Null, V::Num))
    }
    fn visit_str<E>(self, s: &str) -> Result<V, E> {
        Ok(V::Str(s.into()))
    }
    fn visit_unit<E>(self) -> Result<V, E> {
        Ok(V::Null)
    }
    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<V, A::Error> {
        let mut items = Vec::with_capacity(seq.size_hint().unwrap_or(0).min(4096));
        while let Some(v) = seq.next_element()? {
            items.push(v);
        }
        Ok(V::Arr(items))
    }
    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<V, A::Error> {
        let mut entries: Vec<(Key, V)> = Vec::with_capacity(4);
        while let Some(k) = map.next_key::<Key>()? {
            let v = map.next_value()?;
            // A repeated key keeps its first place and takes the last value, as in Python.
            match entries.iter_mut().find(|(seen, _)| seen.as_str() == k.as_str()) {
                Some(slot) => slot.1 = v,
                None => entries.push((k, v)),
            }
        }
        entries.shrink_to_fit();
        Ok(V::Obj(entries))
    }
}

impl<'de> Deserialize<'de> for V {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<V, D::Error> {
        d.deserialize_any(VVisitor)
    }
}

impl Serialize for V {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        match self {
            V::Null => s.serialize_unit(),
            V::Bool(b) => s.serialize_bool(*b),
            V::Num(n) => n.serialize(s),
            V::Str(t) => s.serialize_str(t),
            V::Arr(items) => s.collect_seq(items),
            V::Obj(entries) => {
                let mut m = s.serialize_map(Some(entries.len()))?;
                for (k, v) in entries {
                    m.serialize_entry(k.as_str(), v)?;
                }
                m.end()
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Node: a response assembled from borrowed pieces of a cached tree
// ---------------------------------------------------------------------------

pub enum Node<'a> {
    Ref(&'a V),
    Json(&'a Value),
    Owned(Value),
    Obj(Vec<(&'a str, Node<'a>)>),
    Arr(Vec<Node<'a>>),
}

impl Serialize for Node<'_> {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        match self {
            Node::Ref(v) => v.serialize(s),
            Node::Json(v) => v.serialize(s),
            Node::Owned(v) => v.serialize(s),
            Node::Obj(entries) => {
                let mut m = s.serialize_map(Some(entries.len()))?;
                for (k, v) in entries {
                    m.serialize_entry(k, v)?;
                }
                m.end()
            }
            Node::Arr(items) => {
                let mut seq = s.serialize_seq(Some(items.len()))?;
                for v in items {
                    seq.serialize_element(v)?;
                }
                seq.end()
            }
        }
    }
}

/// Python's `{**base, k1: v1, k2: v2}`: an existing key keeps its place and takes the new
/// value; new keys are appended in the order given. A `base` that is not an object is
/// returned as it is.
pub fn merged<'a>(base: &'a V, over: Vec<(&'a str, Node<'a>)>) -> Node<'a> {
    let V::Obj(entries) = base else { return Node::Ref(base) };
    let mut over: Vec<Option<(&'a str, Node<'a>)>> = over.into_iter().map(Some).collect();
    let mut out = Vec::with_capacity(entries.len() + over.len());
    for (k, v) in entries {
        let hit = over.iter_mut().find(|o| matches!(o, Some((ok, _)) if *ok == k.as_str()));
        match hit {
            Some(slot) => out.push(slot.take().expect("slot checked above")),
            None => out.push((k.as_str(), Node::Ref(v))),
        }
    }
    out.extend(over.into_iter().flatten());
    Node::Obj(out)
}
