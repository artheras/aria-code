package main

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestParseCron(t *testing.T) {
	shanghai, _ := time.LoadLocation("Asia/Shanghai")
	at := func(s string) time.Time {
		v, err := time.ParseInLocation("2006-01-02 15:04", s, shanghai)
		if err != nil {
			t.Fatal(err)
		}
		return v
	}
	for _, c := range []struct {
		expr, when string
		want       bool
	}{
		{"55 8 * * 1-5", "2026-11-02 08:55", true},  // Monday
		{"55 8 * * 1-5", "2026-11-01 08:55", false}, // Sunday
		{"55 8 * * mon-fri", "2026-11-06 08:55", true},
		{"*/15 * * * *", "2026-11-02 10:45", true},
		{"*/15 * * * *", "2026-11-02 10:46", false},
		{"0 9-17/4 * * *", "2026-11-02 13:00", true},
		{"0 9-17/4 * * *", "2026-11-02 15:00", false},
		{"30 16 * * 5", "2026-11-06 16:30", true},
		{"0 0 * * 7", "2026-11-01 00:00", true}, // 7 is Sunday
		{"0 0 1 * 1", "2026-11-02 00:00", true}, // day 1 OR Monday
		{"0 0 1 * 1", "2026-11-01 00:00", true},
		{"0 0 1 * 1", "2026-11-03 00:00", false},
		{"0 12 * jan,nov *", "2026-11-03 12:00", true},
		{"0 12 * 1,2 *", "2026-11-03 12:00", false},
		{"5,10 * * * *", "2026-11-03 12:10", true},
	} {
		spec, err := parseCron(c.expr)
		if err != nil {
			t.Fatal(c.expr, err)
		}
		if got := spec.matches(at(c.when)); got != c.want {
			t.Errorf("%q at %s = %v", c.expr, c.when, got)
		}
	}
	for _, bad := range []string{"", "* * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *", "*/0 * * * *", "5-1 * * * *", "x * * * *", "* * * * * *"} {
		if _, err := parseCron(bad); err == nil {
			t.Errorf("accepted %q", bad)
		}
	}
}

type fakeMarket struct {
	mu     sync.Mutex
	quotes map[string]quote
	weeks  map[string]float64
	asked  []string
}

func (f *fakeMarket) quote(_ context.Context, symbol string) (quote, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.asked = append(f.asked, symbol)
	q, ok := f.quotes[symbol]
	if !ok {
		return quote{}, errors.New("no quote")
	}
	return q, nil
}

func (f *fakeMarket) weekReturn(_ context.Context, symbol string) (float64, error) {
	r, ok := f.weeks[symbol]
	if !ok {
		return 0, errors.New("no history")
	}
	return r, nil
}

type outbox struct {
	mu       sync.Mutex
	telegram []map[string]any
	feishu   []map[string]any
	desktop  []string
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(req *http.Request) (*http.Response, error) {
	return f(req)
}

// daemonFixture is a daemon on a temporary folder with stand-in channels.
func daemonFixture(t *testing.T) (*daemon, *fakeMarket, *outbox, *sql.DB) {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("ARIA_DAEMON_DIR", dir)
	t.Setenv("ARIA_DAEMON_TZ", "Asia/Shanghai")
	t.Setenv("ARIA_DAEMON_DEFAULT_JOBS", "0")
	box := &outbox{}
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		box.mu.Lock()
		defer box.mu.Unlock()
		if strings.HasPrefix(r.URL.Path, "/bottok/") {
			box.telegram = append(box.telegram, body)
		} else {
			box.feishu = append(box.feishu, body)
		}
		w.WriteHeader(http.StatusOK)
	})
	savedAPI, savedNotify := telegramAPI, desktopNotify
	telegramAPI = "http://daemon.fixture"
	desktopNotify = func(title, body string) {
		box.mu.Lock()
		box.desktop = append(box.desktop, title+"|"+body)
		box.mu.Unlock()
	}
	t.Cleanup(func() { telegramAPI, desktopNotify = savedAPI, savedNotify })
	t.Setenv("TELEGRAM_BOT_TOKEN", "tok")
	t.Setenv("TELEGRAM_ALLOWED_IDS", "101, 202,x")
	t.Setenv("FEISHU_WEBHOOK_URL", "http://daemon.fixture/feishu")
	db, err := openDaemonDB(dir)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	market := &fakeMarket{quotes: map[string]quote{}, weeks: map[string]float64{}}
	d, err := newDaemon(db, market)
	if err != nil {
		t.Fatal(err)
	}
	d.notify.client.Transport = roundTripFunc(func(req *http.Request) (*http.Response, error) {
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
		return rec.Result(), nil
	})
	return d, market, box, db
}

