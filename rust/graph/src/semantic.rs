//! Which definitions a change touched.
//!
//! This is the native counterpart of `src/aria_code/runtime/semantic_diff.py`.
//! Python keeps the same functions and uses this answer when `aria-native`
//! can give it. A grammar this parser rejects, or a language it does not
//! know, comes back as `changes: null` so Python decides.

use crate::symbols::{self, LineIndex};
use crate::{parse_python, Parsed};
use regex::Regex;
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::sync::OnceLock;

#[derive(Debug, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
pub enum Request {
    Changes {
        before: String,
        after: String,
        language: String,
    },
    Reverse {
        after: String,
        diff: String,
    },
}

#[derive(Debug, Serialize)]
#[serde(untagged)]
pub enum Response {
    Changes { changes: Option<Vec<Change>> },
    Reverse { applied: bool, text: Option<String> },
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct Change {
    pub kind: String,
    pub name: String,
    pub symbol_kind: String,
}

pub fn answer(request: &Request) -> Response {
    match request {
        Request::Changes {
            before,
            after,
            language,
        } => Response::Changes {
            changes: symbol_changes(before, after, language),
        },
        Request::Reverse { after, diff } => match reverse_apply(after, diff) {
            Some(text) => Response::Reverse {
                applied: true,
                text: Some(text),
            },
            None => Response::Reverse {
                applied: false,
                text: None,
            },
        },
    }
}

struct Def {
    qualified: String,
    parent: String,
    kind: String,
    line: usize,
}

/// `None` when Python should parse this pair itself.
pub fn symbol_changes(before: &str, after: &str, language: &str) -> Option<Vec<Change>> {
    let old = bodies(before, language)?;
    let new = bodies(after, language)?;
    let mut changes = Vec::new();
    for (name, (kind, text)) in &new {
        if !old.contains_key(name) {
            let parent = parent_name(name);
            if !parent.is_empty() && new.contains_key(parent) && !old.contains_key(parent) {
                continue;
            }
            changes.push(Change {
                kind: "added".into(),
                name: name.clone(),
                symbol_kind: kind.clone(),
            });
        } else if old.get(name).map(|(_, old_text)| old_text) != Some(text) {
            changes.push(Change {
                kind: "modified".into(),
                name: name.clone(),
                symbol_kind: kind.clone(),
            });
        }
    }
    for (name, (kind, _)) in &old {
        if new.contains_key(name) {
            continue;
        }
        let parent = parent_name(name);
        if !parent.is_empty() && old.contains_key(parent) && !new.contains_key(parent) {
            continue;
        }
        changes.push(Change {
            kind: "removed".into(),
            name: name.clone(),
            symbol_kind: kind.clone(),
        });
    }
    changes.sort_by(|a, b| {
        kind_order(&a.kind)
            .cmp(&kind_order(&b.kind))
            .then(a.name.cmp(&b.name))
    });
    Some(changes)
}

fn kind_order(kind: &str) -> u8 {
    match kind {
        "added" => 0,
        "modified" => 1,
        _ => 2,
    }
}

fn parent_name(name: &str) -> &str {
    match name.rsplit_once('.') {
        Some((parent, _)) => parent,
        None => "",
    }
}

fn bodies(source: &str, language: &str) -> Option<HashMap<String, (String, String)>> {
    let mut defs = definitions(source, language)?;
    defs.sort_by_key(|item| item.line);
    let lines = split_lines(source);
    let mut out = HashMap::new();
    for (index, symbol) in defs.iter().enumerate() {
        let mut end = lines.len();
        for later in defs.iter().skip(index + 1) {
            if later.parent != symbol.qualified
                && !later.parent.starts_with(&format!("{}.", symbol.qualified))
            {
                end = later.line.saturating_sub(1);
                break;
            }
        }
        let children: HashSet<usize> = defs
            .iter()
            .skip(index + 1)
            .filter(|item| item.parent == symbol.qualified)
            .map(|item| item.line)
            .collect();
        let mut own = Vec::new();
        let start = symbol.line.saturating_sub(1);
        let end = end.min(lines.len());
        if start < end {
            for (offset, line) in lines[start..end].iter().enumerate() {
                let number = symbol.line + offset;
                if !inside_child(number, &children, &defs) {
                    own.push(line.as_str());
                }
            }
        }
        while own
            .last()
            .is_some_and(|line| line.trim().is_empty() || comment_only(line))
        {
            own.pop();
        }
        out.insert(
            symbol.qualified.clone(),
            (symbol.kind.clone(), own.join("\n")),
        );
    }
    Some(out)
}

fn comment_only(line: &str) -> bool {
    let trimmed = line.trim_start();
    trimmed.starts_with('#') || trimmed.starts_with("//")
}

fn inside_child(number: usize, child_lines: &HashSet<usize>, symbols: &[Def]) -> bool {
    if child_lines.is_empty() {
        return false;
    }
    let mut starts: Vec<usize> = child_lines.iter().copied().collect();
    starts.sort_unstable();
    for start in starts {
        let next = symbols
            .iter()
            .map(|item| item.line)
            .find(|line| *line > start);
        if start <= number && number < next.unwrap_or(1_000_000_000) {
            return true;
        }
    }
    false
}

fn definitions(source: &str, language: &str) -> Option<Vec<Def>> {
    if language == "python" {
        return match parse_python(source, "<semantic>") {
            Parsed::Rejected => None,
            Parsed::Invalid => Some(Vec::new()),
            Parsed::Suite(suite) => Some(
                symbols::symbols(&suite, &LineIndex::new(source))
                    .into_iter()
                    .map(|(name, kind, line, parent)| Def {
                        qualified: if parent.is_empty() {
                            name
                        } else {
                            format!("{parent}.{name}")
                        },
                        parent,
                        kind: kind.to_string(),
                        line,
                    })
                    .collect(),
            ),
        };
    }
    let patterns = declarations(language)?;
    let mut out = Vec::new();
    let mut seen = HashSet::new();
    for (lineno, line) in split_lines(source).into_iter().enumerate() {
        if line.chars().count() > 400 {
            continue;
        }
        for decl in patterns {
            if let Some(found) = decl.pattern.captures(&line) {
                let Some(name) = found.get(1) else { continue };
                let key = (name.as_str().to_string(), lineno + 1);
                if seen.insert(key) {
                    out.push(Def {
                        qualified: name.as_str().to_string(),
                        parent: String::new(),
                        kind: decl.kind.to_string(),
                        line: lineno + 1,
                    });
                }
                break;
            }
        }
    }
    Some(out)
}

#[derive(Clone)]
struct Decl {
    kind: &'static str,
    pattern: Regex,
}

fn declarations(language: &str) -> Option<&'static [Decl]> {
    static ALL: OnceLock<HashMap<&'static str, Vec<Decl>>> = OnceLock::new();
    ALL.get_or_init(declaration_table)
        .get(language)
        .map(Vec::as_slice)
}

