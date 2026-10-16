package main

import (
	"bytes"
	"context"
	"encoding/json"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"path"
	"strings"
)

type chartPoint struct {
	Index int    `xml:"idx,attr" json:"index"`
	Value string `xml:"v" json:"value"`
}
type chartCache struct {
	Points []chartPoint `xml:"pt"`
}
type chartAxis struct {
	StringRef struct {
		Cache chartCache `xml:"strCache"`
	} `xml:"strRef"`
	NumberRef struct {
		Cache chartCache `xml:"numCache"`
	} `xml:"numRef"`
	StringLiteral chartCache `xml:"strLit"`
	NumberLiteral chartCache `xml:"numLit"`
}

func (a chartAxis) points() []chartPoint {
	for _, p := range [][]chartPoint{a.StringRef.Cache.Points, a.NumberRef.Cache.Points, a.StringLiteral.Points, a.NumberLiteral.Points} {
		if len(p) > 0 {
			return p
		}
	}
	return nil
}

type officeChartSeries struct {
	Name struct {
		Value string `xml:"v"`
		Ref   struct {
			Cache chartCache `xml:"strCache"`
		} `xml:"strRef"`
	} `xml:"tx"`
	Categories chartAxis `xml:"cat"`
	Values     chartAxis `xml:"val"`
	X          chartAxis `xml:"xVal"`
	Y          chartAxis `xml:"yVal"`
	Size       chartAxis `xml:"bubbleSize"`
}

// Only chart relationships actually referenced by this body/slide are followed.
// Cached values are document evidence, not freshly recalculated workbook data.
func officeCharts(ctx context.Context, parent string, ids []string, read func(string) ([]byte, error)) (string, error) {
	if len(ids) == 0 {
		return "", nil
	}
	if len(ids) > 64 {
		return "", errors.New("too many charts in Office component")
	}
	raw, err := read(path.Join(path.Dir(parent), "_rels", path.Base(parent)+".rels"))
	if err != nil {
		return "", err
	}
	var rels struct {
		Items []struct {
			ID     string `xml:"Id,attr"`
			Target string `xml:"Target,attr"`
			Mode   string `xml:"TargetMode,attr"`
			Type   string `xml:"Type,attr"`
		} `xml:"Relationship"`
	}
	if err := xml.Unmarshal(raw, &rels); err != nil {
		return "", err
	}
	targets := map[string]string{}
	for _, r := range rels.Items {
		if _, exists := targets[r.ID]; exists {
			return "", errors.New("duplicate Office chart relationship")
		}
		if !strings.HasSuffix(r.Type, "/chart") {
			continue
		}
		if r.Mode == "External" {
			targets[r.ID] = "external"
			continue
		}
		target := path.Clean(path.Join(path.Dir(parent), r.Target))
		if strings.HasPrefix(r.Target, "/") {
			target = strings.TrimPrefix(path.Clean(r.Target), "/")
		}
		root := strings.Split(parent, "/")[0]
		if !strings.HasPrefix(target, root+"/charts/") || !strings.HasSuffix(target, ".xml") {
			return "", errors.New("invalid Office chart target")
		}
		targets[r.ID] = target
	}
	var out strings.Builder
	for i, id := range ids {
		if err := ctx.Err(); err != nil {
			return "", err
		}
		target, ok := targets[id]
		if !ok {
			return "", errors.New("missing Office chart relationship")
		}
		fmt.Fprintf(&out, "\n[Chart %d in %s: cached data; may be stale, not recalculated. No visual styling inferred.]\n", i+1, parent)
		if target == "external" {
			out.WriteString("External chart not fetched; data unavailable.\n")
			continue
		}
		raw, err := read(target)
		if err != nil {
			return "", err
		}
		d := xml.NewDecoder(bytes.NewReader(raw))
		count := 0
		for {
			if err := ctx.Err(); err != nil {
				return "", err
			}
			token, err := d.Token()
			if err == io.EOF {
				break
			}
			if err != nil {
				return "", err
			}
			start, ok := token.(xml.StartElement)
			if !ok || start.Name.Local != "ser" {
				continue
			}
			var series officeChartSeries
			if err := d.DecodeElement(&series, &start); err != nil {
				return "", err
			}
			count++
			if count > 256 {
				return "", errors.New("too many chart series")
			}
			name := series.Name.Value
			if name == "" && len(series.Name.Ref.Cache.Points) > 0 {
				name = series.Name.Ref.Cache.Points[0].Value
			}
			fields := map[string]any{"series": name}
			for key, axis := range map[string]chartAxis{"categories": series.Categories, "values": series.Values, "x": series.X, "y": series.Y, "bubble_size": series.Size} {
				points := axis.points()
				if len(points) > 10000 {
					return "", errors.New("chart point limit exceeded")
				}
				seen := map[int]bool{}
				for _, point := range points {
					if point.Index < 0 || seen[point.Index] {
						return "", errors.New("invalid or duplicate chart point index")
					}
					seen[point.Index] = true
				}
				if len(points) > 0 {
					fields[key] = points
				}
			}
			if len(fields) == 1 {
				fields["status"] = "No supported cached values; source workbook not read."
			}
			encoded, err := json.Marshal(fields)
			if err != nil {
				return "", err
			}
			out.Write(encoded)
			out.WriteByte('\n')
			if out.Len() > documentTextLimit {
				return "", errors.New("chart text exceeds document budget")
			}
		}
		if count == 0 {
			out.WriteString("No supported chart series; chart data unavailable.\n")
		}
	}
	return out.String(), nil
}
