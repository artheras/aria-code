package main

import (
	"bufio"
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

type asset3DInfo struct {
	Format          string     `json:"format"`
	FileSizeBytes   int64      `json:"file_size_bytes"`
	VertexCount     int        `json:"vertex_count"`
	TriangleCount   int        `json:"triangle_count"`
	BoundingBoxMin  [3]float64 `json:"bounding_box_min"`
	BoundingBoxMax  [3]float64 `json:"bounding_box_max"`
	Dimensions      [3]float64 `json:"dimensions"`
	Center          [3]float64 `json:"center"`
	MaterialCount   int        `json:"material_count"`
	TextureCount    int        `json:"texture_count"`
	AnimationCount  int        `json:"animation_count"`
	Recommendations []string   `json:"recommendations,omitempty"`
	Summary         string     `json:"summary"`
}

// inspect3DAssetTool extracts structural metadata, bounding box, triangle counts,
// and WebGL rendering recommendations from 3D model files (.gltf, .glb, .obj, .stl).
// Avoids dumping raw vertices into the LLM context, keeping token usage under 150 tokens.
func inspect3DAssetTool(ctx context.Context, sc *scope, params map[string]any) toolResult {
	rawPath := strings.TrimSpace(str(params, "path", ""))
	if rawPath == "" {
		return failed(errors.New("inspect_3d_asset requires 'path' parameter"))
	}
	scoped, err := sc.open(rawPath, false)
	if err != nil {
		return failed(err)
	}
	defer scoped.Close()
	targetPath := scoped.absolute

	stat, err := os.Stat(targetPath)
	if err != nil {
		return failed(err)
	}

	ext := strings.ToLower(filepath.Ext(targetPath))
	var info *asset3DInfo

	switch ext {
	case ".obj":
		info, err = inspectObj(targetPath, stat.Size())
	case ".stl":
		info, err = inspectStl(targetPath, stat.Size())
	case ".gltf":
		info, err = inspectGltf(targetPath, stat.Size())
	case ".glb":
		info, err = inspectGlb(targetPath, stat.Size())
	default:
		return failed(fmt.Errorf("unsupported 3D asset format '%s'; supported: .gltf, .glb, .obj, .stl", ext))
	}

	if err != nil {
		return failed(fmt.Errorf("failed to inspect 3D asset: %w", err))
	}

	// Calculate dimensions and center
	for i := 0; i < 3; i++ {
		info.Dimensions[i] = math.Round((info.BoundingBoxMax[i]-info.BoundingBoxMin[i])*1000) / 1000
		info.Center[i] = math.Round(((info.BoundingBoxMax[i]+info.BoundingBoxMin[i])/2.0)*1000) / 1000
		info.BoundingBoxMin[i] = math.Round(info.BoundingBoxMin[i]*1000) / 1000
		info.BoundingBoxMax[i] = math.Round(info.BoundingBoxMax[i]*1000) / 1000
	}

	// Recommendations
	maxDim := math.Max(info.Dimensions[0], math.Max(info.Dimensions[1], info.Dimensions[2]))
	if info.TriangleCount > 150000 {
		info.Recommendations = append(info.Recommendations, fmt.Sprintf("High polygon count (%d triangles): consider decimation/LOD before interactive browser WebGL rendering.", info.TriangleCount))
	}
	if maxDim > 500.0 {
		info.Recommendations = append(info.Recommendations, fmt.Sprintf("Large dimensions (max %.2f): coordinate units appear to be millimeters; consider 0.001 scaling to meters for physics simulations.", maxDim))
	} else if maxDim < 0.005 && maxDim > 0 {
		info.Recommendations = append(info.Recommendations, fmt.Sprintf("Small dimensions (max %.4f): check if unit scaling is appropriate.", maxDim))
	}

	info.Summary = fmt.Sprintf("%s 3D model | %d triangles | %d vertices | BoundingBox: [%.2f, %.2f, %.2f]m | Size: %.1f KB",
		strings.ToUpper(strings.TrimPrefix(info.Format, ".")),
		info.TriangleCount,
		info.VertexCount,
		info.Dimensions[0], info.Dimensions[1], info.Dimensions[2],
		float64(info.FileSizeBytes)/1024.0,
	)

	return toolResult{
		Success: true,
		Data: map[string]any{
			"format":           info.Format,
			"file_size_bytes":  info.FileSizeBytes,
			"vertex_count":     info.VertexCount,
			"triangle_count":   info.TriangleCount,
			"bounding_box_min": info.BoundingBoxMin,
			"bounding_box_max": info.BoundingBoxMax,
			"dimensions":       info.Dimensions,
			"center":           info.Center,
			"material_count":   info.MaterialCount,
			"texture_count":    info.TextureCount,
			"animation_count":  info.AnimationCount,
			"recommendations":  info.Recommendations,
			"summary":          info.Summary,
		},
	}
}

func inspectObj(path string, size int64) (*asset3DInfo, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()

	info := &asset3DInfo{
		Format:         ".obj",
		FileSizeBytes:  size,
		BoundingBoxMin: [3]float64{math.MaxFloat64, math.MaxFloat64, math.MaxFloat64},
		BoundingBoxMax: [3]float64{-math.MaxFloat64, -math.MaxFloat64, -math.MaxFloat64},
	}

	materials := make(map[string]bool)
	scanner := bufio.NewScanner(file)
	hasVertices := false

	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if len(line) < 2 || line[0] == '#' {
			continue
		}
		parts := strings.Fields(line)
		if len(parts) == 0 {
			continue
		}

		switch parts[0] {
		case "v":
			if len(parts) >= 4 {
				x, _ := strconv.ParseFloat(parts[1], 64)
				y, _ := strconv.ParseFloat(parts[2], 64)
				z, _ := strconv.ParseFloat(parts[3], 64)
				info.VertexCount++
				hasVertices = true
				if x < info.BoundingBoxMin[0] {
					info.BoundingBoxMin[0] = x
				}
				if x > info.BoundingBoxMax[0] {
					info.BoundingBoxMax[0] = x
				}
				if y < info.BoundingBoxMin[1] {
					info.BoundingBoxMin[1] = y
				}
				if y > info.BoundingBoxMax[1] {
					info.BoundingBoxMax[1] = y
				}
				if z < info.BoundingBoxMin[2] {
					info.BoundingBoxMin[2] = z
				}
				if z > info.BoundingBoxMax[2] {
					info.BoundingBoxMax[2] = z
				}
			}
		case "f":
			verts := len(parts) - 1
			if verts >= 3 {
				info.TriangleCount += (verts - 2)
			}
		case "usemtl":
			if len(parts) > 1 {
				materials[parts[1]] = true
			}
		}
	}

	if !hasVertices {
		info.BoundingBoxMin = [3]float64{0, 0, 0}
		info.BoundingBoxMax = [3]float64{0, 0, 0}
	}
	info.MaterialCount = len(materials)
	return info, nil
}