fn compile(kind: &'static str, pattern: &str) -> Decl {
    Decl {
        kind,
        pattern: Regex::new(pattern).expect("declaration pattern"),
    }
}

fn declaration_table() -> HashMap<&'static str, Vec<Decl>> {
    let mut table = HashMap::new();
    let javascript = vec![
        compile(
            "class",
            r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)",
        ),
        compile(
            "function",
            r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)",
        ),
        compile(
            "function",
            r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>",
        ),
    ];
    let mut typescript = javascript.clone();
    typescript.extend([
        compile(
            "interface",
            r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)",
        ),
        compile("type", r"^\s*(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\s*="),
        compile(
            "enum",
            r"^\s*(?:export\s+)?(?:const\s+)?enum\s+([A-Za-z_$][\w$]*)",
        ),
    ]);
    table.insert("javascript", javascript);
    table.insert("typescript", typescript);
    table.insert(
        "go",
        vec![
            compile("func", r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_][\w]*)"),
            compile("type", r"^type\s+([A-Za-z_][\w]*)"),
        ],
    );
    table.insert(
        "rust",
        vec![
            compile(
                "fn",
                r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_][\w]*)",
            ),
            compile(
                "struct",
                r"^\s*(?:pub(?:\([^)]*\))?\s+)?struct\s+([A-Za-z_][\w]*)",
            ),
            compile(
                "enum",
                r"^\s*(?:pub(?:\([^)]*\))?\s+)?enum\s+([A-Za-z_][\w]*)",
            ),
            compile(
                "trait",
                r"^\s*(?:pub(?:\([^)]*\))?\s+)?trait\s+([A-Za-z_][\w]*)",
            ),
            compile(
                "impl",
                r"^\s*impl(?:<[^>]*>)?\s+(?:[\w:<>, ]+\s+for\s+)?([A-Za-z_][\w]*)",
            ),
        ],
    );
    table.insert("java", vec![
        compile("class", r"^\s*(?:public|private|protected|abstract|final|static|\s)*class\s+([A-Za-z_][\w]*)"),
        compile("interface", r"^\s*(?:public|private|protected|abstract|\s)*interface\s+([A-Za-z_][\w]*)"),
        compile("enum", r"^\s*(?:public|private|protected|\s)*enum\s+([A-Za-z_][\w]*)"),
    ]);
    table.insert(
        "ruby",
        vec![
            compile("class", r"^\s*class\s+([A-Za-z_][\w]*)"),
            compile("module", r"^\s*module\s+([A-Za-z_][\w]*)"),
            compile("def", r"^\s*def\s+(?:self\.)?([A-Za-z_][\w]*[?!=]?)"),
        ],
    );
    table.insert(
        "php",
        vec![
            compile(
                "class",
                r"^\s*(?:abstract\s+|final\s+)?class\s+([A-Za-z_][\w]*)",
            ),
            compile(
                "function",
                r"^\s*(?:public\s+|private\s+|protected\s+|static\s+)*function\s+([A-Za-z_][\w]*)",
            ),
        ],
    );
    table.insert("csharp", vec![
        compile("class", r"^\s*(?:public|private|protected|internal|abstract|sealed|static|partial|\s)*class\s+([A-Za-z_][\w]*)"),
        compile("interface", r"^\s*(?:public|private|protected|internal|\s)*interface\s+([A-Za-z_][\w]*)"),
        compile("record", r"^\s*(?:public|private|protected|internal|\s)*record\s+([A-Za-z_][\w]*)"),
    ]);
    table.insert("swift", vec![
        compile("class", r"^\s*(?:public\s+|private\s+|internal\s+|open\s+|final\s+)*class\s+([A-Za-z_][\w]*)"),
        compile("struct", r"^\s*(?:public\s+|private\s+|internal\s+)?struct\s+([A-Za-z_][\w]*)"),
        compile("protocol", r"^\s*(?:public\s+|private\s+|internal\s+)?protocol\s+([A-Za-z_][\w]*)"),
        compile("func", r"^\s*(?:public\s+|private\s+|internal\s+|static\s+|override\s+|@\w+\s+)*func\s+([A-Za-z_][\w]*)"),
    ]);
    table.insert("kotlin", vec![
        compile("class", r"^\s*(?:public\s+|private\s+|internal\s+|open\s+|data\s+|sealed\s+|abstract\s+)*class\s+([A-Za-z_][\w]*)"),
        compile("object", r"^\s*(?:public\s+|private\s+|internal\s+)?object\s+([A-Za-z_][\w]*)"),
        compile("fun", r"^\s*(?:public\s+|private\s+|internal\s+|suspend\s+|override\s+)*fun\s+(?:<[^>]*>\s*)?([A-Za-z_][\w]*)"),
    ]);
    let c = vec![
        compile("struct", r"^\s*(?:typedef\s+)?struct\s+([A-Za-z_][\w]*)"),
        compile(
            "function",
            r"^[A-Za-z_][\w \*]*\s+\*?([A-Za-z_][\w]*)\s*\([^;]*\)\s*\{",
        ),
    ];
    let mut cpp = c.clone();
    cpp.extend([
        compile("class", r"^\s*class\s+([A-Za-z_][\w]*)"),
        compile("namespace", r"^\s*namespace\s+([A-Za-z_][\w]*)"),
    ]);
    table.insert("c", c);
    table.insert("cpp", cpp);
    table.insert(
        "scala",
        vec![
            compile("class", r"^\s*(?:case\s+)?class\s+([A-Za-z_][\w]*)"),
            compile("object", r"^\s*(?:case\s+)?object\s+([A-Za-z_][\w]*)"),
            compile("trait", r"^\s*trait\s+([A-Za-z_][\w]*)"),
            compile("def", r"^\s*def\s+([A-Za-z_][\w]*)"),
        ],
    );
    table.insert(
        "shell",
        vec![compile(
            "function",
            r"^\s*(?:function\s+)?([A-Za-z_][\w]*)\s*\(\s*\)\s*\{",
        )],
    );
    table.insert(
        "sql",
        vec![
            compile(
                "table",
                r#"(?i)^\s*create\s+table\s+(?:if\s+not\s+exists\s+)?[`"\[]?([A-Za-z_][\w]*)"#,
            ),
            compile(
                "view",
                r#"(?i)^\s*create\s+(?:or\s+replace\s+)?view\s+[`"\[]?([A-Za-z_][\w]*)"#,
            ),
            compile(
                "function",
                r#"(?i)^\s*create\s+(?:or\s+replace\s+)?function\s+[`"\[]?([A-Za-z_][\w]*)"#,
            ),
        ],
    );
    table.insert(
        "lua",
        vec![compile(
            "function",
            r"^\s*(?:local\s+)?function\s+([A-Za-z_][\w.:]*)",
        )],
    );
    table.insert(
        "dart",
        vec![compile(
            "class",
            r"^\s*(?:abstract\s+)?class\s+([A-Za-z_][\w]*)",
        )],
    );
    table.insert(
        "elixir",
        vec![
            compile("defmodule", r"^\s*defmodule\s+([A-Za-z_][\w.]*)"),
            compile("def", r"^\s*defp?\s+([a-z_][\w]*[?!]?)"),
        ],
    );
    table
}

