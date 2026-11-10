package main

import "testing"

func TestTextToolCallsIgnoreQuotedExamples(t *testing.T) {
	if !looksLikeTextToolCalls("I will call it.\n<tool_call>\n{\"name\":\"read_file\"}\n</tool_call>") {
		t.Fatal("a written tool call should not count as a finished answer")
	}
	if !looksLikeTextToolCalls("default_api.read_file") {
		t.Fatal("default_api call syntax should be recovered")
	}
	for _, snippet := range []string{
		"Let me inspect.\n<function=read_file>{\"path\":\"main.go\"}</function>",
		"[TOOL_CALLS] [{\"name\":\"read_file\"}]",
		"<|python_tag|>read_file(path=\"main.go\")",
	} {
		if !looksLikeTextToolCalls(snippet) {
			t.Fatalf("expected text tool call detection for %q", snippet)
		}
	}
	if looksLikeTextToolCalls("The repo contains ```\n<tool_call>\n``` in a fixture.") {
		t.Fatal("a tool call inside a code span is an example, not a call")
	}
}

func TestPromptEchoIsOnlyAShortInstruction(t *testing.T) {
	if !looksLikePromptEcho(" Do not stop until all necessary actions have been performed.") {
		t.Fatal("a prompt continuation should be sent back")
	}
	if looksLikePromptEcho("Changed session.go.\n\nMake sure to run the tests before you commit.") {
		t.Fatal("a real answer that mentions a follow-up is not an echo")
	}
}
