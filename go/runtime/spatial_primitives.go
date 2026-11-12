package main

import (
	"context"
	"errors"
	"fmt"
	"math"
	"strconv"
	"strings"
)

type spatialObject struct {
	Name     string     `json:"name"`
	Type     string     `json:"type"` // "sphere", "box", "point"
	Position [3]float64 `json:"position"`
	Radius   float64    `json:"radius,omitempty"`
	Size     [3]float64 `json:"size,omitempty"` // [dx, dy, dz]
}

type spatialPairDistance struct {
	ObjectA     string  `json:"object_a"`
	ObjectB     string  `json:"object_b"`
	CenterDistM float64 `json:"center_distance_m"`
	ClearanceM  float64 `json:"clearance_m"`
	IsColliding bool    `json:"is_colliding"`
}

// solveSpatialConstraintsTool provides deterministic geometric, collision and
// kinematic reachability checks for physical AI and spatial agents.
func solveSpatialConstraintsTool(ctx context.Context, params map[string]any) toolResult {
	op := strings.ToLower(strings.TrimSpace(str(params, "operation", "collision_check")))

	switch op {
	case "collision_check", "distance_matrix":
		rawObjs, _ := params["objects"].([]any)
		if len(rawObjs) < 2 {
			return failed(errors.New("collision_check requires at least 2 objects in 'objects' list"))
		}
		var objs []spatialObject
		for _, ro := range rawObjs {
			m, ok := ro.(map[string]any)
			if !ok {
				continue
			}
			obj := spatialObject{
				Name: str(m, "name", fmt.Sprintf("obj_%d", len(objs))),
				Type: strings.ToLower(str(m, "type", "sphere")),
			}
			if pos, ok := m["position"].([]any); ok && len(pos) == 3 {
				obj.Position = [3]float64{toFloat(pos[0]), toFloat(pos[1]), toFloat(pos[2])}
			}
			if sz, ok := m["size"].([]any); ok && len(sz) == 3 {
				obj.Size = [3]float64{toFloat(sz[0]), toFloat(sz[1]), toFloat(sz[2])}
			}
			obj.Radius = toFloat(m["radius"])
			if obj.Type == "sphere" && obj.Radius <= 0 {
				obj.Radius = 0.05 // default 5cm sphere
			}
			objs = append(objs, obj)
		}

		var pairs []spatialPairDistance
		var collidingPairs [][]string
		minClearance := math.MaxFloat64
		hasCollision := false

		for i := 0; i < len(objs); i++ {
			for j := i + 1; j < len(objs); j++ {
				a, b := objs[i], objs[j]
				dx := a.Position[0] - b.Position[0]
				dy := a.Position[1] - b.Position[1]
				dz := a.Position[2] - b.Position[2]
				centerDist := math.Sqrt(dx*dx + dy*dy + dz*dz)

				// Approximate bounding radius
				rA := a.Radius
				if a.Type == "box" {
					rA = math.Sqrt(a.Size[0]*a.Size[0]+a.Size[1]*a.Size[1]+a.Size[2]*a.Size[2]) / 2.0
				}
				rB := b.Radius
				if b.Type == "box" {
					rB = math.Sqrt(b.Size[0]*b.Size[0]+b.Size[1]*b.Size[1]+b.Size[2]*b.Size[2]) / 2.0
				}

				clearance := centerDist - (rA + rB)
				isColliding := clearance <= 0.0
				if isColliding {
					hasCollision = true
					collidingPairs = append(collidingPairs, []string{a.Name, b.Name})
				}
				if clearance < minClearance {
					minClearance = clearance
				}

				pairs = append(pairs, spatialPairDistance{
					ObjectA:     a.Name,
					ObjectB:     b.Name,
					CenterDistM: math.Round(centerDist*1000) / 1000,
					ClearanceM:  math.Round(clearance*1000) / 1000,
					IsColliding: isColliding,
				})
			}
		}

		return toolResult{
			Success: true,
			Data: map[string]any{
				"operation":       op,
				"has_collision":   hasCollision,
				"min_clearance_m": math.Round(minClearance*1000) / 1000,
				"collisions":      collidingPairs,
				"pairs":           pairs,
			},
		}

	case "reachability_check":
		target, ok := params["target"].([]any)
		if !ok || len(target) != 3 {
			return failed(errors.New("reachability_check requires 3D [x, y, z] 'target' coordinate"))
		}
		tx, ty, tz := toFloat(target[0]), toFloat(target[1]), toFloat(target[2])

		// Arm workspace limits (defaults to standard 6-DOF industrial/collaborative robot arm)
		baseX, baseY, baseZ := 0.0, 0.0, 0.0
		minRadius, maxRadius := 0.15, 0.85 // 15cm to 85cm reach
		minZ, maxZ := -0.10, 1.20          // -10cm table to 120cm height

		if limits, ok := params["arm_limits"].(map[string]any); ok {
			if b, ok := limits["base"].([]any); ok && len(b) == 3 {
				baseX, baseY, baseZ = toFloat(b[0]), toFloat(b[1]), toFloat(b[2])
			}
			if mr := toFloat(limits["min_radius"]); mr > 0 {
				minRadius = mr
			}
			if mr := toFloat(limits["max_radius"]); mr > 0 {
				maxRadius = mr
			}
			if mz := toFloat(limits["min_z"]); mz != 0 {
				minZ = mz
			}
			if mz := toFloat(limits["max_z"]); mz != 0 {
				maxZ = mz
			}
		}

		dx := tx - baseX
		dy := ty - baseY
		dz := tz - baseZ
		dist := math.Sqrt(dx*dx + dy*dy + dz*dz)

		var violations []string
		if dist < minRadius {
			violations = append(violations, fmt.Sprintf("target distance %.3fm is inside arm minimum singularity radius %.3fm", dist, minRadius))
		}
		if dist > maxRadius {
			violations = append(violations, fmt.Sprintf("target distance %.3fm exceeds maximum arm reach %.3fm", dist, maxRadius))
		}
		if tz < minZ {
			violations = append(violations, fmt.Sprintf("target z=%.3fm is below allowable workspace lower bound %.3fm", tz, minZ))
		}
		if tz > maxZ {
			violations = append(violations, fmt.Sprintf("target z=%.3fm exceeds allowable workspace upper bound %.3fm", tz, maxZ))
		}

		reachable := len(violations) == 0

		return toolResult{
			Success: true,
			Data: map[string]any{
				"operation":          "reachability_check",
				"target":             []float64{tx, ty, tz},
				"distance_to_base_m": math.Round(dist*1000) / 1000,
				"reachable":          reachable,
				"violations":         violations,
			},
		}

	default:
		return failed(fmt.Errorf("unknown operation '%s'; supported: collision_check, distance_matrix, reachability_check", op))
	}
}

func toFloat(val any) float64 {
	switch v := val.(type) {
	case float64:
		return v
	case float32:
		return float64(v)
	case int:
		return float64(v)
	case int64:
		return float64(v)
	case string:
		f, _ := strconv.ParseFloat(v, 64)
		return f
	default:
		return 0.0
	}
}