pub fn reverse_apply(after: &str, diff: &str) -> Option<String> {
    let mut lines = split_keep(after);
    let hunk_re = hunk_pattern();
    let mut hunks: Vec<Hunk> = Vec::new();
    let mut current: Option<usize> = None;
    for raw in split_keep(diff) {
        if hunk_re.is_match(raw.trim_end_matches(['\r', '\n'])) {
            let captures = hunk_re
                .captures(raw.trim_end_matches(['\r', '\n']))
                .unwrap();
            let new_start: usize = captures.get(3).unwrap().as_str().parse().ok()?;
            hunks.push(Hunk {
                new_start,
                old: Vec::new(),
                new: Vec::new(),
            });
            current = Some(hunks.len() - 1);
            continue;
        }
        let Some(index) = current else { continue };
        if raw.starts_with("---") || raw.starts_with("+++") || raw.starts_with('\\') {
            continue;
        }
        let (tag, body) = raw.split_at(raw.chars().next().map(|c| c.len_utf8()).unwrap_or(0));
        match tag {
            " " => {
                hunks[index].old.push(body.to_string());
                hunks[index].new.push(body.to_string());
            }
            "-" => hunks[index].old.push(body.to_string()),
            "+" => hunks[index].new.push(body.to_string()),
            _ if raw.trim().is_empty() => {
                hunks[index].old.push(raw.clone());
                hunks[index].new.push(raw);
            }
            _ => {}
        }
    }
    if hunks.is_empty() {
        return None;
    }
    for hunk in hunks.iter().rev() {
        let start = if hunk.new.is_empty() {
            hunk.new_start
        } else {
            hunk.new_start.saturating_sub(1)
        };
        let end = start + hunk.new.len();
        if end > lines.len() {
            return None;
        }
        let got: Vec<String> = lines[start..end]
            .iter()
            .map(|line| trim_newline(line))
            .collect();
        let want: Vec<String> = hunk.new.iter().map(|line| trim_newline(line)).collect();
        if got != want {
            return None;
        }
        let old = hunk.old.clone();
        lines.splice(start..end, old);
    }
    Some(lines.concat())
}

