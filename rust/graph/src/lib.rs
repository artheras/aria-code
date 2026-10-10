//! Import resolution for Aria Code's project graph.
//!
//! This is the native counterpart of `_imports` in
//! `src/aria_code/runtime/project_graph.py`, and must give the same answer:
//! for every file asked about, the repository files its imports resolve to.
//! Python imports are found with a real parser (anywhere in the file, like
//! `ast.walk`); JS/TS relative specifiers with the same pattern the Python
//! side uses. A Python file this parser rejects is reported in `fallback`
//! for the caller to parse itself, so a grammar gap here never becomes a
//! missing edge.

pub mod symbols;

use rayon::prelude::*;
use regex::Regex;
use rustpython_parser::{ast, Parse};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::Path;
use std::sync::OnceLock;

const JS_EXTENSIONS: [&str; 8] = [
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte",
];

#[derive(Debug, Deserialize)]
pub struct Request {
    pub root: String,
    /// Every file in the repository as `[path, language]`: the module index
    /// and JS resolution need all of them, not only the ones being parsed.
    pub files: Vec<(String, String)>,
    /// The files whose imports to compute.
    pub parse: Vec<String>,
}

#[derive(Debug, Default, Serialize, PartialEq)]
pub struct Response {
    pub imports: BTreeMap<String, Vec<String>>,
    pub fallback: Vec<String>,
}

enum Outcome {
    Found(Vec<String>),
    Fallback,
}

pub fn resolve(request: &Request) -> Response {
    let root = Path::new(&request.root);
    let modules = module_index(&request.files);
    let known: HashSet<&str> = request.files.iter().map(|(p, _)| p.as_str()).collect();
    let languages: HashMap<&str, &str> = request
        .files
        .iter()
        .map(|(p, l)| (p.as_str(), l.as_str()))
        .collect();

    let results: Vec<(String, Outcome)> = request
        .parse
        .par_iter()
        .map(|path| {
            let language = languages.get(path.as_str()).copied().unwrap_or("");
            (
                path.clone(),
                imports(root, path, language, &modules, &known),
            )
        })
        .collect();

    let mut response = Response::default();
    for (path, outcome) in results {
        match outcome {
            Outcome::Found(found) => {
                response.imports.insert(path, found);
            }
            Outcome::Fallback => response.fallback.push(path),
        }
    }
    response.fallback.sort();
    response
}

fn imports(
    root: &Path,
    path: &str,
    language: &str,
    modules: &HashMap<String, Vec<String>>,
    known: &HashSet<&str>,
) -> Outcome {
    if language == "python" {
        return python_imports(root, path, modules);
    }
    if JS_EXTENSIONS.iter().any(|ext| path.ends_with(ext)) {
        return Outcome::Found(js_imports(root, path, known));
    }
    Outcome::Found(Vec::new())
}

pub(crate) fn read(root: &Path, path: &str) -> Option<String> {
    let bytes = std::fs::read(root.join(path)).ok()?;
    // Python reads with errors="replace" and universal newlines.
    Some(
        String::from_utf8_lossy(&bytes)
            .replace("\r\n", "\n")
            .replace('\r', "\n"),
    )
}

pub(crate) enum Parsed {
    Suite(Vec<ast::Stmt>),
    /// Text `ast.parse` refuses outright: Python finds nothing in it.
    Invalid,
    /// This parser refused it; Python must decide.
    Rejected,
}

pub(crate) fn parse_python(source: &str, path: &str) -> Parsed {
    // CPython rejects a byte-order mark or NUL inside decoded text.
    if source.starts_with('\u{feff}') || source.contains('\0') {
        return Parsed::Invalid;
    }
    match ast::Suite::parse(source, path) {
        Ok(suite) => Parsed::Suite(suite),
        Err(_) => Parsed::Rejected,
    }
}

// ── paths ──────────────────────────────────────────────────────────────────

fn sep() -> char {
    std::path::MAIN_SEPARATOR
}

/// `Path(path).parts` for a relative path: no empty or `.` components.
fn parts(path: &str) -> Vec<&str> {
    path.split(['/', '\\'])
        .filter(|p| !p.is_empty() && *p != ".")
        .collect()
}

/// `os.path.normpath` for a relative path.
fn normpath(path: &str) -> String {
    let mut out: Vec<&str> = Vec::new();
    for part in path.split(['/', '\\']) {
        match part {
            "" | "." => {}
            ".." => {
                if out.last().is_some_and(|last| *last != "..") {
                    out.pop();
                } else {
                    out.push("..");
                }
            }
            other => out.push(other),
        }
    }
    if out.is_empty() {
        ".".into()
    } else {
        out.join(&sep().to_string())
    }
}

