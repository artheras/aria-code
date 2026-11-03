package main

// aria-runtime daemon: price alerts and scheduled briefs, the part of
// aria_daemon.py that moves to the Go runtime. It reads and writes the same
// daemon.db (alerts, schedules) in the same folder (ARIA_DAEMON_DIR, else
// ~/.aria), so alerts and schedules made before carry over.
//
// Quotes and history come from the registered Arthera financial-service.
// Notifications go to Telegram (TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_IDS),
// a Feishu group webhook (FEISHU_WEBHOOK_URL) and the desktop. The Python
// daemon keeps its Telegram command bot and TradingView webhook queue for
// now; while this one runs, it leaves alerts and schedules to it.

import (
	"bufio"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	_ "modernc.org/sqlite"
)

var (
	telegramAPI = "https://api.telegram.org"
	// desktopNotify is a variable so tests do not pop notifications.
	desktopNotify = systemNotify
)

const daemonSchema = `
CREATE TABLE IF NOT EXISTS alerts (
    id TEXT PRIMARY KEY, symbol TEXT NOT NULL, condition TEXT NOT NULL, value REAL NOT NULL,
    message TEXT, notify_push INTEGER DEFAULT 1, notify_telegram INTEGER DEFAULT 1, notify_email TEXT,
    once INTEGER DEFAULT 1, active INTEGER DEFAULT 1, trigger_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')), triggered_at TEXT);
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY, name TEXT, cron_expr TEXT NOT NULL, command TEXT NOT NULL,
    symbols TEXT DEFAULT '[]', channels TEXT DEFAULT '["ios","telegram"]', language TEXT DEFAULT 'zh',
    user_id TEXT, enabled INTEGER DEFAULT 1, created_at TEXT DEFAULT (datetime('now')),
    last_run TEXT, next_run TEXT);`

var alertConditions = map[string]string{
	"price_above": "price above", "price_below": "price below",
	"pct_change_above": "day change above (%)", "pct_change_below": "day change below (%)",
}

func daemonDir() string {
	if dir := envKey("ARIA_DAEMON_DIR"); dir != "" {
		return expandUser(dir)
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".aria")
}

func openDaemonDB(dir string) (*sql.DB, error) {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	db, err := sql.Open("sqlite", "file:"+filepath.ToSlash(filepath.Join(dir, "daemon.db"))+"?_pragma=busy_timeout(10000)")
	if err != nil {
		return nil, err
	}
	if _, err := db.Exec(daemonSchema); err != nil {
		db.Close()
		return nil, err
	}
	return db, nil
}

// loadDaemonEnv fills notification settings from <dir>/.env, as the Python
// daemon did, for these names only and only from a file no one else can read.
func loadDaemonEnv(dir string) {
	path := filepath.Join(dir, ".env")
	info, err := os.Stat(path)
	if err != nil {
		return
	}
	if runtime.GOOS != "windows" && info.Mode().Perm()&0o077 != 0 {
		log.Printf("ignoring %s: other users can read it (chmod 600 to use it)", path)
		return
	}
	allowed := map[string]bool{"TELEGRAM_BOT_TOKEN": true, "TELEGRAM_ALLOWED_IDS": true, "FEISHU_WEBHOOK_URL": true,
		"ARIA_DAEMON_TZ": true, "ARIA_DAEMON_DEFAULT_JOBS": true}
	f, err := os.Open(path)
	if err != nil {
		return
	}
	defer f.Close()
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(scanner.Text()), "export "))
		key, value, ok := strings.Cut(line, "=")
		key = strings.TrimSpace(key)
		if !ok || !allowed[key] || os.Getenv(key) != "" {
			continue
		}
		os.Setenv(key, strings.Trim(strings.TrimSpace(value), `"'`))
	}
}

// ── Market data through the financial-service ───────────────────────────────

type quote struct {
	Price, ChangePct float64
	HasChange        bool
	Currency, Source string
}

// financeClient keeps one financial-service process for the daemon's life.
type financeClient struct {
	mu     sync.Mutex
	client *mcpClient
}

