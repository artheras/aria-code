package main

// Where the relay keeps what it must not forget: bindings (which Feishu user
// reaches which machine), each machine's credentials, the chats a client may
// post to, and which client posted which card.
//
// Both stores keep the Python relay's layout (src/aria_code/relay_store.py),
// so either implementation can open the other's SQLite file or Firestore
// database. The one operation that must be atomic is ClaimCredentials: the
// first registration of a client_id wins.

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"strings"
	"sync"
	"time"

	_ "modernc.org/sqlite"
)

type Credentials struct {
	TokenHash    string
	BindCodeHash string
}

type Store interface {
	Kind() string
	ClientForUser(ctx context.Context, feishuUserID string) (string, error)
	BindUser(ctx context.Context, feishuUserID, clientID string) error
	BindingCount(ctx context.Context) (int, error)
	Credentials(ctx context.Context, clientID string) (*Credentials, error)
	// ClaimCredentials records a client's first credentials; false when the
	// client_id is already claimed.
	ClaimCredentials(ctx context.Context, clientID, tokenHash, bindCodeHash string) (bool, error)
	SetBindCode(ctx context.Context, clientID, bindCodeHash string) error
	ClientForBindCode(ctx context.Context, bindCodeHash string) (string, error)
	RememberChat(ctx context.Context, clientID, chatID string) error
	HasChat(ctx context.Context, clientID, chatID string) (bool, error)
	RememberCard(ctx context.Context, messageID, clientID string) error
	CardOrigin(ctx context.Context, messageID string) (string, error)
}

func now() float64 { return float64(time.Now().UnixNano()) / 1e9 }

func sha256Hex(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}

// ── SQLite ─────────────────────────────────────────────────────────────────

type sqliteStore struct {
	db *sql.DB
	mu sync.Mutex // one writer at a time, like the Python relay's single connection
}

func openSQLite(path string) (*sqliteStore, error) {
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	_, err = db.Exec(`
		CREATE TABLE IF NOT EXISTS bindings (
			feishu_user_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, bound_at REAL NOT NULL);
		CREATE TABLE IF NOT EXISTS client_credentials (
			client_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL,
			bind_code_hash TEXT NOT NULL, created_at REAL NOT NULL);
		CREATE TABLE IF NOT EXISTS client_chats (
			client_id TEXT NOT NULL, chat_id TEXT NOT NULL, last_seen REAL NOT NULL,
			PRIMARY KEY (client_id, chat_id));
		CREATE TABLE IF NOT EXISTS card_origins (
			message_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, created_at REAL NOT NULL);`)
	if err != nil {
		db.Close()
		return nil, err
	}
	return &sqliteStore{db: db}, nil
}

func (s *sqliteStore) Kind() string { return "sqlite" }

func (s *sqliteStore) one(ctx context.Context, query string, args ...any) (string, error) {
	var value string
	err := s.db.QueryRowContext(ctx, query, args...).Scan(&value)
	if errors.Is(err, sql.ErrNoRows) {
		return "", nil
	}
	return value, err
}

func (s *sqliteStore) exec(ctx context.Context, query string, args ...any) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.ExecContext(ctx, query, args...)
	return err
}

func (s *sqliteStore) ClientForUser(ctx context.Context, user string) (string, error) {
	return s.one(ctx, "SELECT client_id FROM bindings WHERE feishu_user_id = ?", user)
}

func (s *sqliteStore) BindUser(ctx context.Context, user, client string) error {
	return s.exec(ctx, "INSERT OR REPLACE INTO bindings VALUES (?, ?, ?)", user, client, now())
}

func (s *sqliteStore) BindingCount(ctx context.Context) (int, error) {
	var n int
	err := s.db.QueryRowContext(ctx, "SELECT COUNT(*) FROM bindings").Scan(&n)
	return n, err
}

func (s *sqliteStore) Credentials(ctx context.Context, client string) (*Credentials, error) {
	var c Credentials
	err := s.db.QueryRowContext(ctx,
		"SELECT token_hash, bind_code_hash FROM client_credentials WHERE client_id = ?", client).
		Scan(&c.TokenHash, &c.BindCodeHash)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &c, nil
}

func (s *sqliteStore) ClaimCredentials(ctx context.Context, client, token, code string) (bool, error) {
	err := s.exec(ctx, "INSERT INTO client_credentials VALUES (?, ?, ?, ?)", client, token, code, now())
	if err != nil && strings.Contains(strings.ToLower(err.Error()), "unique") {
		return false, nil
	}
	return err == nil, err
}

func (s *sqliteStore) SetBindCode(ctx context.Context, client, code string) error {
	return s.exec(ctx, "UPDATE client_credentials SET bind_code_hash = ? WHERE client_id = ?", code, client)
}

func (s *sqliteStore) ClientForBindCode(ctx context.Context, code string) (string, error) {
	return s.one(ctx, "SELECT client_id FROM client_credentials WHERE bind_code_hash = ?", code)
}

func (s *sqliteStore) RememberChat(ctx context.Context, client, chat string) error {
	return s.exec(ctx, "INSERT OR REPLACE INTO client_chats VALUES (?, ?, ?)", client, chat, now())
}

func (s *sqliteStore) HasChat(ctx context.Context, client, chat string) (bool, error) {
	found, err := s.one(ctx, "SELECT 'yes' FROM client_chats WHERE client_id = ? AND chat_id = ?", client, chat)
	return found != "", err
}

func (s *sqliteStore) RememberCard(ctx context.Context, message, client string) error {
	return s.exec(ctx, "INSERT OR REPLACE INTO card_origins VALUES (?, ?, ?)", message, client, now())
}

func (s *sqliteStore) CardOrigin(ctx context.Context, message string) (string, error) {
	return s.one(ctx, "SELECT client_id FROM card_origins WHERE message_id = ?", message)
}