// ── Python ─────────────────────────────────────────────────────────────────

/// Dotted module names, every suffix of the path, mapped to Python files
/// (`_module_index`). Candidate order follows the request's file order.
pub fn module_index(files: &[(String, String)]) -> HashMap<String, Vec<String>> {
    let mut index: HashMap<String, Vec<String>> = HashMap::new();
    for (path, language) in files {
        if language != "python" {
            continue;
        }
        let mut parts: Vec<String> = parts(path).into_iter().map(String::from).collect();
        if let Some(last) = parts.last_mut() {
            // Path.with_suffix(""): drop the last extension of the final name.
            if let Some(dot) = last.rfind('.') {
                if dot > 0 {
                    last.truncate(dot);
                }
            }
        }
        if parts.last().is_some_and(|last| last == "__init__") {
            parts.pop();
        }
        for start in 0..parts.len() {
            index
                .entry(parts[start..].join("."))
                .or_default()
                .push(path.clone());
        }
    }
    index
}

/// `_pick`: the only candidate, or the one sharing strictly the most leading
/// directories with the importer.
fn pick(candidates: &[String], importer: &str) -> Option<String> {
    match candidates {
        [] => None,
        [only] => Some(only.clone()),
        _ => {
            let importer_parts = parts(importer);
            let shared = |path: &str| {
                parts(path)
                    .iter()
                    .zip(&importer_parts)
                    .take_while(|(a, b)| a == b)
                    .count()
            };
            let mut best: Option<(usize, &String)> = None;
            let mut second = 0usize;
            for candidate in candidates {
                let score = shared(candidate);
                match best {
                    Some((top, _)) if score <= top => second = second.max(score),
                    Some((top, _)) => {
                        second = top;
                        best = Some((score, candidate));
                    }
                    None => best = Some((score, candidate)),
                }
            }
            let (top, winner) = best?;
            (top > second).then(|| winner.clone())
        }
    }
}

/// Python's `seq[:end]` with a possibly negative `end`.
fn slice_to<T: Clone>(seq: &[T], end: i64) -> Vec<T> {
    let len = seq.len() as i64;
    let stop = if end < 0 {
        (len + end).max(0)
    } else {
        end.min(len)
    };
    seq[..stop as usize].to_vec()
}

fn python_imports(root: &Path, path: &str, modules: &HashMap<String, Vec<String>>) -> Outcome {
    let Some(source) = read(root, path) else {
        return Outcome::Found(Vec::new());
    };
    let suite = match parse_python(&source, path) {
        Parsed::Suite(suite) => suite,
        Parsed::Invalid => return Outcome::Found(Vec::new()),
        Parsed::Rejected => return Outcome::Fallback,
    };
    let mut package: Vec<String> = parts(path).into_iter().map(String::from).collect();
    package.pop();
    let resolve = |name: &str| pick(modules.get(name).map(Vec::as_slice).unwrap_or(&[]), path);

    let mut found = Vec::new();
    let mut visit = |stmt: &ast::Stmt| match stmt {
        ast::Stmt::Import(node) => {
            for alias in &node.names {
                if let Some(hit) = resolve(alias.name.as_str()) {
                    found.push(hit);
                }
            }
        }
        ast::Stmt::ImportFrom(node) => {
            let level = node.level.map(|l| l.to_u32()).unwrap_or(0) as i64;
            let module = node.module.as_ref().map(|m| m.as_str().to_string());
            let base = if level > 0 {
                let base_parts = if level > 1 {
                    slice_to(&package, package.len() as i64 - (level - 1))
                } else {
                    package.clone()
                };
                let mut all = base_parts;
                all.extend(module);
                all.join(".")
            } else {
                module.unwrap_or_default()
            };
            for alias in &node.names {
                let name = alias.name.as_str();
                let full = if base.is_empty() {
                    name.to_string()
                } else {
                    format!("{base}.{name}")
                };
                let hit = resolve(&full).or_else(|| {
                    if base.is_empty() {
                        None
                    } else {
                        resolve(&base)
                    }
                });
                if let Some(hit) = hit {
                    found.push(hit);
                }
            }
        }
        _ => {}
    };
    walk(&suite, &mut visit);
    Outcome::Found(found)
}

