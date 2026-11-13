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

	case "inverse_kinematics_2d":
		target, ok := params["target"].([]any)
		if !ok || len(target) < 2 {
			return failed(errors.New("inverse_kinematics_2d requires 2D [x, y] 'target' coordinate"))
		}
		tx, ty := toFloat(target[0]), toFloat(target[1])
		l1, l2 := 0.4, 0.4 // default link lengths (40cm each)
		if links, ok := params["link_lengths"].([]any); ok && len(links) >= 2 {
			l1, l2 = toFloat(links[0]), toFloat(links[1])
		}
		elbow := strings.ToLower(str(params, "elbow", "up"))

		d2 := tx*tx + ty*ty
		d := math.Sqrt(d2)
		maxReach := l1 + l2
		minReach := math.Abs(l1 - l2)

		if d > maxReach || d < minReach {
			return toolResult{
				Success: true,
				Data: map[string]any{
					"operation":   "inverse_kinematics_2d",
					"target":      []float64{tx, ty},
					"reachable":   false,
					"distance_m":  math.Round(d*1000) / 1000,
					"workspace_m": []float64{minReach, maxReach},
					"reason":      fmt.Sprintf("target distance %.3fm is outside planar workspace [%.3f, %.3f]m", d, minReach, maxReach),
				},
			}
		}

		cosTheta2 := (d2 - l1*l1 - l2*l2) / (2.0 * l1 * l2)
		if cosTheta2 > 1.0 {
			cosTheta2 = 1.0
		} else if cosTheta2 < -1.0 {
			cosTheta2 = -1.0
		}
		theta2 := math.Acos(cosTheta2)
		if elbow == "down" {
			theta2 = -theta2
		}
		theta1 := math.Atan2(ty, tx) - math.Atan2(l2*math.Sin(theta2), l1+l2*math.Cos(theta2))

		elbowX := l1 * math.Cos(theta1)
		elbowY := l1 * math.Sin(theta1)

		deg1 := math.Round(theta1*180.0/math.Pi*100) / 100
		deg2 := math.Round(theta2*180.0/math.Pi*100) / 100

		return toolResult{
			Success: true,
			Data: map[string]any{
				"operation":        "inverse_kinematics_2d",
				"target":           []float64{tx, ty},
				"reachable":        true,
				"distance_m":       math.Round(d*1000) / 1000,
				"theta1_rad":       math.Round(theta1*1000) / 1000,
				"theta2_rad":       math.Round(theta2*1000) / 1000,
				"theta1_deg":       deg1,
				"theta2_deg":       deg2,
				"elbow_position":   []float64{math.Round(elbowX*1000) / 1000, math.Round(elbowY*1000) / 1000},
				"end_effector_pos": []float64{math.Round(tx*1000) / 1000, math.Round(ty*1000) / 1000},
			},
		}

	case "trajectory_check":
		rawWaypoints, ok := params["waypoints"].([]any)
		if !ok || len(rawWaypoints) < 2 {
			return failed(errors.New("trajectory_check requires at least 2 waypoints in 'waypoints' list"))
		}
		var waypoints [][3]float64
		for _, rw := range rawWaypoints {
			if wp, ok := rw.([]any); ok && len(wp) == 3 {
				waypoints = append(waypoints, [3]float64{toFloat(wp[0]), toFloat(wp[1]), toFloat(wp[2])})
			}
		}
		if len(waypoints) < 2 {
			return failed(errors.New("trajectory_check requires valid 3D points [x, y, z] for each waypoint"))
		}

		// Parse obstacles
		rawObjs, _ := params["obstacles"].([]any)
		var objs []spatialObject
		for _, ro := range rawObjs {
			m, ok := ro.(map[string]any)
			if !ok {
				continue
			}
			obj := spatialObject{
				Name: str(m, "name", fmt.Sprintf("obs_%d", len(objs))),
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
				obj.Radius = 0.05
			}
			objs = append(objs, obj)
		}

		safetyMargin := 0.05 // default 5cm margin
		if sm := toFloat(params["safety_margin_m"]); sm > 0 {
			safetyMargin = sm
		}

		totalPathLength := 0.0
		minClearance := math.MaxFloat64
		var collisionSegments []map[string]any
		isSafe := true

		for i := 0; i < len(waypoints)-1; i++ {
			p1 := waypoints[i]
			p2 := waypoints[i+1]
			dx := p2[0] - p1[0]
			dy := p2[1] - p1[1]
			dz := p2[2] - p1[2]
			segLen := math.Sqrt(dx*dx + dy*dy + dz*dz)
			totalPathLength += segLen

			// Check against all obstacles along segment p1 -> p2
			for _, obs := range objs {
				// Approximate obstacle radius
				rObs := obs.Radius
				if obs.Type == "box" {
					rObs = math.Sqrt(obs.Size[0]*obs.Size[0]+obs.Size[1]*obs.Size[1]+obs.Size[2]*obs.Size[2]) / 2.0
				}

				// Project obstacle center onto segment
				var t float64
				if segLen > 1e-6 {
					vx := p2[0] - p1[0]
					vy := p2[1] - p1[1]
					vz := p2[2] - p1[2]
					dot := (obs.Position[0]-p1[0])*vx + (obs.Position[1]-p1[1])*vy + (obs.Position[2]-p1[2])*vz
					t = dot / (segLen * segLen)
					if t < 0 {
						t = 0
					} else if t > 1 {
						t = 1
					}
				}
				closestX := p1[0] + t*(p2[0]-p1[0])
				closestY := p1[1] + t*(p2[1]-p1[1])
				closestZ := p1[2] + t*(p2[2]-p1[2])

				distX := obs.Position[0] - closestX
				distY := obs.Position[1] - closestY
				distZ := obs.Position[2] - closestZ
				dist := math.Sqrt(distX*distX + distY*distY + distZ*distZ)

				clearance := dist - (rObs + safetyMargin)
				if clearance < minClearance {
					minClearance = clearance
				}
				if clearance <= 0.0 {
					isSafe = false
					collisionSegments = append(collisionSegments, map[string]any{
						"segment_index":  i,
						"start_waypoint": p1,
						"end_waypoint":   p2,
						"obstacle":       obs.Name,
						"clearance_m":    math.Round(clearance*1000) / 1000,
					})
				}
			}
		}

		return toolResult{
			Success: true,
			Data: map[string]any{
				"operation":          "trajectory_check",
				"is_safe":            isSafe,
				"waypoint_count":     len(waypoints),
				"path_length_m":      math.Round(totalPathLength*1000) / 1000,
				"min_clearance_m":    math.Round(minClearance*1000) / 1000,
				"collision_segments": collisionSegments,
			},
		}

	default:
		return failed(fmt.Errorf("unknown operation '%s'; supported: collision_check, distance_matrix, reachability_check, inverse_kinematics_2d, trajectory_check", op))
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