func (f *financeClient) call(ctx context.Context, tool string, args map[string]any) (map[string]any, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.client == nil || !f.client.alive() {
		state, err := stateRoot()
		if err != nil {
			return nil, err
		}
		configs, err := loadMCPConfig(state)
		if err != nil {
			return nil, err
		}
		var cfg *mcpServerConfig
		for i := range configs {
			if configs[i].Name == financeServer && (configs[i].Enabled == nil || *configs[i].Enabled) {
				cfg = &configs[i]
			}
		}
		if cfg == nil {
			return nil, errors.New("the Arthera financial-service is not registered (aria: /finance setup <path>)")
		}
		if f.client, err = startMCP(ctx, *cfg); err != nil {
			return nil, fmt.Errorf("financial-service: %w", err)
		}
	}
	callCtx, cancel := context.WithTimeout(ctx, max(f.client.timeout(), 60*time.Second))
	defer cancel()
	raw, err := f.client.request(callCtx, "tools/call", map[string]any{"name": tool, "arguments": args})
	if err != nil {
		return nil, fmt.Errorf("financial-service %s: %w", tool, err)
	}
	var result struct {
		Content []struct {
			Text string `json:"text"`
		} `json:"content"`
	}
	var payload map[string]any
	if json.Unmarshal(raw, &result) != nil || len(result.Content) == 0 || json.Unmarshal([]byte(result.Content[0].Text), &payload) != nil {
		return nil, fmt.Errorf("financial-service %s returned no result", tool)
	}
	if ok, _ := payload["success"].(bool); !ok {
		msg, _ := payload["error"].(string)
		return nil, fmt.Errorf("financial-service %s: %s", tool, capRunes(msg, 200))
	}
	return payload, nil
}

func (f *financeClient) close() {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.client != nil {
		f.client.close()
		f.client = nil
	}
}

type marketData interface {
	quote(ctx context.Context, symbol string) (quote, error)
	weekReturn(ctx context.Context, symbol string) (float64, error)
}

type serviceMarket struct{ finance *financeClient }

func (m serviceMarket) quote(ctx context.Context, symbol string) (quote, error) {
	payload, err := m.finance.call(ctx, "financial_quote", map[string]any{"symbol": symbol})
	if err != nil {
		return quote{}, err
	}
	data, _ := payload["data"].(map[string]any)
	q := quote{Source: fmt.Sprint(payload["source"])}
	q.Price, _ = chartNumber(data["price"])
	if q.Price <= 0 {
		return quote{}, fmt.Errorf("no price for %s", symbol)
	}
	if v, ok := chartNumber(data["change_pct"]); ok {
		q.ChangePct, q.HasChange = v, true
	}
	q.Currency, _ = data["currency"].(string)
	return q, nil
}

func (m serviceMarket) weekReturn(ctx context.Context, symbol string) (float64, error) {
	payload, err := m.finance.call(ctx, "financial_market_history", map[string]any{"symbol": symbol, "days": 10})
	if err != nil {
		return 0, err
	}
	bars, err := parseBars(payload)
	if err != nil {
		return 0, err
	}
	week := bars[max(0, len(bars)-5):]
	return pctChange(week[0].Close, week[len(week)-1].Close), nil
}

// ── Notifications ───────────────────────────────────────────────────────────

type notifier struct{ client *http.Client }

func (n notifier) telegram(ctx context.Context, text string) {
	token, ids := envKey("TELEGRAM_BOT_TOKEN"), envKey("TELEGRAM_ALLOWED_IDS")
	if token == "" || ids == "" {
		return
	}
	for _, id := range strings.Split(ids, ",") {
		chat, err := strconv.ParseInt(strings.TrimSpace(id), 10, 64)
		if err != nil {
			continue
		}
		body := jsonBody(map[string]any{"chat_id": chat, "text": capRunes(text, 4000)})
		req, _ := http.NewRequestWithContext(ctx, http.MethodPost, telegramAPI+"/bot"+token+"/sendMessage", body)
		req.Header.Set("Content-Type", "application/json")
		if resp, err := n.client.Do(req); err != nil {
			log.Printf("telegram: %v", strings.ReplaceAll(err.Error(), token, "<token>"))
		} else {
			io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<16))
			resp.Body.Close()
			if resp.StatusCode >= 400 {
				log.Printf("telegram: HTTP %d", resp.StatusCode)
			}
		}
	}
}

