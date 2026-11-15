package main

import (
	"encoding/json"
	"fmt"
	"strings"
)

const defaultReadOnlySoftThreshold = 10

var readOnlyExplorationTools = map[string]bool{
	"read_file":   true,
	"search_code": true,
	"grep":        true,
	"glob":        true,
	"list_files":  true,
	"repo_map":    true,
	"find_symbol": true,
}

var writeMutationTools = map[string]bool{
	"write_file": true,
	"edit_file":  true,
	"multi_edit": true,
}

type loopGuard struct {
	readOnlySoftThreshold int
	failedCallCounts      map[string]int
	fileHashes            map[string][]string
	consecutiveReads      int
	readBudgetWarned      bool
	shouldBreak           bool
}

func newLoopGuard(readOnlySoftThreshold int) *loopGuard {
	if readOnlySoftThreshold <= 0 {
		readOnlySoftThreshold = defaultReadOnlySoftThreshold
	}
	return &loopGuard{
		readOnlySoftThreshold: readOnlySoftThreshold,
		failedCallCounts:      map[string]int{},
		fileHashes:            map[string][]string{},
	}
}

func (g *loopGuard) record(toolName string, args map[string]any, result toolResult, mutate bool) (failureWarning, advisory string) {
	isFail := !result.Success || (result.Data != nil && result.Data["exit_code"] != nil && result.Data["exit_code"] != 0)

	if readOnlyExplorationTools[toolName] && !isFail {
		g.consecutiveReads++
		if g.consecutiveReads >= g.readOnlySoftThreshold && !g.readBudgetWarned {
			g.readBudgetWarned = true
			advisory = fmt.Sprintf(
				" [Advisory: %d consecutive read-only exploration calls without edits or commands. Act on the collected context now or report your conclusion.]",
				g.consecutiveReads,
			)
		}
	} else if !readOnlyExplorationTools[toolName] {
		g.consecutiveReads = 0
		g.readBudgetWarned = false
	}

	if writeMutationTools[toolName] && !isFail {
		fpath := stringField(result.Data, "path")
		if fpath == "" {
			fpath = stringField(args, "path")
		}
		afterHash := stringField(result.Data, "after_hash")
		if fpath != "" && afterHash != "" {
			seen := g.fileHashes[fpath]
			for i := 0; i < len(seen)-1; i++ {
				if seen[i] == afterHash {
					g.shouldBreak = true
					advisory = fmt.Sprintf(
						" [Warning: edits to %s are oscillating back to a previous state (A->B->A). Stop repeating edits and reassess the root cause.]",
						fpath,
					)
					break
				}
			}
			if len(seen) == 0 || seen[len(seen)-1] != afterHash {
				g.fileHashes[fpath] = append(seen, afterHash)
			}
		}
	}

	callKeyBytes, _ := json.Marshal(args)
	callKey := toolName + ":" + string(callKeyBytes)
	if isFail {
		g.failedCallCounts[callKey]++
		if count := g.failedCallCounts[callKey]; count >= 2 {
			failureWarning = fmt.Sprintf(
				" [Warning: this exact call failed %d times consecutively. Do not repeat identical parameters; change arguments or try another approach.]",
				count,
			)
		}
	} else {
		delete(g.failedCallCounts, callKey)
		if mutate && result.Success {
			g.failedCallCounts = map[string]int{}
		}
	}
	return failureWarning, advisory
}

func stringField(m map[string]any, key string) string {
	if m == nil {
		return ""
	}
	if v, ok := m[key].(string); ok {
		return strings.TrimSpace(v)
	}
	return ""
}