func inspectStl(path string, size int64) (*asset3DInfo, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()

	info := &asset3DInfo{
		Format:         ".stl",
		FileSizeBytes:  size,
		BoundingBoxMin: [3]float64{math.MaxFloat64, math.MaxFloat64, math.MaxFloat64},
		BoundingBoxMax: [3]float64{-math.MaxFloat64, -math.MaxFloat64, -math.MaxFloat64},
	}

	header := make([]byte, 84)
	n, _ := io.ReadFull(file, header)

	// Check if binary STL: size == 84 + 50 * numTriangles
	if n == 84 {
		expectedTris := binary.LittleEndian.Uint32(header[80:84])
		expectedSize := int64(84) + int64(expectedTris)*50
		if expectedSize == size {
			// Binary STL
			info.TriangleCount = int(expectedTris)
			info.VertexCount = info.TriangleCount * 3

			// Read triangles (sample up to 50000 to keep bounded memory/time)
			triBuf := make([]byte, 50)
			limit := expectedTris
			stride := 1
			if limit > 50000 {
				stride = int(limit / 50000)
			}

			for i := uint32(0); i < limit; i++ {
				if _, err := io.ReadFull(file, triBuf); err != nil {
					break
				}
				if i%uint32(stride) != 0 {
					continue
				}
				// 3 vertices, each 3 float32 (12 bytes per vertex, starting after 12 bytes normal)
				for v := 0; v < 3; v++ {
					offset := 12 + v*12
					x := float64(math.Float32frombits(binary.LittleEndian.Uint32(triBuf[offset : offset+4])))
					y := float64(math.Float32frombits(binary.LittleEndian.Uint32(triBuf[offset+4 : offset+8])))
					z := float64(math.Float32frombits(binary.LittleEndian.Uint32(triBuf[offset+8 : offset+12])))

					if x < info.BoundingBoxMin[0] {
						info.BoundingBoxMin[0] = x
					}
					if x > info.BoundingBoxMax[0] {
						info.BoundingBoxMax[0] = x
					}
					if y < info.BoundingBoxMin[1] {
						info.BoundingBoxMin[1] = y
					}
					if y > info.BoundingBoxMax[1] {
						info.BoundingBoxMax[1] = y
					}
					if z < info.BoundingBoxMin[2] {
						info.BoundingBoxMin[2] = z
					}
					if z > info.BoundingBoxMax[2] {
						info.BoundingBoxMax[2] = z
					}
				}
			}
			return info, nil
		}
	}

	// ASCII STL fallback
	_, _ = file.Seek(0, io.SeekStart)
	scanner := bufio.NewScanner(file)
	hasVertices := false

	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if strings.HasPrefix(line, "vertex") {
			parts := strings.Fields(line)
			if len(parts) >= 4 {
				x, _ := strconv.ParseFloat(parts[1], 64)
				y, _ := strconv.ParseFloat(parts[2], 64)
				z, _ := strconv.ParseFloat(parts[3], 64)
				info.VertexCount++
				hasVertices = true
				if x < info.BoundingBoxMin[0] {
					info.BoundingBoxMin[0] = x
				}
				if x > info.BoundingBoxMax[0] {
					info.BoundingBoxMax[0] = x
				}
				if y < info.BoundingBoxMin[1] {
					info.BoundingBoxMin[1] = y
				}
				if y > info.BoundingBoxMax[1] {
					info.BoundingBoxMax[1] = y
				}
				if z < info.BoundingBoxMin[2] {
					info.BoundingBoxMin[2] = z
				}
				if z > info.BoundingBoxMax[2] {
					info.BoundingBoxMax[2] = z
				}
			}
		} else if strings.HasPrefix(line, "endfacet") {
			info.TriangleCount++
		}
	}

	if !hasVertices {
		info.BoundingBoxMin = [3]float64{0, 0, 0}
		info.BoundingBoxMax = [3]float64{0, 0, 0}
	}
	return info, nil
}