func TestAlertConditions(t *testing.T) {
	q := quote{Price: 105, ChangePct: -3.5, HasChange: true}
	for _, c := range []struct {
		cond  string
		value float64
		want  bool
	}{
		{"price_above", 100, true}, {"price_above", 105, false}, {"price_below", 110, true},
		{"pct_change_above", -4, true}, {"pct_change_below", -3, true}, {"pct_change_below", -4, false}, {"unknown", 0, false},
	} {
		if got := alertFires(alertRow{Condition: c.cond, Value: c.value}, q); got != c.want {
			t.Errorf("%s %v = %v", c.cond, c.value, got)
		}
	}
	if alertFires(alertRow{Condition: "pct_change_below", Value: 1}, quote{Price: 1}) {
		t.Fatal("a day-change alert needs a reported change")
	}
}

func TestAlertsFireAndNotify(t *testing.T) {
	d, market, box, db := daemonFixture(t)
	ctx := context.Background()
	var out bytes.Buffer
	for _, args := range [][]string{
		{"alert", "add", "aapl", "price_above", "200", "--message", "AAPL breakout"},
		{"alert", "add", "AAPL", "pct_change_below", "-2", "--repeat", "--no-telegram"},
		{"alert", "add", "600519", "price_below", "1500"},
	} {
		if err := daemonManage(ctx, db, args, &out); err != nil {
			t.Fatal(args, err)
		}
	}
	market.quotes["AAPL"] = quote{Price: 210.5, ChangePct: -2.4, HasChange: true, Currency: "USD", Source: "yfinance"}
	d.checkAlerts(ctx)
	d.checkAlerts(ctx)
	if strings.Count(strings.Join(market.asked, ","), "AAPL") != 2 {
		t.Fatal("one quote per symbol per check", market.asked)
	}
	box.mu.Lock()
	tg, fs, desk := len(box.telegram), len(box.feishu), len(box.desktop)
	text, _ := box.telegram[0]["text"].(string)
	box.mu.Unlock()
	// The once alert fires on the first check only; the repeating one on both,
	// without Telegram. Telegram goes to the two numeric chat ids.
	if tg != 2 || fs != 3 || desk != 3 {
		t.Fatal(tg, fs, desk)
	}
	if !strings.Contains(text, "AAPL $210.50 (-2.40%) — AAPL breakout") {
		t.Fatal(text)
	}
	out.Reset()
	daemonManage(ctx, db, []string{"alert", "list"}, &out)
	if !strings.Contains(out.String(), "done") || !strings.Contains(out.String(), "repeating") || !strings.Contains(out.String(), "600519") {
		t.Fatal(out.String())
	}
	var id string
	db.QueryRow(`SELECT id FROM alerts WHERE symbol='600519'`).Scan(&id)
	if err := daemonManage(ctx, db, []string{"alert", "remove", id}, &out); err != nil {
		t.Fatal(err)
	}
	for _, bad := range [][]string{
		{"alert", "add", "AAPL", "above", "1"}, {"alert", "add", "AAPL", "price_above", "x"},
		{"alert", "add", "A B", "price_above", "1"}, {"alert", "add", "AAPL"}, {"alert", "remove", "nope"},
		{"alert", "add", "AAPL", "price_above", "1", "--message"}, {"bogus"},
	} {
		if err := daemonManage(ctx, db, bad, &out); err == nil {
			t.Fatal("accepted", bad)
		}
	}
}

