package main

import (
	"context"
	"testing"
)

func TestSolveSpatialConstraintsCollisionAndReachability(t *testing.T) {
	// 1. Collision check: two close spheres
	res := solveSpatialConstraintsTool(context.Background(), map[string]any{
		"operation": "collision_check",
		"objects": []any{
			map[string]any{
				"name":     "gripper",
				"type":     "sphere",
				"position": []any{0.0, 0.0, 0.0},
				"radius":   0.05,
			},
			map[string]any{
				"name":     "cup",
				"type":     "sphere",
				"position": []any{0.08, 0.0, 0.0},
				"radius":   0.04,
			},
		},
	})
	if !res.Success {
		t.Fatalf("collision check failed: %v", res.Error)
	}
	if res.Data["has_collision"] != true {
		t.Fatalf("expected collision for overlapping spheres at d=0.08m with r1=0.05, r2=0.04: %v", res.Data)
	}

	// 2. Collision check: separated spheres
	resSeparated := solveSpatialConstraintsTool(context.Background(), map[string]any{
		"operation": "collision_check",
		"objects": []any{
			map[string]any{
				"name":     "obstacle_1",
				"type":     "sphere",
				"position": []any{0.0, 0.0, 0.0},
				"radius":   0.1,
			},
			map[string]any{
				"name":     "obstacle_2",
				"type":     "sphere",
				"position": []any{0.5, 0.0, 0.0},
				"radius":   0.1,
			},
		},
	})
	if !resSeparated.Success || resSeparated.Data["has_collision"] != false {
		t.Fatalf("expected no collision for separated obstacles: %v", resSeparated.Data)
	}
	if clearance, ok := resSeparated.Data["min_clearance_m"].(float64); !ok || clearance < 0.29 {
		t.Fatalf("expected ~0.3m clearance, got %v", clearance)
	}

	// 3. Reachability check: valid target within arm workspace
	resReachable := solveSpatialConstraintsTool(context.Background(), map[string]any{
		"operation": "reachability_check",
		"target":    []any{0.4, 0.2, 0.3},
	})
	if !resReachable.Success || resReachable.Data["reachable"] != true {
		t.Fatalf("expected target to be reachable: %v", resReachable.Data)
	}

	// 4. Reachability check: target beyond arm maximum reach
	resOutReach := solveSpatialConstraintsTool(context.Background(), map[string]any{
		"operation": "reachability_check",
		"target":    []any{1.5, 0.0, 0.0},
	})
	if !resOutReach.Success || resOutReach.Data["reachable"] != false {
		t.Fatalf("expected target at 1.5m to exceed maximum reach: %v", resOutReach.Data)
	}
}
