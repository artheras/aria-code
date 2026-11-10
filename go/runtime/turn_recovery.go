package main

import (
	"regexp"
	"strings"
)

// Local and small models often write a tool call, or continue the prompt, as
// their final answer. The Python loop asks once and then ends the turn
// incomplete (runtime/agent_loop.py). The native loop does the same so a
// plan that was never executed is not delivered as done.

const (
	maxTextToolCallRetries = 2
	maxPromptEchoRetries   = 1
	promptEchoMaxChars     = 240
	textToolCallDirective  = "Your last reply wrote tool calls (or tool results) as text. None of them were executed and no files were changed. Do not write calls or results out as text: call the tools through function calling, and only report results you actually received."
	promptEchoDirective    = "Your last reply was an instruction, not an answer: it reads like a continuation of the prompt, and the task is not finished. Carry on with the user's request — call the tools you need. When the work is actually done, reply with what you changed and how you checked it."
)

var (
	codeSpan     = regexp.MustCompile("(?s)```.*?(?:```|\\z)|`[^`\n]*`")
	textToolCall = regexp.MustCompile("(?m)<tool_call>|<function=\\w+>|\\[TOOL_CALLS\\]|<\\|python_tag\\|>|<｜tool▁calls▁begin｜>|\\bdefault_api[:.]\\w|^#{2,3} ?Tool Results\\b|^### \\[\\w+\\] (?:✓ Success|❌ Error)")
	promptEcho   = regexp.MustCompile(`(?i)^(?:do not stop|don't stop|if you are (?:finished|done)|if the task is|continue (?:using|with|the task)|use (?:the )?tools|you (?:must|should) (?:continue|now)|make sure (?:to|you)|remember to)\b|\b(?:necessary actions|your final (?:response|answer)|describe your findings|continue using tools|\d+-\d+ sentences)\b`)
)

func withoutCodeSpans(text string) string { return codeSpan.ReplaceAllString(text, "") }

func looksLikeTextToolCalls(text string) bool {
	return text != "" && textToolCall.MatchString(withoutCodeSpans(text))
}

func looksLikePromptEcho(text string) bool {
	stripped := strings.TrimSpace(text)
	if stripped == "" || len(stripped) > promptEchoMaxChars || strings.Contains(stripped, "\n\n") {
		return false
	}
	return promptEcho.MatchString(withoutCodeSpans(stripped))
}