/// Every statement at any depth: imports can sit in functions, classes,
/// conditionals and handlers, and `ast.walk` finds them all.
fn walk(body: &[ast::Stmt], visit: &mut impl FnMut(&ast::Stmt)) {
    use ast::Stmt::*;
    for stmt in body {
        visit(stmt);
        match stmt {
            FunctionDef(s) => walk(&s.body, visit),
            AsyncFunctionDef(s) => walk(&s.body, visit),
            ClassDef(s) => walk(&s.body, visit),
            For(s) => {
                walk(&s.body, visit);
                walk(&s.orelse, visit);
            }
            AsyncFor(s) => {
                walk(&s.body, visit);
                walk(&s.orelse, visit);
            }
            While(s) => {
                walk(&s.body, visit);
                walk(&s.orelse, visit);
            }
            If(s) => {
                walk(&s.body, visit);
                walk(&s.orelse, visit);
            }
            With(s) => walk(&s.body, visit),
            AsyncWith(s) => walk(&s.body, visit),
            Match(s) => s.cases.iter().for_each(|case| walk(&case.body, visit)),
            Try(s) => {
                walk(&s.body, visit);
                for handler in &s.handlers {
                    let ast::ExceptHandler::ExceptHandler(h) = handler;
                    walk(&h.body, visit);
                }
                walk(&s.orelse, visit);
                walk(&s.finalbody, visit);
            }
            TryStar(s) => {
                walk(&s.body, visit);
                for handler in &s.handlers {
                    let ast::ExceptHandler::ExceptHandler(h) = handler;
                    walk(&h.body, visit);
                }
                walk(&s.orelse, visit);
                walk(&s.finalbody, visit);
            }
            _ => {}
        }
    }
}

// ── JS / TS ────────────────────────────────────────────────────────────────

fn js_pattern() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| {
        Regex::new(concat!(
            r#"(?:\bimport\s[^'"]*?\bfrom\s*|\bimport\s*\(\s*|\brequire\s*\(\s*|\bexport\s[^'"]*?\bfrom\s*|\bimport\s+)"#,
            r#"['"](\.{1,2}/[^'"]+)['"]"#
        ))
        .expect("valid pattern")
    })
}

fn js_imports(root: &Path, path: &str, known: &HashSet<&str>) -> Vec<String> {
    let Some(source) = read(root, path) else {
        return Vec::new();
    };
    let mut folder = parts(path);
    folder.pop();
    let folder = folder.join("/");
    let mut found = Vec::new();
    for caps in js_pattern().captures_iter(&source) {
        let spec = &caps[1];
        let base = normpath(&if folder.is_empty() {
            spec.to_string()
        } else {
            format!("{folder}/{spec}")
        });
        let s = sep();
        let candidates = std::iter::once(base.clone())
            .chain(JS_EXTENSIONS.iter().map(|ext| format!("{base}{ext}")))
            .chain(
                JS_EXTENSIONS
                    .iter()
                    .map(|ext| format!("{base}{s}index{ext}")),
            );
        if let Some(hit) = candidates.into_iter().find(|c| known.contains(c.as_str())) {
            found.push(hit);
        }
    }
    found
}

#[cfg(test)]
mod tests {
    use super::*;

    fn files(list: &[(&str, &str)]) -> Vec<(String, String)> {
        list.iter()
            .map(|(p, l)| (p.to_string(), l.to_string()))
            .collect()
    }

    #[test]
    fn module_index_covers_every_suffix() {
        let index = module_index(&files(&[
            ("src/pkg/mod.py", "python"),
            ("src/pkg/__init__.py", "python"),
        ]));
        assert_eq!(index["mod"], vec!["src/pkg/mod.py"]);
        assert_eq!(index["pkg.mod"], vec!["src/pkg/mod.py"]);
        assert_eq!(index["src.pkg.mod"], vec!["src/pkg/mod.py"]);
        assert_eq!(index["pkg"], vec!["src/pkg/__init__.py"]);
    }

    #[test]
    fn pick_prefers_the_nearest_unique_candidate() {
        let c = vec!["a/x/util.py".to_string(), "b/util.py".to_string()];
        assert_eq!(pick(&c, "a/x/main.py"), Some("a/x/util.py".into()));
        assert_eq!(pick(&c, "c/main.py"), None);
    }

    #[test]
    fn normpath_and_negative_slices_follow_python() {
        assert_eq!(
            normpath("a/b/../c/./d.js"),
            ["a", "c", "d.js"].join(&sep().to_string())
        );
        assert_eq!(
            normpath("../x"),
            "..".to_string() + &sep().to_string() + "x"
        );
        assert_eq!(slice_to(&[1, 2, 3], -1), vec![1, 2]);
        assert_eq!(slice_to(&[1], -5), Vec::<i32>::new());
    }

    #[test]
    fn js_pattern_matches_the_python_one() {
        let source = "import a from './a'\nconst b = require(\"../b\")\nexport { c } from './c/index'\nimport('./d')\nimport x from 'react'";
        let specs: Vec<_> = js_pattern()
            .captures_iter(source)
            .map(|c| c[1].to_string())
            .collect();
        assert_eq!(specs, vec!["./a", "../b", "./c/index", "./d"]);
    }
}
