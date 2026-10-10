//! Python definitions for the repository map: the native counterpart of
//! `_python_symbols` in `src/aria_code/runtime/repo_map.py`.
//!
//! Top-level functions and classes, the functions directly in a class body
//! (with the class as parent), and module-level constants: an `Assign` or
//! `AnnAssign` to a plain name that is upper case and longer than two
//! characters. Line numbers are 1-based, from the `def`/`class` keyword (a
//! decorator does not move them), exactly as `ast` reports them.

use crate::{parse_python, read, Parsed};
use rayon::prelude::*;
use rustpython_parser::ast::{self, Ranged};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::Path;

#[derive(Debug, Deserialize)]
pub struct Request {
    pub root: String,
    /// Python files to extract definitions from.
    pub parse: Vec<String>,
}

/// `[name, kind, line, parent]`, the shape `repo_map` stores.
pub type Symbol = (String, &'static str, usize, String);

#[derive(Debug, Default, Serialize, PartialEq)]
pub struct Response {
    pub symbols: BTreeMap<String, Vec<Symbol>>,
    pub fallback: Vec<String>,
}

pub fn extract(request: &Request) -> Response {
    let root = Path::new(&request.root);
    let results: Vec<(String, Option<Vec<Symbol>>)> = request
        .parse
        .par_iter()
        .map(|path| (path.clone(), file_symbols(root, path)))
        .collect();
    let mut response = Response::default();
    for (path, symbols) in results {
        match symbols {
            Some(found) => {
                response.symbols.insert(path, found);
            }
            None => response.fallback.push(path),
        }
    }
    response.fallback.sort();
    response
}

/// `None` when the parser rejects the file and Python should decide.
fn file_symbols(root: &Path, path: &str) -> Option<Vec<Symbol>> {
    // repo_map skips a file it cannot read; let Python see that for itself.
    let source = read(root, path)?;
    match parse_python(&source, path) {
        Parsed::Rejected => None,
        Parsed::Invalid => Some(Vec::new()),
        Parsed::Suite(suite) => Some(symbols(&suite, &LineIndex::new(&source))),
    }
}

pub fn symbols(suite: &[ast::Stmt], lines: &LineIndex) -> Vec<Symbol> {
    let mut out = Vec::new();
    for stmt in suite {
        match stmt {
            ast::Stmt::FunctionDef(s) => {
                out.push((s.name.to_string(), "def", lines.line(stmt), String::new()))
            }
            ast::Stmt::AsyncFunctionDef(s) => out.push((
                s.name.to_string(),
                "async def",
                lines.line(stmt),
                String::new(),
            )),
            ast::Stmt::ClassDef(class) => {
                out.push((
                    class.name.to_string(),
                    "class",
                    lines.line(stmt),
                    String::new(),
                ));
                for child in &class.body {
                    let kind = match child {
                        ast::Stmt::FunctionDef(s) => Some((s.name.as_str(), "def")),
                        ast::Stmt::AsyncFunctionDef(s) => Some((s.name.as_str(), "async def")),
                        _ => None,
                    };
                    if let Some((name, kind)) = kind {
                        out.push((
                            name.to_string(),
                            kind,
                            lines.line(child),
                            class.name.to_string(),
                        ));
                    }
                }
            }
            ast::Stmt::Assign(s) => {
                for target in &s.targets {
                    push_constant(&mut out, target, lines.line(stmt));
                }
            }
            ast::Stmt::AnnAssign(s) => push_constant(&mut out, &s.target, lines.line(stmt)),
            _ => {}
        }
    }
    out
}

fn push_constant(out: &mut Vec<Symbol>, target: &ast::Expr, line: usize) {
    if let ast::Expr::Name(name) = target {
        let id = name.id.as_str();
        if is_upper(id) && id.chars().count() > 2 {
            out.push((id.to_string(), "const", line, String::new()));
        }
    }
}

/// Python's `str.isupper()`: at least one cased character, none lower case.
pub fn is_upper(text: &str) -> bool {
    let mut cased = false;
    for c in text.chars() {
        if c.is_lowercase() {
            return false;
        }
        if c.is_uppercase() {
            cased = true;
        }
    }
    cased
}

/// Byte offset to 1-based line number.
pub struct LineIndex {
    starts: Vec<usize>,
}

impl LineIndex {
    pub fn new(source: &str) -> Self {
        let mut starts = vec![0];
        starts.extend(source.match_indices('\n').map(|(i, _)| i + 1));
        LineIndex { starts }
    }

    pub fn line(&self, node: &impl Ranged) -> usize {
        let offset = usize::from(node.range().start());
        self.starts.partition_point(|&start| start <= offset)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rustpython_parser::Parse;

    fn of(source: &str) -> Vec<Symbol> {
        let suite = ast::Suite::parse(source, "<test>").unwrap();
        symbols(&suite, &LineIndex::new(source))
    }

    #[test]
    fn definitions_methods_and_constants() {
        let found = of("import os\nLIMIT = 3\nab = 1\nXY = 2\nA = B = 4\n\n@dec(\n  1)\nasync def run():\n    pass\n\nclass Gate:\n    TOKEN = 1\n    def check(self): ...\n    async def wait(self): ...\n    if True:\n        def hidden(self): ...\nNAME: str = 'x'\n");
        let expected: Vec<Symbol> = vec![
            ("LIMIT".into(), "const", 2, String::new()),
            ("run".into(), "async def", 9, String::new()),
            ("Gate".into(), "class", 12, String::new()),
            ("check".into(), "def", 14, "Gate".into()),
            ("wait".into(), "async def", 15, "Gate".into()),
            ("NAME".into(), "const", 18, String::new()),
        ];
        // `ab` is lower case, `XY`, `A` and `B` too short, `TOKEN` and
        // `hidden` not directly a function in the class body.
        assert_eq!(found, expected);
    }

    #[test]
    fn isupper_follows_python() {
        assert!(is_upper("MAX_1"));
        assert!(is_upper("_X"));
        assert!(!is_upper("Max"));
        assert!(!is_upper("__"));
        assert!(!is_upper("123"));
    }
}
