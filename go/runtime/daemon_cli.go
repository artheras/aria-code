package main

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"html"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"text/tabwriter"
)

const daemonUsage = `usage: aria-runtime daemon <command>

  run                                   check alerts every 30 s and run schedules (foreground)
  status                                whether a daemon runs, and what it watches
  alert add SYMBOL CONDITION VALUE [--message TEXT] [--repeat] [--no-telegram]
                                        CONDITION: price_above, price_below,
                                        pct_change_above, pct_change_below
  alert list | alert remove ID
  schedule add "CRON" COMMAND [--name NAME] [--channels telegram,feishu,desktop]
                                        COMMAND: morning-brief, evening-brief, weekly-recap,
                                        "report SYMBOL" or "custom:TEXT"
  schedule list | schedule remove ID
  install | uninstall                   start at login (macOS LaunchAgent, Linux systemd user unit)

Folder: ARIA_DAEMON_DIR, else ~/.aria (daemon.db, logs/). Time zone: ARIA_DAEMON_TZ
(default Asia/Shanghai). ARIA_DAEMON_DEFAULT_JOBS=0 turns off the four built-in briefs.`

func newDaemonID() string {
	b := make([]byte, 6)
	rand.Read(b)
	return hex.EncodeToString(b)
}

// takeFlag removes --name VALUE (or a bare --flag) from args.
func takeFlag(args []string, name string, hasValue bool) (string, bool, []string, error) {
	for i, a := range args {
		if a != name {
			continue
		}
		rest := append(append([]string{}, args[:i]...), args[i+1:]...)
		if !hasValue {
			return "", true, rest, nil
		}
		if i+1 >= len(args) {
			return "", false, nil, fmt.Errorf("%s needs a value", name)
		}
		return args[i+1], true, append(append([]string{}, args[:i]...), args[i+2:]...), nil
	}
	return "", false, args, nil
}

func daemonCommand(args []string, out, errOut io.Writer) int {
	if len(args) == 0 || args[0] == "-h" || args[0] == "--help" || args[0] == "help" {
		fmt.Fprintln(out, daemonUsage)
		if len(args) == 0 {
			return 2
		}
		return 0
	}
	if args[0] == "run" {
		return daemonRun(errOut)
	}
	if args[0] == "install" || args[0] == "uninstall" {
		return daemonService(args[0], out, errOut)
	}
	db, err := openDaemonDB(daemonDir())
	if err != nil {
		fmt.Fprintln(errOut, err)
		return 1
	}
	defer db.Close()
	if err := daemonManage(context.Background(), db, args, out); err != nil {
		fmt.Fprintln(errOut, err)
		if strings.HasPrefix(err.Error(), "usage") {
			return 2
		}
		return 1
	}
	return 0
}