func (n notifier) feishu(ctx context.Context, title, body string) {
	hook := envKey("FEISHU_WEBHOOK_URL")
	if hook == "" {
		return
	}
	color := "blue"
	switch {
	case strings.Contains(title, "预警") || strings.Contains(title, "Alert"):
		color = "red"
	case strings.Contains(title, "报") || strings.Contains(title, "Brief"):
		color = "green"
	}
	card := map[string]any{"msg_type": "interactive", "card": map[string]any{
		"header": map[string]any{"title": map[string]any{"tag": "plain_text", "content": title}, "template": color},
		"elements": []any{
			map[string]any{"tag": "div", "text": map[string]any{"tag": "lark_md", "content": capRunes(body, 2000)}},
			map[string]any{"tag": "hr"},
			map[string]any{"tag": "note", "elements": []any{map[string]any{"tag": "plain_text", "content": "Aria Daemon · " + time.Now().Format("2006-01-02 15:04")}}},
		}}}
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, hook, jsonBody(card))
	req.Header.Set("Content-Type", "application/json")
	if resp, err := n.client.Do(req); err != nil {
		log.Printf("feishu: request failed")
	} else {
		io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<16))
		resp.Body.Close()
		if resp.StatusCode >= 400 {
			log.Printf("feishu: HTTP %d", resp.StatusCode)
		}
	}
}

// send delivers to the named channels; telegram and feishu only when configured.
func (n notifier) send(ctx context.Context, title, body string, channels map[string]bool) {
	if channels["telegram"] {
		n.telegram(ctx, title+"\n"+body)
	}
	if channels["feishu"] {
		n.feishu(ctx, title, body)
	}
	if channels["desktop"] {
		desktopNotify(title, body)
	}
}