struct Hunk {
    new_start: usize,
    old: Vec<String>,
    new: Vec<String>,
}

fn hunk_pattern() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| {
        Regex::new(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@").expect("hunk pattern")
    })
}

fn trim_newline(line: &str) -> String {
    line.trim_end_matches(['\r', '\n']).to_string()
}

fn split_lines(source: &str) -> Vec<String> {
    split_keep(source)
        .into_iter()
        .map(|line| trim_newline(&line))
        .collect()
}

fn split_keep(source: &str) -> Vec<String> {
    let mut lines = Vec::new();
    let mut rest = source;
    while !rest.is_empty() {
        if let Some(index) = rest.find(['\n', '\r']) {
            let end = if rest[index..].starts_with("\r\n") {
                index + 2
            } else {
                index + 1
            };
            lines.push(rest[..end].to_string());
            rest = &rest[end..];
        } else {
            lines.push(rest.to_string());
            break;
        }
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::*;

    fn labels(before: &str, after: &str, language: &str) -> Vec<String> {
        symbol_changes(before, after, language)
            .unwrap()
            .into_iter()
            .map(|change| {
                let callable = matches!(
                    change.symbol_kind.as_str(),
                    "def" | "async def" | "function" | "method" | "func" | "fn"
                );
                format!(
                    "{}{} {}",
                    change.name,
                    if callable { "()" } else { "" },
                    change.kind
                )
            })
            .collect()
    }

    const V0: &str = "import time\n\n\nclass Session:\n    def refresh(self):\n        return fetch(self.token)\n\n    def close(self):\n        pass\n\n\ndef legacy_refresh(s):\n    return s.refresh()\n\n\ndef helper():\n    return 1\n";

    #[test]
    fn added_modified_and_removed_definitions() {
        let v1 = V0.replace(
            "        return fetch(self.token)",
            "        with self._lock:\n            return fetch(self.token)",
        );
        let v2 = v1.replace("def legacy_refresh(s):\n    return s.refresh()\n\n\n", "")
            + "\n\nclass SessionManager:\n    def get(self):\n        return None\n";
        assert_eq!(
            labels(V0, &v2, "python"),
            vec![
                "SessionManager added",
                "Session.refresh() modified",
                "legacy_refresh() removed",
            ]
        );
    }

    #[test]
    fn an_added_class_stands_for_its_methods() {
        assert_eq!(
            labels("", "class A:\n    def f(self):\n        pass\n", "python"),
            vec!["A added"]
        );
    }

    #[test]
    fn a_changed_method_does_not_mark_the_class() {
        let before = "class A:\n    x = 1\n\n    def f(self):\n        return 1\n";
        let after = before.replace("return 1", "return 2");
        assert_eq!(labels(before, &after, "python"), vec!["A.f() modified"]);
    }

    #[test]
    fn a_comment_between_definitions_changes_nothing() {
        let before = "def a():\n    return 1\n\n# about b\n";
        let after = "def a():\n    return 1\n\n# about b\n\ndef b():\n    return 2\n";
        assert_eq!(labels(before, after, "python"), vec!["b() added"]);
    }

    #[test]
    fn typescript_declarations_follow_the_python_patterns() {
        let before = "export function load(a) {\n  return a\n}\n";
        let after = before.replace("return a", "return a + 1")
            + "export function save(b) {\n  return b\n}\n";
        assert_eq!(
            labels(before, &after, "typescript"),
            vec!["save() added", "load() modified"]
        );
    }

    #[test]
    fn an_unknown_language_stays_with_python() {
        assert!(symbol_changes("a", "b", "cobol").is_none());
    }

    #[test]
    fn reverse_apply_restores_the_previous_text() {
        let after = "a\nB\n";
        let diff = "@@ -1,2 +1,2 @@\n a\n-b\n+B\n";
        assert_eq!(reverse_apply(after, diff).as_deref(), Some("a\nb\n"));
        assert_eq!(reverse_apply("something else\n", diff), None);
    }
}