func daemonManage(ctx context.Context, db *sql.DB, args []string, out io.Writer) error {
	sub := strings.Join(args[:min(2, len(args))], " ")
	switch {
	case args[0] == "status":
		return daemonStatus(ctx, db, out)
	case sub == "alert add":
		rest := args[2:]
		message, _, rest, err := takeFlag(rest, "--message", true)
		if err != nil {
			return err
		}
		_, repeat, rest, _ := takeFlag(rest, "--repeat", false)
		_, noTelegram, rest, _ := takeFlag(rest, "--no-telegram", false)
		if len(rest) != 3 {
			return fmt.Errorf("usage: aria-runtime daemon alert add SYMBOL CONDITION VALUE [--message TEXT] [--repeat] [--no-telegram]")
		}
		symbol := strings.ToUpper(strings.TrimSpace(rest[0]))
		if !chartSymbol.MatchString(symbol) {
			return fmt.Errorf("invalid symbol %q", rest[0])
		}
		if _, ok := alertConditions[rest[1]]; !ok {
			return fmt.Errorf("condition must be price_above, price_below, pct_change_above or pct_change_below")
		}
		value, err := strconv.ParseFloat(rest[2], 64)
		if err != nil || value != value {
			return fmt.Errorf("value must be a number")
		}
		id := newDaemonID()
		once, telegram := 1, 1
		if repeat {
			once = 0
		}
		if noTelegram {
			telegram = 0
		}
		if _, err := db.ExecContext(ctx, `INSERT INTO alerts (id, symbol, condition, value, message, notify_telegram, once) VALUES (?,?,?,?,?,?,?)`,
			id, symbol, rest[1], value, capRunes(message, 200), telegram, once); err != nil {
			return err
		}
		fmt.Fprintf(out, "alert %s: %s %s %g\n", id, symbol, alertConditions[rest[1]], value)
	case sub == "alert list":
		rows, err := db.QueryContext(ctx, `SELECT id, symbol, condition, value, COALESCE(message,''), active, once, trigger_count, COALESCE(triggered_at,'') FROM alerts ORDER BY created_at`)
		if err != nil {
			return err
		}
		defer rows.Close()
		w := tabwriter.NewWriter(out, 0, 2, 2, ' ', 0)
		fmt.Fprintln(w, "ID\tSYMBOL\tCONDITION\tVALUE\tSTATE\tFIRED\tMESSAGE")
		for rows.Next() {
			var id, symbol, cond, msg, last string
			var value float64
			var active, once, count int
			if err := rows.Scan(&id, &symbol, &cond, &value, &msg, &active, &once, &count, &last); err != nil {
				return err
			}
			state := "active"
			if active == 0 {
				state = "done"
			} else if once == 0 {
				state = "repeating"
			}
			fired := strconv.Itoa(count)
			if last != "" {
				fired += " (last " + last + ")"
			}
			fmt.Fprintf(w, "%s\t%s\t%s\t%g\t%s\t%s\t%s\n", id, symbol, cond, value, state, fired, msg)
		}
		w.Flush()
		return rows.Err()
	case sub == "alert remove":
		return removeRow(ctx, db, "alerts", args[2:], out)
	case sub == "schedule add":
		rest := args[2:]
		name, _, rest, err := takeFlag(rest, "--name", true)
		if err != nil {
			return err
		}
		channels, hasChannels, rest, err := takeFlag(rest, "--channels", true)
		if err != nil {
			return err
		}
		if len(rest) != 2 {
			return fmt.Errorf("usage: aria-runtime daemon schedule add \"CRON\" COMMAND [--name NAME] [--channels telegram,feishu,desktop]")
		}
		if _, err := parseCron(rest[0]); err != nil {
			return err
		}
		command := strings.TrimSpace(rest[1])
		if !validScheduleCommand(command) {
			return fmt.Errorf("command must be one of: %s", scheduleCommands)
		}
		list := []string{"telegram", "feishu", "desktop"}
		if hasChannels {
			list = nil
			for _, c := range strings.Split(channels, ",") {
				c = strings.ToLower(strings.TrimSpace(c))
				if c != "telegram" && c != "feishu" && c != "desktop" {
					return fmt.Errorf("channels are telegram, feishu and desktop")
				}
				list = append(list, c)
			}
		}
		encoded, _ := json.Marshal(list)
		id := newDaemonID()
		if _, err := db.ExecContext(ctx, `INSERT INTO schedules (id, name, cron_expr, command, channels) VALUES (?,?,?,?,?)`,
			id, capRunes(name, 80), rest[0], command, string(encoded)); err != nil {
			return err
		}
		fmt.Fprintf(out, "schedule %s: %q runs %s\n", id, rest[0], command)
	case sub == "schedule list":
		rows, err := db.QueryContext(ctx, `SELECT id, COALESCE(name,''), cron_expr, command, COALESCE(channels,''), enabled, COALESCE(last_run,'') FROM schedules ORDER BY created_at`)
		if err != nil {
			return err
		}
		defer rows.Close()
		w := tabwriter.NewWriter(out, 0, 2, 2, ' ', 0)
		fmt.Fprintln(w, "ID\tCRON\tCOMMAND\tCHANNELS\tSTATE\tLAST RUN\tNAME")
		for rows.Next() {
			var id, name, cron, command, channels, last string
			var enabled int
			if err := rows.Scan(&id, &name, &cron, &command, &channels, &enabled, &last); err != nil {
				return err
			}
			state := "on"
			if enabled == 0 {
				state = "off"
			}
			fmt.Fprintf(w, "%s\t%s\t%s\t%s\t%s\t%s\t%s\n", id, cron, command, channels, state, last, name)
		}
		w.Flush()
		if envKey("ARIA_DAEMON_DEFAULT_JOBS") != "0" {
			fmt.Fprintln(out, "\nBuilt in (ARIA_DAEMON_DEFAULT_JOBS=0 turns them off):")
			for _, s := range defaultSchedules {
				fmt.Fprintf(out, "  %-14s %-14s %s\n", s.Cron, s.Command, s.Name)
			}
		}
		return rows.Err()
	case sub == "schedule remove":
		return removeRow(ctx, db, "schedules", args[2:], out)
	default:
		return fmt.Errorf("usage: unknown daemon command %q (aria-runtime daemon help)", strings.Join(args, " "))
	}
	return nil
}

func validScheduleCommand(command string) bool {
	switch command {
	case "morning-brief", "morning", "evening-brief", "evening", "weekly-recap", "weekly":
		return true
	}
	if symbol, ok := strings.CutPrefix(command, "report "); ok {
		return chartSymbol.MatchString(strings.TrimSpace(symbol))
	}
	return strings.HasPrefix(command, "custom:") && len(command) > len("custom:")
}

func removeRow(ctx context.Context, db *sql.DB, table string, args []string, out io.Writer) error {
	if len(args) != 1 {
		return fmt.Errorf("usage: aria-runtime daemon %s remove ID", strings.TrimSuffix(table, "s"))
	}
	res, err := db.ExecContext(ctx, `DELETE FROM `+table+` WHERE id=?`, args[0])
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return fmt.Errorf("no %s with id %s", strings.TrimSuffix(table, "s"), args[0])
	}
	fmt.Fprintf(out, "removed %s\n", args[0])
	return nil
}

