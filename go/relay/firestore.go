package main

// Firestore keeps the relay's state across Cloud Run deploys. The layout is
// the Python relay's; every read is by document id, never a query:
//
//	relay_bindings/{feishu_user_id}          {client_id, bound_at}
//	relay_clients/{client_id}                {token_hash, bind_code_hash, created_at}
//	relay_bind_codes/{bind_code_hash}        {client_id}
//	relay_client_chats/{sha256(client\0chat)} {client_id, chat_id, last_seen}
//	relay_card_origins/{message_id}          {client_id, created_at}
//
// The store logic talks to a small documents interface, so the same tests
// run it against an in-memory fake; firestoreDocuments adapts the client.

import (
	"context"
	"errors"

	"cloud.google.com/go/firestore"
	"google.golang.org/api/iterator"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type documents interface {
	Get(ctx context.Context, collection, id string) (map[string]any, bool, error)
	Set(ctx context.Context, collection, id string, data map[string]any, merge bool) error
	// Create fails with created=false when the document exists.
	Create(ctx context.Context, collection, id string, data map[string]any) (bool, error)
	Delete(ctx context.Context, collection, id string) error
	Count(ctx context.Context, collection string) (int, error)
}

type firestoreStore struct{ docs documents }

func (s *firestoreStore) Kind() string { return "firestore" }

func (s *firestoreStore) field(ctx context.Context, collection, id, name string) (string, error) {
	if id == "" {
		return "", nil
	}
	data, ok, err := s.docs.Get(ctx, collection, id)
	if err != nil || !ok {
		return "", err
	}
	value, _ := data[name].(string)
	return value, nil
}

func chatKey(client, chat string) string { return sha256Hex(client + "\x00" + chat) }

func (s *firestoreStore) ClientForUser(ctx context.Context, user string) (string, error) {
	return s.field(ctx, "relay_bindings", user, "client_id")
}

func (s *firestoreStore) BindUser(ctx context.Context, user, client string) error {
	return s.docs.Set(ctx, "relay_bindings", user, map[string]any{"client_id": client, "bound_at": now()}, false)
}

func (s *firestoreStore) BindingCount(ctx context.Context) (int, error) {
	return s.docs.Count(ctx, "relay_bindings")
}

func (s *firestoreStore) Credentials(ctx context.Context, client string) (*Credentials, error) {
	if client == "" {
		return nil, nil
	}
	data, ok, err := s.docs.Get(ctx, "relay_clients", client)
	if err != nil || !ok {
		return nil, err
	}
	token, _ := data["token_hash"].(string)
	code, _ := data["bind_code_hash"].(string)
	return &Credentials{TokenHash: token, BindCodeHash: code}, nil
}

func (s *firestoreStore) ClaimCredentials(ctx context.Context, client, token, code string) (bool, error) {
	created, err := s.docs.Create(ctx, "relay_clients", client,
		map[string]any{"token_hash": token, "bind_code_hash": code, "created_at": now()})
	if err != nil || !created {
		return false, err
	}
	return true, s.docs.Set(ctx, "relay_bind_codes", code, map[string]any{"client_id": client}, false)
}

func (s *firestoreStore) SetBindCode(ctx context.Context, client, code string) error {
	old, err := s.field(ctx, "relay_clients", client, "bind_code_hash")
	if err != nil {
		return err
	}
	if old != "" && old != code {
		if err := s.docs.Delete(ctx, "relay_bind_codes", old); err != nil {
			return err
		}
	}
	if err := s.docs.Set(ctx, "relay_bind_codes", code, map[string]any{"client_id": client}, false); err != nil {
		return err
	}
	return s.docs.Set(ctx, "relay_clients", client, map[string]any{"bind_code_hash": code}, true)
}

func (s *firestoreStore) ClientForBindCode(ctx context.Context, code string) (string, error) {
	return s.field(ctx, "relay_bind_codes", code, "client_id")
}

func (s *firestoreStore) RememberChat(ctx context.Context, client, chat string) error {
	return s.docs.Set(ctx, "relay_client_chats", chatKey(client, chat),
		map[string]any{"client_id": client, "chat_id": chat, "last_seen": now()}, false)
}

func (s *firestoreStore) HasChat(ctx context.Context, client, chat string) (bool, error) {
	_, ok, err := s.docs.Get(ctx, "relay_client_chats", chatKey(client, chat))
	return ok, err
}

func (s *firestoreStore) RememberCard(ctx context.Context, message, client string) error {
	return s.docs.Set(ctx, "relay_card_origins", message, map[string]any{"client_id": client, "created_at": now()}, false)
}

func (s *firestoreStore) CardOrigin(ctx context.Context, message string) (string, error) {
	return s.field(ctx, "relay_card_origins", message, "client_id")
}

// ── the real client ─────────────────────────────────────────────────────────

type firestoreDocuments struct{ client *firestore.Client }

func openFirestore(ctx context.Context, project, database string) (*firestoreStore, error) {
	if project == "" {
		project = firestore.DetectProjectID
	}
	if database == "" {
		database = firestore.DefaultDatabaseID
	}
	client, err := firestore.NewClientWithDatabase(ctx, project, database)
	if err != nil {
		return nil, err
	}
	return &firestoreStore{docs: &firestoreDocuments{client: client}}, nil
}

func (f *firestoreDocuments) Get(ctx context.Context, collection, id string) (map[string]any, bool, error) {
	snap, err := f.client.Collection(collection).Doc(id).Get(ctx)
	if status.Code(err) == codes.NotFound {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	return snap.Data(), snap.Exists(), nil
}

func (f *firestoreDocuments) Set(ctx context.Context, collection, id string, data map[string]any, merge bool) error {
	ref := f.client.Collection(collection).Doc(id)
	var err error
	if merge {
		_, err = ref.Set(ctx, data, firestore.MergeAll)
	} else {
		_, err = ref.Set(ctx, data)
	}
	return err
}

func (f *firestoreDocuments) Create(ctx context.Context, collection, id string, data map[string]any) (bool, error) {
	_, err := f.client.Collection(collection).Doc(id).Create(ctx, data)
	if status.Code(err) == codes.AlreadyExists {
		return false, nil
	}
	return err == nil, err
}

func (f *firestoreDocuments) Delete(ctx context.Context, collection, id string) error {
	_, err := f.client.Collection(collection).Doc(id).Delete(ctx)
	return err
}

func (f *firestoreDocuments) Count(ctx context.Context, collection string) (int, error) {
	refs := f.client.Collection(collection).DocumentRefs(ctx)
	count := 0
	for {
		_, err := refs.Next()
		if errors.Is(err, iterator.Done) {
			return count, nil
		}
		if err != nil {
			return 0, err
		}
		count++
	}
}