func inspectGltf(path string, size int64) (*asset3DInfo, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return parseGltfJson(data, ".gltf", size)
}

func inspectGlb(path string, size int64) (*asset3DInfo, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()

	// GLB header: magic (4), version (4), length (4)
	header := make([]byte, 12)
	if _, err := io.ReadFull(file, header); err != nil {
		return nil, err
	}
	magic := binary.LittleEndian.Uint32(header[0:4])
	if magic != 0x46546C67 { // "glTF"
		return nil, errors.New("invalid GLB magic header")
	}

	// Chunk 0: length (4), type (4)
	chunkHeader := make([]byte, 8)
	if _, err := io.ReadFull(file, chunkHeader); err != nil {
		return nil, err
	}
	chunkLen := binary.LittleEndian.Uint32(chunkHeader[0:4])
	chunkType := binary.LittleEndian.Uint32(chunkHeader[4:8])
	if chunkType != 0x4E4F534A { // "JSON"
		return nil, errors.New("expected GLB chunk 0 to be JSON")
	}

	jsonData := make([]byte, chunkLen)
	if _, err := io.ReadFull(file, jsonData); err != nil {
		return nil, err
	}

	return parseGltfJson(jsonData, ".glb", size)
}

func parseGltfJson(data []byte, format string, size int64) (*asset3DInfo, error) {
	var root struct {
		Accessors []struct {
			Count int       `json:"count"`
			Type  string    `json:"type"`
			Min   []float64 `json:"min"`
			Max   []float64 `json:"max"`
		} `json:"accessors"`
		Meshes []struct {
			Primitives []struct {
				Indices    *int           `json:"indices"`
				Attributes map[string]int `json:"attributes"`
			} `json:"primitives"`
		} `json:"meshes"`
		Materials  []any `json:"materials"`
		Textures   []any `json:"textures"`
		Animations []any `json:"animations"`
	}

	if err := json.Unmarshal(data, &root); err != nil {
		return nil, err
	}

	info := &asset3DInfo{
		Format:         format,
		FileSizeBytes:  size,
		BoundingBoxMin: [3]float64{math.MaxFloat64, math.MaxFloat64, math.MaxFloat64},
		BoundingBoxMax: [3]float64{-math.MaxFloat64, -math.MaxFloat64, -math.MaxFloat64},
		MaterialCount:  len(root.Materials),
		TextureCount:   len(root.Textures),
		AnimationCount: len(root.Animations),
	}

	hasBounds := false
	for _, acc := range root.Accessors {
		if len(acc.Min) >= 3 && len(acc.Max) >= 3 {
			hasBounds = true
			if acc.Min[0] < info.BoundingBoxMin[0] {
				info.BoundingBoxMin[0] = acc.Min[0]
			}
			if acc.Max[0] > info.BoundingBoxMax[0] {
				info.BoundingBoxMax[0] = acc.Max[0]
			}
			if acc.Min[1] < info.BoundingBoxMin[1] {
				info.BoundingBoxMin[1] = acc.Min[1]
			}
			if acc.Max[1] > info.BoundingBoxMax[1] {
				info.BoundingBoxMax[1] = acc.Max[1]
			}
			if acc.Min[2] < info.BoundingBoxMin[2] {
				info.BoundingBoxMin[2] = acc.Min[2]
			}
			if acc.Max[2] > info.BoundingBoxMax[2] {
				info.BoundingBoxMax[2] = acc.Max[2]
			}
		}
	}

	// Calculate triangle counts
	for _, mesh := range root.Meshes {
		for _, prim := range mesh.Primitives {
			if prim.Indices != nil && *prim.Indices < len(root.Accessors) {
				idxAcc := root.Accessors[*prim.Indices]
				info.TriangleCount += idxAcc.Count / 3
			} else if posIdx, ok := prim.Attributes["POSITION"]; ok && posIdx < len(root.Accessors) {
				posAcc := root.Accessors[posIdx]
				info.VertexCount += posAcc.Count
				info.TriangleCount += posAcc.Count / 3
			}
		}
	}

	if !hasBounds {
		info.BoundingBoxMin = [3]float64{0, 0, 0}
		info.BoundingBoxMax = [3]float64{0, 0, 0}
	}
	return info, nil
}
