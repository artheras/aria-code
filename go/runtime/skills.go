package main

// Portable SKILL.md workflows, ported from packages/aria_skills/loader.py:
// <dir>/<skill>/SKILL.md with "name" and "description" in YAML front
// matter, then the instructions. /skills lists and shows them, /skills use
// runs a task with one, and "$name" in a prompt activates it for that turn.

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

const maxSkillChars = 64 << 10

var skillName = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,63}$`)
var skillMention = regexp.MustCompile(`\$([a-z0-9][a-z0-9-]{0,63})\b`)

type skill struct {
	name, description, instructions, path string
}

func skillRoots(root, workspace string) []string {
	roots := []string{}
	for _, item := range filepath.SplitList(os.Getenv("ARIA_SKILLS_PATH")) {
		if strings.TrimSpace(item) != "" {
			roots = append(roots, expandUser(item))
		}
	}
	roots = append(roots, filepath.Join(root, "skills"), filepath.Join(workspace, ".aria", "skills"))
	if home, err := os.UserHomeDir(); err == nil {
		for _, dir := range []string{".aria", ".claude", ".codex"} {
			roots = append(roots, filepath.Join(home, dir, "skills"))
		}
	}
	return roots
}

func frontmatterValue(lines []string, key string) string {
	for _, line := range lines {
		k, v, ok := strings.Cut(line, ":")
		if ok && strings.TrimSpace(k) == key {
			return strings.Trim(strings.TrimSpace(v), `"'`)
		}
	}
	return ""
}

func parseSkill(path string) (skill, bool) {
	body, err := os.ReadFile(path)
	text := strings.ReplaceAll(string(body), "\r\n", "\n")
	if err != nil || len(text) > maxSkillChars || !strings.HasPrefix(text, "---\n") {
		return skill{}, false
	}
	end := strings.Index(text[4:], "\n---\n")
	if end < 0 {
		return skill{}, false
	}
	front := strings.Split(text[4:4+end], "\n")
	s := skill{name: frontmatterValue(front, "name"), description: frontmatterValue(front, "description"), path: path}
	s.instructions = strings.TrimSpace(text[4+end+5:])
	if !skillName.MatchString(s.name) || s.description == "" || s.instructions == "" {
		return skill{}, false
	}
	return s, true
}

// discoverSkills finds every skill; the first root that has a name wins.
func discoverSkills(root, workspace string) []skill {
	seen := map[string]bool{}
	found := []skill{}
	for _, dir := range skillRoots(root, workspace) {
		paths, _ := filepath.Glob(filepath.Join(dir, "*", "SKILL.md"))
		sort.Strings(paths)
		for _, path := range paths {
			if s, ok := parseSkill(path); ok && !seen[s.name] {
				seen[s.name] = true
				found = append(found, s)
			}
		}
	}
	return found
}

func findSkill(root, workspace, name string) (skill, bool) {
	for _, s := range discoverSkills(root, workspace) {
		if s.name == strings.TrimPrefix(name, "$") {
			return s, true
		}
	}
	return skill{}, false
}

func skillBlock(s skill) string {
	return fmt.Sprintf("\n\n<skill name=%q path=%q>\n%s\n</skill>", s.name, s.path, s.instructions)
}

// mentionedSkills are the instructions of the skills a prompt names with
// $name, for that turn only.
func mentionedSkills(root, workspace, prompt string) string {
	matches := skillMention.FindAllStringSubmatch(strings.ToLower(prompt), -1)
	if len(matches) == 0 {
		return ""
	}
	var b strings.Builder
	used := map[string]bool{}
	for _, m := range matches {
		if used[m[1]] {
			continue
		}
		if s, ok := findSkill(root, workspace, m[1]); ok {
			used[m[1]] = true
			b.WriteString(skillBlock(s))
		}
	}
	if b.Len() == 0 {
		return ""
	}
	return "\n\nThe user activated these skills for this request; follow them:" + b.String()
}

func (s *server) skillsCommand(text string) (string, error) {
	fields := strings.Fields(text)[1:]
	if len(fields) == 0 || fields[0] == "list" {
		skills := discoverSkills(s.opts.root, s.scope.workspace)
		if len(skills) == 0 {
			return "No skills. Put one at " + filepath.Join(s.opts.root, "skills", "<name>", "SKILL.md") + " or .aria/skills/<name>/SKILL.md (front matter: name, description).", nil
		}
		var b strings.Builder
		b.WriteString("Skills")
		for _, sk := range skills {
			fmt.Fprintf(&b, "\n  %-24s %s", sk.name, capText(oneLine(sk.description), 100))
		}
		return b.String() + "\n\n$name in a prompt uses one · /skills show <name>", nil
	}
	if fields[0] == "show" && len(fields) == 2 {
		sk, ok := findSkill(s.opts.root, s.scope.workspace, fields[1])
		if !ok {
			return "", fmt.Errorf("no skill %q; /skills lists them", fields[1])
		}
		return fmt.Sprintf("%s — %s\n%s\n\n%s", sk.name, sk.description, sk.path, capText(sk.instructions, 20000)), nil
	}
	return "", errors.New("usage: /skills [list | show <name> | use <name> <task>]")
}

// skillPrompt is the /skills use turn.
func (s *server) skillPrompt(extra string) (string, error) {
	name, task, _ := strings.Cut(strings.TrimSpace(extra), " ")
	if name == "" || strings.TrimSpace(task) == "" {
		return "", errors.New("usage: /skills use <name> <task>")
	}
	sk, ok := findSkill(s.opts.root, s.scope.workspace, name)
	if !ok {
		return "", fmt.Errorf("no skill %q; /skills lists them", name)
	}
	return "Follow the skill \"" + sk.name + "\" for this task." + skillBlock(sk) + "\n\nTask: " + strings.TrimSpace(task), nil
}

// visionPrompt is /vision <image> [question]: the image as an @ reference.
func visionPrompt(extra string) (string, error) {
	path, question, _ := strings.Cut(strings.TrimSpace(extra), " ")
	if path == "" {
		return "", errors.New("usage: /vision <image path> [question] (needs /config set document_vision=true)")
	}
	if strings.TrimSpace(question) == "" {
		question = "Describe this image: what it shows, any text in it, and anything notable."
	}
	return strings.TrimSpace(question) + " @file:\"" + strings.ReplaceAll(path, `"`, "") + "\"", nil
}