func systemNotify(title, body string) {
	switch runtime.GOOS {
	case "darwin":
		quote := func(s string) string { return strings.NewReplacer(`\`, `\\`, `"`, `\"`).Replace(capRunes(s, 200)) }
		exec.Command("osascript", "-e", `display notification "`+quote(body)+`" with title "`+quote(title)+`" subtitle "Aria Code"`).Start()
	case "linux":
		if path, err := exec.LookPath("notify-send"); err == nil {
			exec.Command(path, "--app-name=Aria", title, capRunes(body, 200)).Start()
		}
	}
}

// ── Alerts ──────────────────────────────────────────────────────────────────

type alertRow struct {
	ID, Symbol, Condition, Message string
	Value                          float64
	NotifyTelegram, Once           bool
}

func activeAlerts(ctx context.Context, db *sql.DB) ([]alertRow, error) {
	rows, err := db.QueryContext(ctx, `SELECT id, symbol, condition, value, COALESCE(message,''), COALESCE(notify_telegram,1), COALESCE(once,1) FROM alerts WHERE active=1`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []alertRow
	for rows.Next() {
		var a alertRow
		var tg, once int
		if err := rows.Scan(&a.ID, &a.Symbol, &a.Condition, &a.Value, &a.Message, &tg, &once); err != nil {
			return nil, err
		}
		a.NotifyTelegram, a.Once = tg != 0, once != 0
		out = append(out, a)
	}
	return out, rows.Err()
}

// alertFires is _check_alert's condition; a day-change alert needs a change.
func alertFires(a alertRow, q quote) bool {
	switch a.Condition {
	case "price_above":
		return q.Price > a.Value
	case "price_below":
		return q.Price < a.Value
	case "pct_change_above":
		return q.HasChange && q.ChangePct > a.Value
	case "pct_change_below":
		return q.HasChange && q.ChangePct < a.Value
	}
	return false
}

// quotePrice is a price with two decimals, or significant digits below 1.
func quotePrice(v float64) string {
	if math.Abs(v) >= 1 {
		return strconv.FormatFloat(v, 'f', 2, 64)
	}
	return strconv.FormatFloat(v, 'g', 4, 64)
}

func currencyMark(q quote) string {
	switch strings.ToUpper(q.Currency) {
	case "CNY", "RMB":
		return "¥"
	case "USD":
		return "$"
	case "HKD":
		return "HK$"
	}
	return ""
}

type daemon struct {
	db       *sql.DB
	market   marketData
	notify   notifier
	location *time.Location
	defaults bool
	ran      map[string]string // schedule id -> minute last run, so a minute runs once
	mu       sync.Mutex
	jobs     sync.WaitGroup
}

func (d *daemon) checkAlerts(ctx context.Context) {
	alerts, err := activeAlerts(ctx, d.db)
	if err != nil {
		log.Printf("alerts: %v", err)
		return
	}
	bySymbol := map[string][]alertRow{}
	for _, a := range alerts {
		bySymbol[a.Symbol] = append(bySymbol[a.Symbol], a)
	}
	symbols := make([]string, 0, len(bySymbol))
	for s := range bySymbol {
		symbols = append(symbols, s)
	}
	sort.Strings(symbols)
	for _, symbol := range symbols {
		q, err := d.market.quote(ctx, symbol)
		if err != nil {
			log.Printf("alerts: %s: %v", symbol, err)
			continue
		}
		for _, a := range bySymbol[symbol] {
			if alertFires(a, q) {
				d.fire(ctx, a, q)
			}
		}
	}
}

func (d *daemon) fire(ctx context.Context, a alertRow, q quote) {
	update := `UPDATE alerts SET trigger_count=trigger_count+1, triggered_at=datetime('now')`
	if a.Once {
		update += `, active=0`
	}
	if _, err := d.db.ExecContext(ctx, update+` WHERE id=?`, a.ID); err != nil {
		log.Printf("alert %s: %v", a.ID, err)
		return
	}
	change := ""
	if q.HasChange {
		change = fmt.Sprintf(" (%+.2f%%)", q.ChangePct)
	}
	message := a.Message
	if message == "" {
		message = fmt.Sprintf("%s %s %g", a.Symbol, alertConditions[a.Condition], a.Value)
	}
	body := fmt.Sprintf("%s %s%s%s — %s", a.Symbol, currencyMark(q), quotePrice(q.Price), change, message)
	log.Printf("alert %s fired: %s", a.ID, body)
	d.notify.send(ctx, "🔔 ARIA 价格预警", body, map[string]bool{"telegram": a.NotifyTelegram, "feishu": true, "desktop": true})
}

// ── Schedules ───────────────────────────────────────────────────────────────

type scheduleRow struct {
	ID, Name, Cron, Command string
	Channels                map[string]bool
	Default                 bool
}

var defaultSchedules = []scheduleRow{
	{ID: "default_morning_brief", Name: "A股开盘前晨报", Cron: "55 8 * * 1-5", Command: "morning-brief", Default: true},
	{ID: "default_us_brief", Name: "美股开盘前简报", Cron: "0 21 * * 1-5", Command: "morning-brief", Default: true},
	{ID: "default_evening_brief", Name: "A股收盘晚报", Cron: "35 15 * * 1-5", Command: "evening-brief", Default: true},
	{ID: "default_weekly_recap", Name: "周报", Cron: "30 16 * * 5", Command: "weekly-recap", Default: true},
}

func channelSet(raw string) map[string]bool {
	var names []string
	if json.Unmarshal([]byte(raw), &names) != nil {
		names = []string{"ios", "telegram"}
	}
	out := map[string]bool{}
	for _, n := range names {
		out[strings.ToLower(n)] = true
	}
	return out
}

func (d *daemon) schedules(ctx context.Context) ([]scheduleRow, error) {
	rows, err := d.db.QueryContext(ctx, `SELECT id, COALESCE(name,''), cron_expr, command, COALESCE(channels,'') FROM schedules WHERE enabled=1`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []scheduleRow
	for rows.Next() {
		var s scheduleRow
		var channels string
		if err := rows.Scan(&s.ID, &s.Name, &s.Cron, &s.Command, &channels); err != nil {
			return nil, err
		}
		s.Channels = channelSet(channels)
		out = append(out, s)
	}
	if d.defaults {
		for _, s := range defaultSchedules {
			s.Channels = map[string]bool{"telegram": true, "feishu": true, "desktop": true}
			out = append(out, s)
		}
	}
	return out, rows.Err()
}

// tick runs every schedule due in the minute of now, each at most once.
func (d *daemon) tick(ctx context.Context, now time.Time) {
	now = now.In(d.location)
	minute := now.Format("2006-01-02T15:04")
	list, err := d.schedules(ctx)
	if err != nil {
		log.Printf("schedules: %v", err)
		return
	}
	for _, s := range list {
		spec, err := parseCron(s.Cron)
		if err != nil {
			log.Printf("schedule %s: %v", s.ID, err)
			continue
		}
		if !spec.matches(now) {
			continue
		}
		d.mu.Lock()
		already := d.ran[s.ID] == minute
		d.ran[s.ID] = minute
		d.mu.Unlock()
		if already {
			continue
		}
		d.jobs.Add(1)
		go func(s scheduleRow) {
			defer d.jobs.Done()
			d.runSchedule(ctx, s)
		}(s)
	}
}

func (d *daemon) runSchedule(ctx context.Context, s scheduleRow) {
	ctx, cancel := context.WithTimeout(ctx, 10*time.Minute)
	defer cancel()
	log.Printf("schedule %s: %s", s.ID, s.Command)
	title, result, err := d.runCommand(ctx, s.Command)
	if err != nil {
		title, result = "⏰ 定时任务失败："+s.Command, err.Error()
	}
	d.notify.send(ctx, title, result, s.Channels)
	if !s.Default {
		if _, err := d.db.ExecContext(ctx, `UPDATE schedules SET last_run=datetime('now') WHERE id=?`, s.ID); err != nil {
			log.Printf("schedule %s: %v", s.ID, err)
		}
	}
}

var scheduleCommands = "morning-brief, evening-brief, weekly-recap, report <symbol>, custom:<text>"

func (d *daemon) runCommand(ctx context.Context, command string) (string, string, error) {
	command = strings.TrimSpace(command)
	switch {
	case command == "morning-brief" || command == "morning":
		return "📊 Aria 晨报", d.brief(ctx, "🌅 Aria 晨报", []briefSection{
			{"🇨🇳 A股指数", cnIndices},
			{"🇺🇸 美股指数", [][2]string{{"S&P 500", "^GSPC"}, {"纳斯达克", "^IXIC"}, {"道琼斯", "^DJI"}, {"VIX恐慌", "^VIX"}}},
			{"₿ 加密 & 大宗", [][2]string{{"BTC", "BTC-USD"}, {"ETH", "ETH-USD"}, {"黄金", "GC=F"}}},
			{"💱 汇率", [][2]string{{"USD/CNY", "CNY=X"}, {"USD/JPY", "JPY=X"}}},
		}), nil
	case command == "evening-brief" || command == "evening":
		return "🌆 Aria 收盘简报", d.brief(ctx, "🌆 Aria 收盘简报", []briefSection{{"今日收盘", cnIndices}}), nil
	case command == "weekly-recap" || command == "weekly":
		return "📅 Aria 周报", d.weekly(ctx), nil
	case strings.HasPrefix(command, "report "):
		symbol := strings.ToUpper(strings.TrimSpace(strings.TrimPrefix(command, "report ")))
		result := mcpReportGenerate(ctx, map[string]any{"symbol": symbol})
		if result["success"] != true {
			return "", "", fmt.Errorf("report %s: %v", symbol, result["error"])
		}
		return "📄 Aria 研报 " + symbol, fmt.Sprintf("%s 研报已生成：%v", symbol, result["path"]), nil
	case strings.HasPrefix(command, "custom:"):
		return "⏰ Aria 定时提醒", strings.TrimSpace(strings.TrimPrefix(command, "custom:")), nil
	}
	return "", "", fmt.Errorf("unknown scheduled command %q (supported: %s)", command, scheduleCommands)
}

type briefSection struct {
	title string
	items [][2]string
}

var cnIndices = [][2]string{{"上证指数", "000001.SS"}, {"深证成指", "399001.SZ"}, {"创业板", "399006.SZ"}, {"沪深300", "000300.SS"}}

func (d *daemon) brief(ctx context.Context, title string, sections []briefSection) string {
	lines := []string{title + "  " + time.Now().In(d.location).Format("2006-01-02 15:04")}
	sources := map[string]bool{}
	for _, section := range sections {
		lines = append(lines, "", section.title)
		for _, item := range section.items {
			q, err := d.market.quote(ctx, item[1])
			if err != nil {
				lines = append(lines, "  — "+item[0]+": N/A")
				continue
			}
			sources[q.Source] = true
			line := fmt.Sprintf("  %s: %s%s", item[0], currencyMark(q), quotePrice(q.Price))
			if q.HasChange {
				arrow := "📈"
				if q.ChangePct < 0 {
					arrow = "📉"
				}
				line = fmt.Sprintf("  %s %s: %s%s %+.2f%%", arrow, item[0], currencyMark(q), quotePrice(q.Price), q.ChangePct)
			}
			lines = append(lines, line)
		}
	}
	return strings.Join(append(lines, "", "数据来源："+joinSources(sources)+"，仅供参考，可能延迟"), "\n")
}

func (d *daemon) weekly(ctx context.Context) string {
	year, week := time.Now().In(d.location).ISOWeek()
	lines := []string{fmt.Sprintf("📅 Aria 周报  %d 第%d周", year, week)}
	for _, section := range []briefSection{
		{"本周美股 ETF 表现", [][2]string{{"S&P500", "SPY"}, {"纳指", "QQQ"}, {"罗素", "IWM"}, {"黄金", "GLD"}}},
		{"本周A股指数", [][2]string{{"上证", "000001.SS"}, {"沪深300", "000300.SS"}, {"创业板", "399006.SZ"}}},
	} {
		lines = append(lines, "", section.title)
		for _, item := range section.items {
			r, err := d.market.weekReturn(ctx, item[1])
			if err != nil || math.IsNaN(r) {
				lines = append(lines, "  — "+item[0]+": N/A")
				continue
			}
			arrow := "📈"
			if r < 0 {
				arrow = "📉"
			}
			lines = append(lines, fmt.Sprintf("  %s %s: %+.2f%%", arrow, item[0], r))
		}
	}
	return strings.Join(append(lines, "", "最近五个交易日；数据来自 financial-service，仅供参考"), "\n")
}

func joinSources(sources map[string]bool) string {
	var names []string
	for s := range sources {
		if s != "" && s != "<nil>" {
			names = append(names, s)
		}
	}
	sort.Strings(names)
	if len(names) == 0 {
		return "financial-service"
	}
	return strings.Join(names, "、")
}

// ── Process ─────────────────────────────────────────────────────────────────

func processAlive(pidFile string) (int, bool) {
	data, err := os.ReadFile(pidFile)
	if err != nil {
		return 0, false
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(data)))
	if err != nil || pid <= 0 || pid == os.Getpid() {
		return pid, false
	}
	proc, err := os.FindProcess(pid)
	if err != nil {
		return pid, false
	}
	if runtime.GOOS == "windows" {
		return pid, true
	}
	return pid, proc.Signal(syscall.Signal(0)) == nil
}

func (d *daemon) run(ctx context.Context, alertEvery time.Duration) {
	alerts := time.NewTicker(alertEvery)
	defer alerts.Stop()
	minute := time.NewTimer(time.Until(time.Now().Truncate(time.Minute).Add(time.Minute)))
	defer minute.Stop()
	d.checkAlerts(ctx)
	d.tick(ctx, time.Now())
	for {
		select {
		case <-ctx.Done():
			d.jobs.Wait()
			return
		case <-alerts.C:
			d.checkAlerts(ctx)
		case now := <-minute.C:
			d.tick(ctx, now)
			minute.Reset(time.Until(time.Now().Truncate(time.Minute).Add(time.Minute)))
		}
	}
}

func newDaemon(db *sql.DB, market marketData) (*daemon, error) {
	zone := envKey("ARIA_DAEMON_TZ")
	if zone == "" {
		zone = "Asia/Shanghai"
	}
	location, err := time.LoadLocation(zone)
	if err != nil {
		return nil, fmt.Errorf("ARIA_DAEMON_TZ: %w", err)
	}
	return &daemon{db: db, market: market, notify: notifier{client: &http.Client{Timeout: 15 * time.Second}},
		location: location, defaults: envKey("ARIA_DAEMON_DEFAULT_JOBS") != "0", ran: map[string]string{}}, nil
}

func daemonRun(errOut io.Writer) int {
	dir := daemonDir()
	// A Python daemon (aria_daemon.py) running beside this one sees
	// daemon-go.pid and leaves alerts and schedules to it, keeping its
	// Telegram bot, webhook queue and portfolio watchdog.
	goPid := filepath.Join(dir, "daemon-go.pid")
	if pid, alive := processAlive(goPid); alive {
		fmt.Fprintf(errOut, "aria-runtime daemon is already running (pid %d)\n", pid)
		return 1
	}
	loadDaemonEnv(dir)
	os.MkdirAll(filepath.Join(dir, "logs"), 0o700)
	if logFile, err := os.OpenFile(filepath.Join(dir, "logs", "daemon-go.log"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600); err == nil {
		log.SetOutput(io.MultiWriter(errOut, logFile))
		defer logFile.Close()
	}
	db, err := openDaemonDB(dir)
	if err != nil {
		fmt.Fprintln(errOut, err)
		return 1
	}
	defer db.Close()
	finance := &financeClient{}
	defer finance.close()
	d, err := newDaemon(db, serviceMarket{finance})
	if err != nil {
		fmt.Fprintln(errOut, err)
		return 1
	}
	if err := os.WriteFile(goPid, []byte(strconv.Itoa(os.Getpid())), 0o600); err != nil {
		fmt.Fprintln(errOut, err)
		return 1
	}
	defer os.Remove(goPid)
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	log.Printf("aria-runtime daemon %s: alerts every 30s, schedules in %s, folder %s", version, d.location, dir)
	d.run(ctx, 30*time.Second)
	log.Printf("aria-runtime daemon stopped")
	return 0
}
