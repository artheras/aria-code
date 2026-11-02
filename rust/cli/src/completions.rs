//! `aria-native completions bash|zsh|fish`: a completion script for the
//! command line (the Python worker's /completions, now the binary's own).
//! It registers for `aria-native` and the `aria` wrapper.

const SUBCOMMANDS: &[&str] = &[
    "chat",
    "run",
    "exec",
    "render",
    "tool",
    "config",
    "sessions",
    "resume",
    "update",
    "physical",
    "index",
    "completions",
];
const OPTIONS: &[&str] = &[
    "--python",
    "-C",
    "--workspace",
    "--timeout-ms",
    "--help",
    "--version",
];

fn nested(command: &str) -> &'static [&'static str] {
    match command {
        "config" => &["paths", "show"],
        "sessions" => &["list", "search", "show"],
        "update" => &["check"],
        "index" => &["imports", "symbols"],
        "physical" => &["jsonl", "agent-jsonl", "mcp", "serve"],
        "completions" => &["bash", "zsh", "fish"],
        "chat" | "exec" | "render" => &["--jsonl"],
        _ => &[],
    }
}

const NESTED: &[&str] = &[
    "config",
    "sessions",
    "update",
    "index",
    "physical",
    "completions",
    "chat",
    "exec",
    "render",
];

pub fn script(shell: &str) -> Result<String, String> {
    let top = [SUBCOMMANDS, OPTIONS].concat().join(" ");
    match shell {
        "bash" => {
            let mut cases = String::new();
            for command in NESTED {
                cases += &format!(
                    "    {command}) words=\"{}\" ;;\n",
                    nested(command).join(" ")
                );
            }
            Ok(format!(
                "_aria_native() {{\n  local cur=\"${{COMP_WORDS[COMP_CWORD]}}\" prev=\"\" words=\"{top}\" i\n  for ((i=1; i<COMP_CWORD; i++)); do\n    case \"${{COMP_WORDS[i]}}\" in -*) ;; *) prev=\"${{COMP_WORDS[i]}}\"; break ;; esac\n  done\n  case \"$prev\" in\n{cases}    \"\") ;;\n    *) words=\"\" ;;\n  esac\n  COMPREPLY=($(compgen -W \"$words\" -- \"$cur\"))\n}}\ncomplete -F _aria_native aria-native aria\n"
            ))
        }
        "zsh" => {
            let mut cases = String::new();
            for command in NESTED {
                cases += &format!(
                    "    {command}) compadd -- {} ;;\n",
                    nested(command).join(" ")
                );
            }
            Ok(format!(
                "#compdef aria-native aria\n_aria_native() {{\n  if (( CURRENT == 2 )); then\n    compadd -- {top}\n    return\n  fi\n  case \"${{words[2]}}\" in\n{cases}  esac\n}}\ncompdef _aria_native aria-native aria\n"
            ))
        }
        "fish" => {
            let mut out = String::new();
            for name in ["aria-native", "aria"] {
                out += &format!(
                    "complete -c {name} -f -n '__fish_use_subcommand' -a '{}'\n",
                    SUBCOMMANDS.join(" ")
                );
                for command in NESTED {
                    out += &format!(
                        "complete -c {name} -f -n '__fish_seen_subcommand_from {command}' -a '{}'\n",
                        nested(command).join(" ")
                    );
                }
            }
            Ok(out)
        }
        _ => Err("Usage: completions bash|zsh|fish".into()),
    }
}

#[cfg(test)]
mod tests {
    use super::script;

    #[test]
    fn every_shell_gets_the_subcommands() {
        for shell in ["bash", "zsh", "fish"] {
            let text = script(shell).unwrap();
            for word in ["chat", "sessions", "completions", "aria-native"] {
                assert!(text.contains(word), "{shell} lacks {word}");
            }
        }
        assert!(script("bash")
            .unwrap()
            .contains("complete -F _aria_native aria-native aria"));
        assert!(script("tcsh").is_err());
    }

    #[test]
    fn bash_script_is_valid_and_completes() {
        if std::process::Command::new("bash")
            .arg("--version")
            .output()
            .is_err()
        {
            return;
        }
        let body = script("bash").unwrap()
            + "COMP_WORDS=(aria-native sessions ''); COMP_CWORD=2; _aria_native; echo \"${COMPREPLY[*]}\"\n";
        let out = std::process::Command::new("bash")
            .arg("-c")
            .arg(body)
            .output()
            .unwrap();
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
        assert_eq!(
            String::from_utf8_lossy(&out.stdout).trim(),
            "list search show"
        );
    }
}
