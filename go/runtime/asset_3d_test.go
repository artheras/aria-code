package main

import (
	"context"
	"encoding/binary"
	"math"
	"os"
	"path/filepath"
	"testing"
)

func TestInspect3DAsset_OBJ(t *testing.T) {
	tmpDir := t.TempDir()
	objPath := filepath.Join(tmpDir, "cube.obj")
	objContent := `# Simple cube
v -1.0 -1.0 -1.0
v 1.0 -1.0 -1.0
v 1.0 1.0 -1.0
v -1.0 1.0 -1.0
v -1.0 -1.0 1.0
v 1.0 -1.0 1.0
v 1.0 1.0 1.0
v -1.0 1.0 1.0
usemtl MaterialDefault
f 1 2 3
f 1 3 4
f 5 6 7
f 5 7 8
`
	if err := os.WriteFile(objPath, []byte(objContent), 0644); err != nil {
		t.Fatal(err)
	}

	sc, err := newScope(tmpDir, nil, nil, true)
	if err != nil {
		t.Fatal(err)
	}
	res := inspect3DAssetTool(context.Background(), sc, map[string]any{"path": objPath})
	if !res.Success {
		t.Fatalf("expected success, got err: %s", res.Error)
	}

	data := res.Data
	if data["format"] != ".obj" {
		t.Errorf("expected format .obj, got %v", data["format"])
	}
	if data["vertex_count"] != 8 {
		t.Errorf("expected 8 vertices, got %v", data["vertex_count"])
	}
	if data["triangle_count"] != 4 {
		t.Errorf("expected 4 triangles, got %v", data["triangle_count"])
	}
	dims := data["dimensions"].([3]float64)
	if dims[0] != 2.0 || dims[1] != 2.0 || dims[2] != 2.0 {
		t.Errorf("expected dimensions [2, 2, 2], got %v", dims)
	}
	if data["material_count"] != 1 {
		t.Errorf("expected 1 material, got %v", data["material_count"])
	}
}

func TestInspect3DAsset_ASCII_STL(t *testing.T) {
	tmpDir := t.TempDir()
	stlPath := filepath.Join(tmpDir, "triangle.stl")
	stlContent := `solid test
  facet normal 0.0 0.0 1.0
    outer loop
      vertex 0.0 0.0 0.0
      vertex 1.0 0.0 0.0
      vertex 0.0 1.0 0.0
    endloop
  endfacet
endsolid test
`
	if err := os.WriteFile(stlPath, []byte(stlContent), 0644); err != nil {
		t.Fatal(err)
	}

	sc, err := newScope(tmpDir, nil, nil, true)
	if err != nil {
		t.Fatal(err)
	}
	res := inspect3DAssetTool(context.Background(), sc, map[string]any{"path": stlPath})
	if !res.Success {
		t.Fatalf("expected success, got err: %s", res.Error)
	}

	data := res.Data
	if data["format"] != ".stl" {
		t.Errorf("expected format .stl, got %v", data["format"])
	}
	if data["triangle_count"] != 1 {
		t.Errorf("expected 1 triangle, got %v", data["triangle_count"])
	}
	if data["vertex_count"] != 3 {
		t.Errorf("expected 3 vertices, got %v", data["vertex_count"])
	}
}

func TestInspect3DAsset_Binary_STL(t *testing.T) {
	tmpDir := t.TempDir()
	stlPath := filepath.Join(tmpDir, "binary.stl")

	// 80 bytes header + 4 bytes triangle count (1) + 50 bytes per triangle = 134 bytes
	buf := make([]byte, 134)
	copy(buf[0:80], []byte("Binary STL Test Header"))
	binary.LittleEndian.PutUint32(buf[80:84], 1) // 1 triangle

	// Normal: 0, 0, 1
	binary.LittleEndian.PutUint32(buf[84:88], math.Float32bits(0.0))
	binary.LittleEndian.PutUint32(buf[88:92], math.Float32bits(0.0))
	binary.LittleEndian.PutUint32(buf[92:96], math.Float32bits(1.0))
	// Vertex 1: 0, 0, 0
	binary.LittleEndian.PutUint32(buf[96:100], math.Float32bits(0.0))
	binary.LittleEndian.PutUint32(buf[100:104], math.Float32bits(0.0))
	binary.LittleEndian.PutUint32(buf[104:108], math.Float32bits(0.0))
	// Vertex 2: 5, 0, 0
	binary.LittleEndian.PutUint32(buf[108:112], math.Float32bits(5.0))
	binary.LittleEndian.PutUint32(buf[112:116], math.Float32bits(0.0))
	binary.LittleEndian.PutUint32(buf[116:120], math.Float32bits(0.0))
	// Vertex 3: 0, 10, 0
	binary.LittleEndian.PutUint32(buf[120:124], math.Float32bits(0.0))
	binary.LittleEndian.PutUint32(buf[124:128], math.Float32bits(10.0))
	binary.LittleEndian.PutUint32(buf[128:132], math.Float32bits(0.0))
	// Attribute bytes: 0
	binary.LittleEndian.PutUint16(buf[132:134], 0)

	if err := os.WriteFile(stlPath, buf, 0644); err != nil {
		t.Fatal(err)
	}

	sc, err := newScope(tmpDir, nil, nil, true)
	if err != nil {
		t.Fatal(err)
	}
	res := inspect3DAssetTool(context.Background(), sc, map[string]any{"path": stlPath})
	if !res.Success {
		t.Fatalf("expected success, got err: %s", res.Error)
	}

	data := res.Data
	if data["triangle_count"] != 1 {
		t.Errorf("expected 1 triangle, got %v", data["triangle_count"])
	}
	dims := data["dimensions"].([3]float64)
	if dims[0] != 5.0 || dims[1] != 10.0 || dims[2] != 0.0 {
		t.Errorf("expected dimensions [5, 10, 0], got %v", dims)
	}
}

func TestInspect3DAsset_GLTF(t *testing.T) {
	tmpDir := t.TempDir()
	gltfPath := filepath.Join(tmpDir, "model.gltf")
	gltfContent := `{
  "asset": {"version": "2.0"},
  "accessors": [
    {
      "count": 36,
      "type": "VEC3",
      "min": [-0.5, 0.0, -0.5],
      "max": [0.5, 1.0, 0.5]
    }
  ],
  "meshes": [
    {
      "primitives": [
        {
          "attributes": {"POSITION": 0}
        }
      ]
    }
  ],
  "materials": [{}],
  "textures": [{}, {}]
}`
	if err := os.WriteFile(gltfPath, []byte(gltfContent), 0644); err != nil {
		t.Fatal(err)
	}

	sc, err := newScope(tmpDir, nil, nil, true)
	if err != nil {
		t.Fatal(err)
	}
	res := inspect3DAssetTool(context.Background(), sc, map[string]any{"path": gltfPath})
	if !res.Success {
		t.Fatalf("expected success, got err: %s", res.Error)
	}

	data := res.Data
	if data["format"] != ".gltf" {
		t.Errorf("expected format .gltf, got %v", data["format"])
	}
	if data["material_count"] != 1 {
		t.Errorf("expected 1 material, got %v", data["material_count"])
	}
	if data["texture_count"] != 2 {
		t.Errorf("expected 2 textures, got %v", data["texture_count"])
	}
	dims := data["dimensions"].([3]float64)
	if dims[0] != 1.0 || dims[1] != 1.0 || dims[2] != 1.0 {
		t.Errorf("expected dimensions [1, 1, 1], got %v", dims)
	}
}
