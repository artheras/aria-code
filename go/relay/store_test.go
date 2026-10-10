package main

import (
	"context"
	"path/filepath"
	"sync"
	"testing"
)

// fakeDocuments is Firestore's document semantics in memory.
type fakeDocuments struct {
	mu   sync.Mutex
	data map[string]map[string]map[string]any
}

func newFakeDocuments() *fakeDocuments {
	return &fakeDocuments{data: map[string]map[string]map[string]any{}}
}

func (f *fakeDocuments) Get(_ context.Context, c, id string) (map[string]any, bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	doc, ok := f.data[c][id]
	return doc, ok, nil
}

func (f *fakeDocuments) Set(_ context.Context, c, id string, data map[string]any, merge bool) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.data[c] == nil {
		f.data[c] = map[string]map[string]any{}
	}
	doc := map[string]any{}
	if merge {
		for k, v := range f.data[c][id] {
			doc[k] = v
		}
	}
	for k, v := range data {
		doc[k] = v
	}
	f.data[c][id] = doc
	return nil
}

// Create is atomic, like Firestore's: the existence check and the write
// happen under one lock, so concurrent creates have exactly one winner.
func (f *fakeDocuments) Create(_ context.Context, c, id string, data map[string]any) (bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if _, exists := f.data[c][id]; exists {
		return false, nil
	}
	if f.data[c] == nil {
		f.data[c] = map[string]map[string]any{}
	}
	doc := map[string]any{}
	for k, v := range data {
		doc[k] = v
	}
	f.data[c][id] = doc
	return true, nil
}

func (f *fakeDocuments) Delete(_ context.Context, c, id string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	delete(f.data[c], id)
	return nil
}

func (f *fakeDocuments) Count(_ context.Context, c string) (int, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.data[c]), nil
}

// The same contract for both stores, as tests/test_relay_store.py does in Python.
func stores(t *testing.T) map[string]Store {
	sqlite, err := openSQLite(filepath.Join(t.TempDir(), "relay.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { sqlite.db.Close() })
	return map[string]Store{"sqlite": sqlite, "firestore": &firestoreStore{docs: newFakeDocuments()}}
}

// ok checks the error half of a (value, error) result: ok[string](t)(store.X()).
func ok[T any](t *testing.T) func(T, error) T {
	return func(value T, err error) T {
		t.Helper()
		if err != nil {
			t.Fatal(err)
		}
		return value
	}
}

func TestStoreContract(t *testing.T) {
	ctx := context.Background()
	for name, store := range stores(t) {
		t.Run(name, func(t *testing.T) {
			s, b, n, c := ok[string](t), ok[bool](t), ok[int](t), ok[*Credentials](t)
			e := func(err error) {
				t.Helper()
				if err != nil {
					t.Fatal(err)
				}
			}
			if got := s(store.ClientForUser(ctx, "ou_a")); got != "" {
				t.Fatalf("unbound user has %q", got)
			}
			e(store.BindUser(ctx, "ou_a", "aria-1"))
			e(store.BindUser(ctx, "ou_a", "aria-2")) // rebinding replaces
			if got := s(store.ClientForUser(ctx, "ou_a")); got != "aria-2" {
				t.Fatalf("binding = %q", got)
			}
			if n := n(store.BindingCount(ctx)); n != 1 {
				t.Fatalf("count = %d", n)
			}

			if c := c(store.Credentials(ctx, "aria-1")); c != nil {
				t.Fatalf("unknown client has credentials %+v", c)
			}
			if !b(store.ClaimCredentials(ctx, "aria-1", "tok", "code1")) {
				t.Fatal("first claim refused")
			}
			if b(store.ClaimCredentials(ctx, "aria-1", "other", "code2")) {
				t.Fatal("second claim accepted")
			}
			if c := c(store.Credentials(ctx, "aria-1")); c.TokenHash != "tok" || c.BindCodeHash != "code1" {
				t.Fatalf("credentials %+v", c)
			}
			if got := s(store.ClientForBindCode(ctx, "code1")); got != "aria-1" {
				t.Fatalf("bind code lookup = %q", got)
			}
			e(store.SetBindCode(ctx, "aria-1", "code3"))
			if got := s(store.ClientForBindCode(ctx, "code3")); got != "aria-1" {
				t.Fatalf("rotated code lookup = %q", got)
			}
			if got := s(store.ClientForBindCode(ctx, "code1")); got != "" {
				t.Fatalf("the old code still binds: %q", got)
			}

			if b(store.HasChat(ctx, "aria-1", "oc_1")) {
				t.Fatal("chat before it was seen")
			}
			e(store.RememberChat(ctx, "aria-1", "oc_1"))
			if !b(store.HasChat(ctx, "aria-1", "oc_1")) || b(store.HasChat(ctx, "aria-2", "oc_1")) {
				t.Fatal("chats are per client")
			}

			e(store.RememberCard(ctx, "om_card", "aria-1"))
			if got := s(store.CardOrigin(ctx, "om_card")); got != "aria-1" {
				t.Fatalf("card origin = %q", got)
			}
			if got := s(store.CardOrigin(ctx, "")); got != "" {
				t.Fatalf("empty id = %q", got)
			}
		})
	}
}

func TestOnlyOneConcurrentClaimWins(t *testing.T) {
	ctx := context.Background()
	for name, store := range stores(t) {
		t.Run(name, func(t *testing.T) {
			var wg sync.WaitGroup
			var mu sync.Mutex
			wins := 0
			for i := 0; i < 16; i++ {
				wg.Add(1)
				go func(i int) {
					defer wg.Done()
					if ok, err := store.ClaimCredentials(ctx, "aria-race", "tok", "code"); err == nil && ok {
						mu.Lock()
						wins++
						mu.Unlock()
					}
				}(i)
			}
			wg.Wait()
			if wins != 1 {
				t.Fatalf("%d claims won", wins)
			}
		})
	}
}