func TestSchedulesRunOncePerMinute(t *testing.T) {
	d, market, box, db := daemonFixture(t)
	ctx := context.Background()
	var out bytes.Buffer
	if err := daemonManage(ctx, db, []string{"schedule", "add", "0 9 * * 1-5", "evening-brief", "--channels", "feishu", "--name", "close"}, &out); err != nil {
		t.Fatal(err)
	}
	if err := daemonManage(ctx, db, []string{"schedule", "add", "0 9 * * *", "custom:check the fund"}, &out); err != nil {
		t.Fatal(err)
	}
	db.Exec(`INSERT INTO schedules (id, cron_expr, command) VALUES ('legacy', '0 9 * * *', 'screen')`)
	market.quotes["000001.SS"] = quote{Price: 3210.5, ChangePct: 0.42, HasChange: true, Currency: "CNY", Source: "akshare"}
	shanghai, _ := time.LoadLocation("Asia/Shanghai")
	monday9 := time.Date(2026, 11, 2, 9, 0, 20, 0, shanghai)
	d.tick(ctx, monday9.UTC())
	d.tick(ctx, monday9.Add(30*time.Second))
	d.jobs.Wait()
	box.mu.Lock()
	var feishu []string
	for _, f := range box.feishu {
		b, _ := json.Marshal(f)
		feishu = append(feishu, string(b))
	}
	var telegram []string
	for _, m := range box.telegram {
		telegram = append(telegram, m["text"].(string))
	}
	box.mu.Unlock()
	all := strings.Join(append(feishu, telegram...), "\n")
	// evening-brief: Feishu only; custom: all channels (two Telegram chats);
	// the legacy row's default ["ios","telegram"]: Telegram.
	if len(feishu) != 2 || len(telegram) != 4 {
		t.Fatal("each due schedule runs once in its minute", len(feishu), len(telegram), all)
	}
	if !strings.Contains(all, "上证指数: ¥3210.50 +0.42%") || !strings.Contains(all, "深证成指: N/A") || !strings.Contains(all, "数据来源：akshare") {
		t.Fatal("the brief shows the change in percent, not the previous close", all)
	}
	if !strings.Contains(all, "check the fund") || !strings.Contains(all, "unknown scheduled command") {
		t.Fatal(all)
	}
	var last sql.NullString
	db.QueryRow(`SELECT last_run FROM schedules WHERE command='evening-brief'`).Scan(&last)
	if !last.Valid {
		t.Fatal("last_run is recorded")
	}
	d.tick(ctx, monday9.Add(time.Minute))
	d.jobs.Wait()
	if len(box.feishu) != 2 {
		t.Fatal("nothing is due at 09:01")
	}
	for _, bad := range [][]string{
		{"schedule", "add", "0 9 * *", "morning-brief"}, {"schedule", "add", "0 9 * * *", "rm -rf /"},
		{"schedule", "add", "0 9 * * *", "report A B"}, {"schedule", "add", "0 9 * * *", "custom:"},
		{"schedule", "add", "0 9 * * *", "morning", "--channels", "sms"},
	} {
		if err := daemonManage(ctx, db, bad, &out); err == nil {
			t.Fatal("accepted", bad)
		}
	}
	out.Reset()
	daemonManage(ctx, db, []string{"schedule", "list"}, &out)
	if !strings.Contains(out.String(), "evening-brief") || strings.Contains(out.String(), "Built in") {
		t.Fatal(out.String())
	}
}

func TestDefaultJobsAndWeekly(t *testing.T) {
	d, market, box, _ := daemonFixture(t)
	d.defaults = true
	market.weeks = map[string]float64{"SPY": 1.25, "QQQ": -0.5, "000300.SS": 2}
	shanghai, _ := time.LoadLocation("Asia/Shanghai")
	friday := time.Date(2026, 11, 6, 16, 30, 0, 0, shanghai)
	d.tick(context.Background(), friday)
	d.jobs.Wait()
	box.mu.Lock()
	defer box.mu.Unlock()
	if len(box.desktop) != 1 || !strings.Contains(box.desktop[0], "周报") {
		t.Fatal(box.desktop)
	}
	text := box.desktop[0]
	for _, want := range []string{"📈 S&P500: +1.25%", "📉 纳指: -0.50%", "— 罗素: N/A", "📈 沪深300: +2.00%"} {
		if !strings.Contains(text, want) {
			t.Fatal("missing", want, text)
		}
	}
}