func daemonStatus(ctx context.Context, db *sql.DB, out io.Writer) error {
	dir := daemonDir()
	if pid, alive := processAlive(filepath.Join(dir, "daemon-go.pid")); alive {
		fmt.Fprintf(out, "aria-runtime daemon: running (pid %d)\n", pid)
	} else {
		fmt.Fprintln(out, "aria-runtime daemon: not running (aria-runtime daemon run, or install)")
	}
	if pid, alive := processAlive(filepath.Join(dir, "daemon.pid")); alive {
		fmt.Fprintf(out, "Python daemon: running (pid %d); it leaves alerts and schedules to aria-runtime daemon while that runs\n", pid)
	}
	var alerts, schedules int
	db.QueryRowContext(ctx, `SELECT COUNT(*) FROM alerts WHERE active=1`).Scan(&alerts)
	db.QueryRowContext(ctx, `SELECT COUNT(*) FROM schedules WHERE enabled=1`).Scan(&schedules)
	fmt.Fprintf(out, "active alerts: %d · schedules: %d (+%d built in)\n", alerts, schedules, map[bool]int{true: len(defaultSchedules)}[envKey("ARIA_DAEMON_DEFAULT_JOBS") != "0"])
	var channels []string
	if envKey("TELEGRAM_BOT_TOKEN") != "" && envKey("TELEGRAM_ALLOWED_IDS") != "" {
		channels = append(channels, "telegram")
	}
	if envKey("FEISHU_WEBHOOK_URL") != "" {
		channels = append(channels, "feishu")
	}
	channels = append(channels, "desktop")
	fmt.Fprintf(out, "notifications in this shell: %s · folder: %s\n", strings.Join(channels, ", "), dir)
	return nil
}

// daemonService installs or removes the login service that runs `daemon run`.
func daemonService(action string, out, errOut io.Writer) int {
	exe, err := os.Executable()
	if err == nil {
		exe, err = filepath.EvalSymlinks(exe)
	}
	if err != nil {
		fmt.Fprintln(errOut, err)
		return 1
	}
	home, _ := os.UserHomeDir()
	dir := daemonDir()
	switch runtime.GOOS {
	case "darwin":
		plist := filepath.Join(home, "Library", "LaunchAgents", "com.aria.runtime-daemon.plist")
		exec.Command("launchctl", "unload", plist).Run()
		if action == "uninstall" {
			if err := os.Remove(plist); err != nil && !os.IsNotExist(err) {
				fmt.Fprintln(errOut, err)
				return 1
			}
			fmt.Fprintln(out, "removed the Aria daemon LaunchAgent")
			return 0
		}
		os.MkdirAll(filepath.Dir(plist), 0o755)
		os.MkdirAll(filepath.Join(dir, "logs"), 0o700)
		x := html.EscapeString
		body := `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.aria.runtime-daemon</string>
  <key>ProgramArguments</key><array><string>` + x(exe) + `</string><string>daemon</string><string>run</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>` + x(filepath.Join(dir, "logs", "daemon-go.out")) + `</string>
  <key>StandardErrorPath</key><string>` + x(filepath.Join(dir, "logs", "daemon-go.err")) + `</string>
  <key>EnvironmentVariables</key><dict><key>ARIA_DAEMON_DIR</key><string>` + x(dir) + `</string></dict>
</dict></plist>
`
		if err := os.WriteFile(plist, []byte(body), 0o644); err != nil {
			fmt.Fprintln(errOut, err)
			return 1
		}
		if output, err := exec.Command("launchctl", "load", "-w", plist).CombinedOutput(); err != nil {
			fmt.Fprintf(errOut, "launchctl load: %v: %s\n", err, strings.TrimSpace(string(output)))
			return 1
		}
		fmt.Fprintf(out, "installed %s\nlogs: %s\n", plist, filepath.Join(dir, "logs"))
	case "linux":
		unit := filepath.Join(home, ".config", "systemd", "user", "aria-daemon.service")
		if action == "uninstall" {
			exec.Command("systemctl", "--user", "disable", "--now", "aria-daemon.service").Run()
			if err := os.Remove(unit); err != nil && !os.IsNotExist(err) {
				fmt.Fprintln(errOut, err)
				return 1
			}
			fmt.Fprintln(out, "removed", unit)
			return 0
		}
		os.MkdirAll(filepath.Dir(unit), 0o755)
		quote := func(s string) string { return `"` + strings.NewReplacer(`\`, `\\`, `"`, `\"`).Replace(s) + `"` }
		body := "[Unit]\nDescription=Aria price alerts and schedules\n\n[Service]\nExecStart=" + quote(exe) + " daemon run\n" +
			"Environment=" + quote("ARIA_DAEMON_DIR="+dir) + "\nRestart=on-failure\nRestartSec=30\n\n[Install]\nWantedBy=default.target\n"
		if err := os.WriteFile(unit, []byte(body), 0o644); err != nil {
			fmt.Fprintln(errOut, err)
			return 1
		}
		fmt.Fprintf(out, "wrote %s\nstart it with: systemctl --user daemon-reload && systemctl --user enable --now aria-daemon.service\n", unit)
	default:
		fmt.Fprintln(errOut, "install supports macOS and Linux; on Windows run `aria-runtime daemon run` from Task Scheduler")
		return 1
	}
	return 0
}
