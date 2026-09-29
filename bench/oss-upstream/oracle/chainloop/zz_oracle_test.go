package redaction

// Hidden oracle for chainloop issue 3481. Public engine behaviour only.

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
)

var oracleJWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9." +
	"eyJzdWIiOiJmYWtlLXVwbG9hZCIsImV4cCI6MTc5MDAwMDAwMH0." +
	"c2lnbmF0dXJlLWZha2UtZm9yLXJlcHJv"

func oracleRedact(t *testing.T, doc []byte) []byte {
	t.Helper()
	sc, err := DefaultScanner()
	if err != nil {
		t.Fatal(err)
	}
	r := New(sc)
	out, _, err := r.Redact(context.Background(), doc)
	if err != nil {
		t.Fatal(err)
	}
	again, _, err := r.Redact(context.Background(), out)
	if err != nil {
		t.Fatal(err)
	}
	if string(again) != string(out) {
		t.Errorf("not idempotent:\n%s\n%s", out, again)
	}
	if !json.Valid(out) {
		t.Errorf("invalid json: %s", out)
	}
	return out
}

func oracleLeaf(t *testing.T, leaf string) string {
	t.Helper()
	doc, _ := json.Marshal(map[string]any{"content": leaf})
	out := oracleRedact(t, doc)
	var got struct {
		Content string `json:"content"`
	}
	if err := json.Unmarshal(out, &got); err != nil {
		t.Fatal(err)
	}
	return got.Content
}

func TestOracleJWTBeforeEscapedQuoteKeepsHost(t *testing.T) {
	const host = "https://uploads.linear.app/o/a/b"
	inner := `{"url":"` + host + `?signature=` + oracleJWT + `"}`
	doc, _ := json.Marshal(map[string]any{"result": []any{map[string]any{"type": "text", "text": inner}}})
	out := string(oracleRedact(t, doc))
	if strings.Contains(out, oracleJWT) {
		t.Fatalf("jwt survived: %s", out)
	}
	if !strings.Contains(out, host+`?signature=[REDACTED:jwt]`) {
		t.Fatalf("host context lost: %s", out)
	}
}

// Ticket-conformant: the ticket scopes the change to the backslash trim, so a
// line break after the JWT must be no worse than base (kept, or dropped exactly
// as base drops it), never the whole leaf.
func TestOracleLineBreaksAfterJWTNoWorseThanBase(t *testing.T) {
	for _, sep := range []string{"\n", "\n\n", "\r\n"} {
		got := oracleLeaf(t, "Bearer "+oracleJWT+sep+"Host: uploads.linear.app")
		kept := "Bearer [REDACTED:jwt]" + sep + "Host: uploads.linear.app"
		asBase := "Bearer [REDACTED:jwt]Host: uploads.linear.app"
		if got != kept && got != asBase {
			t.Errorf("sep %q: got %q, want %q or base-equivalent %q", sep, got, kept, asBase)
		}
	}
}

// Strict (informational, beyond the ticket): only the JWT is replaced, so the
// line break survives, as the upstream issue's optional item asks.
func TestStrictLineBreaksAfterJWTSurvive(t *testing.T) {
	for _, sep := range []string{"\n", "\n\n", "\r\n"} {
		got := oracleLeaf(t, "Bearer "+oracleJWT+sep+"Host: uploads.linear.app")
		want := "Bearer [REDACTED:jwt]" + sep + "Host: uploads.linear.app"
		if got != want {
			t.Errorf("sep %q: got %q want %q", sep, got, want)
		}
	}
}

func TestOracleLiteralBackslashNPasswordFullyRedacted(t *testing.T) {
	got := oracleLeaf(t, `DB_PASSWORD=Zq8mLp2Vx9rT\n next`)
	if strings.Contains(got, "Zq8mLp2Vx9rT") || strings.Contains(got, `T\n`) || strings.Contains(got, `]\n next`) {
		t.Errorf("password tail leaked: %q", got)
	}
	if !strings.Contains(got, "[REDACTED:generic-password]") {
		t.Errorf("password not redacted: %q", got)
	}
}

func TestOracleShortSecretDoesNotReplaceUnrelatedText(t *testing.T) {
	got := oracleLeaf(t, "password: s\r\n\r\nHost: services.example.com")
	if n := strings.Count(got, "[REDACTED"); n != 1 {
		t.Errorf("want exactly 1 placeholder, got %d: %q", n, got)
	}
	if !strings.Contains(got, "Host: services.example.com") {
		t.Errorf("unrelated text damaged: %q", got)
	}
}