func TestDaemonEnvAndProcess(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("FEISHU_WEBHOOK_URL", "")
	os.Unsetenv("FEISHU_WEBHOOK_URL")
	t.Setenv("TELEGRAM_BOT_TOKEN", "from-shell")
	os.WriteFile(filepath.Join(dir, ".env"), []byte("export FEISHU_WEBHOOK_URL=\"https://example.invalid/hook\"\nTELEGRAM_BOT_TOKEN=from-file\nOPENAI_API_KEY=nope\n"), 0o600)
	t.Setenv("OPENAI_API_KEY", "")
	loadDaemonEnv(dir)
	if os.Getenv("FEISHU_WEBHOOK_URL") != "https://example.invalid/hook" || os.Getenv("TELEGRAM_BOT_TOKEN") != "from-shell" || os.Getenv("OPENAI_API_KEY") != "" {
		t.Fatal("only the daemon's own settings, and the shell wins")
	}
	if runtime.GOOS != "windows" {
		os.Unsetenv("FEISHU_WEBHOOK_URL")
		os.Chmod(filepath.Join(dir, ".env"), 0o644)
		loadDaemonEnv(dir)
		if os.Getenv("FEISHU_WEBHOOK_URL") != "" {
			t.Fatal("a file others can read is ignored")
		}
	}
	pid := filepath.Join(dir, "x.pid")
	if _, alive := processAlive(pid); alive {
		t.Fatal("no file")
	}
	os.WriteFile(pid, []byte("999999999"), 0o600)
	if _, alive := processAlive(pid); alive && runtime.GOOS != "windows" {
		t.Fatal("a dead pid")
	}
	var out, errOut bytes.Buffer
	t.Setenv("ARIA_DAEMON_DIR", dir)
	if code := daemonCommand([]string{"status"}, &out, &errOut); code != 0 || !strings.Contains(out.String(), "not running") {
		t.Fatal(code, out.String(), errOut.String())
	}
	if code := daemonCommand(nil, &out, io.Discard); code != 2 {
		t.Fatal(code)
	}
}

func TestDaemonOnceAndQuoteForCallers(t *testing.T) {
	t.Setenv("ARIA_DAEMON_DIR", t.TempDir())
	t.Setenv("ARIA_DAEMON_TZ", "UTC")
	market := &fakeMarket{quotes: map[string]quote{"AAPL": {Price: 201.5, ChangePct: 1.25, HasChange: true, Currency: "USD", Source: "test"}}, weeks: map[string]float64{}}
	saved := daemonMarket
	defer func() { daemonMarket = saved }()
	daemonMarket = func() (marketData, func()) { return market, func() {} }
	run := func(args ...string) (int, string, string) {
		var out, errOut strings.Builder
		code := daemonCommand(args, &out, &errOut)
		return code, out.String(), errOut.String()
	}
	if code, out, _ := run("quote", "aapl"); code != 0 || out != "AAPL $201.50 (+1.25%)\n" {
		t.Fatalf("%d %q", code, out)
	}
	code, out, _ := run("quote", "AAPL", "--json")
	var q map[string]any
	if code != 0 || json.Unmarshal([]byte(out), &q) != nil || q["price"] != 201.5 || q["change_pct"] != 1.25 || q["currency"] != "USD" {
		t.Fatal(code, out)
	}
	if code, _, errOut := run("quote", "MSFT"); code != 1 || errOut == "" {
		t.Fatal("no quote is an error", code)
	}
	if code, _, _ := run("quote", "bad symbol!"); code != 2 {
		t.Fatal(code)
	}
	code, out, _ = run("once", "custom:hello", "--json")
	var brief map[string]string
	if code != 0 || json.Unmarshal([]byte(out), &brief) != nil || brief["body"] != "hello" || brief["title"] == "" {
		t.Fatal(code, out)
	}
	if code, out, _ := run("once", "evening-brief"); code != 0 || !strings.Contains(out, "收盘") {
		t.Fatal(code, out)
	}
	if code, _, _ := run("once", "rm", "-rf"); code != 1 {
		t.Fatal("unknown commands are refused", code)
	}
	if code, _, _ := run("once"); code != 2 {
		t.Fatal(code)
	}
}
