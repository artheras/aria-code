package main

// A five-field cron expression (minute hour day-of-month month day-of-week),
// as APScheduler's CronTrigger.from_crontab read it for the Python daemon:
// *, n, a-b, */s, a-b/s and lists, month and weekday names, 7 for Sunday.
// When both day fields are restricted, a day matches either (Vixie cron).

import (
	"fmt"
	"strconv"
	"strings"
	"time"
)

type cronSpec struct {
	minute, hour, dom, month, dow [64]bool
	domAny, dowAny                bool
}

var cronNames = map[int]map[string]int{
	3: {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12},
	4: {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6},
}

func parseCron(expr string) (*cronSpec, error) {
	fields := strings.Fields(expr)
	if len(fields) != 5 {
		return nil, fmt.Errorf("cron needs five fields (minute hour day month weekday), got %q", expr)
	}
	spec := &cronSpec{}
	bounds := [5][2]int{{0, 59}, {0, 23}, {1, 31}, {1, 12}, {0, 7}}
	targets := []*[64]bool{&spec.minute, &spec.hour, &spec.dom, &spec.month, &spec.dow}
	for i, field := range fields {
		if err := cronField(strings.ToLower(field), bounds[i][0], bounds[i][1], cronNames[i], targets[i]); err != nil {
			return nil, fmt.Errorf("cron field %d (%q): %w", i+1, field, err)
		}
	}
	if spec.dow[7] {
		spec.dow[0] = true
	}
	spec.domAny, spec.dowAny = fields[2] == "*" || fields[2] == "?", fields[4] == "*" || fields[4] == "?"
	return spec, nil
}

func cronValue(text string, names map[string]int) (int, error) {
	if v, ok := names[text]; ok {
		return v, nil
	}
	return strconv.Atoi(text)
}

func cronField(field string, lo, hi int, names map[string]int, out *[64]bool) error {
	for _, part := range strings.Split(field, ",") {
		rangePart, stepText, hasStep := strings.Cut(part, "/")
		step := 1
		if hasStep {
			n, err := strconv.Atoi(stepText)
			if err != nil || n < 1 {
				return fmt.Errorf("bad step %q", stepText)
			}
			step = n
		}
		start, end := lo, hi
		switch {
		case rangePart == "*" || rangePart == "?":
		case strings.Contains(rangePart, "-"):
			a, b, _ := strings.Cut(rangePart, "-")
			var err error
			if start, err = cronValue(a, names); err != nil {
				return fmt.Errorf("bad value %q", a)
			}
			if end, err = cronValue(b, names); err != nil {
				return fmt.Errorf("bad value %q", b)
			}
		default:
			v, err := cronValue(rangePart, names)
			if err != nil {
				return fmt.Errorf("bad value %q", rangePart)
			}
			start = v
			if hasStep {
				end = hi
			} else {
				end = v
			}
		}
		if start < lo || end > hi || start > end {
			return fmt.Errorf("%d-%d is outside %d-%d", start, end, lo, hi)
		}
		for v := start; v <= end; v += step {
			out[v] = true
		}
	}
	return nil
}

// matches reports whether t (in the schedule's zone) is a minute the spec fires on.
func (c *cronSpec) matches(t time.Time) bool {
	if !c.minute[t.Minute()] || !c.hour[t.Hour()] || !c.month[int(t.Month())] {
		return false
	}
	dom, dow := c.dom[t.Day()], c.dow[int(t.Weekday())]
	switch {
	case c.domAny && c.dowAny:
		return true
	case c.domAny:
		return dow
	case c.dowAny:
		return dom
	default:
		return dom || dow
	}
}
