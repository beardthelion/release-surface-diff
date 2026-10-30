package main

import (
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// fixtureRoot is the repo's shared Go fixture tree (U4): a module
// (surface/) exercising go/packages loading plus a directory without a
// go.mod (basic/) exercising the parser fallback.
func fixtureRoot(t *testing.T) string {
	t.Helper()
	abs, err := filepath.Abs(filepath.Join("..", "..", "tests", "fixtures", "go"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(abs); err != nil {
		t.Fatalf("fixture tree missing: %v", err)
	}
	return abs
}

func extractFixture(t *testing.T) ([]record, []symbolSpan, []callEdge) {
	t.Helper()
	root := fixtureRoot(t)
	records, spans, edges := extract(loadTree(root))
	sortRecords(records)
	sortSpans(spans)
	edges = sortEdges(edges)
	return records, spans, edges
}

func asJSON(t *testing.T, v any) map[string]any {
	t.Helper()
	data, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	if err := json.Unmarshal(data, &m); err != nil {
		t.Fatal(err)
	}
	return m
}

func TestGoldenRecords(t *testing.T) {
	records, _, _ := extractFixture(t)
	goldenPath := filepath.Join(fixtureRoot(t), "golden.records.jsonl")
	raw, err := os.ReadFile(goldenPath)
	if err != nil {
		t.Fatalf("golden file: %v", err)
	}
	var want []map[string]any
	for _, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
		var m map[string]any
		if err := json.Unmarshal([]byte(line), &m); err != nil {
			t.Fatalf("golden line not JSON: %v", err)
		}
		want = append(want, m)
	}
	if len(records) != len(want) {
		got := make([]string, len(records))
		for i, r := range records {
			got[i] = r.Kind + " " + r.ID + " @ " + r.File
		}
		t.Fatalf("record count %d != golden %d\ngot:\n%s",
			len(records), len(want), strings.Join(got, "\n"))
	}
	for i, r := range records {
		got := asJSON(t, r)
		for k, wv := range want[i] {
			gv, ok := got[k]
			if !ok {
				t.Errorf("record %d (%s %s) missing field %q", i, r.Kind, r.ID, k)
				continue
			}
			gj, _ := json.Marshal(gv)
			wj, _ := json.Marshal(wv)
			if string(gj) != string(wj) {
				t.Errorf("record %d (%s %s) field %q: got %s want %s",
					i, r.Kind, r.ID, k, gj, wj)
			}
		}
	}
}

func recordIDs(records []record) map[string]bool {
	ids := map[string]bool{}
	for _, r := range records {
		ids[r.Kind+"|"+r.ID] = true
	}
	return ids
}

func TestExclusionsHold(t *testing.T) {
	records, spans, _ := extractFixture(t)
	for _, r := range records {
		if strings.Contains(r.File, ".pb.go") {
			t.Errorf("generated file leaked a record: %+v", r)
		}
		if strings.Contains(r.File, "broken.go") {
			t.Errorf("malformed file leaked a record: %+v", r)
		}
		if strings.Contains(r.File, "internal") && r.Kind == "export" {
			t.Errorf("internal package leaked an export record: %+v", r)
		}
	}
	// The internal package still contributes non-export surface.
	ids := recordIDs(records)
	if !ids["route|ANY /hidden"] {
		t.Error("route inside internal/ should still emit a record")
	}
	for _, s := range spans {
		if strings.Contains(s.File, ".pb.go") || strings.Contains(s.File, "broken.go") {
			t.Errorf("excluded file leaked a symbol span: %+v", s)
		}
	}
}

func TestSymbolTableAndEdges(t *testing.T) {
	_, spans, edges := extractFixture(t)
	spanSet := map[string]bool{}
	for _, s := range spans {
		spanSet[s.File+"|"+s.Symbol] = true
		if s.StartLine <= 0 || s.EndLine < s.StartLine {
			t.Errorf("bad span range: %+v", s)
		}
	}
	for _, want := range []string{
		"surface/main.go|main",
		"surface/server.go|getUser",
		"surface/config.go|Config",
		"surface/lib/lib.go|Client.Connect",
		"basic/main.go|hello",
	} {
		if !spanSet[want] {
			t.Errorf("missing symbol span %s", want)
		}
	}
	edgeSet := map[callEdge]bool{}
	for _, e := range edges {
		edgeSet[e] = true
	}
	for _, want := range []callEdge{
		{Caller: "main", Callee: "auth"},
		{Caller: "getUser", Callee: "loadUser"},
	} {
		if !edgeSet[want] {
			t.Errorf("missing call edge %+v", want)
		}
	}
	// Cross-package calls never become edges (KTD8).
	for e := range edgeSet {
		if strings.Contains(e.Callee, "http.") || strings.Contains(e.Callee, "chi.") {
			t.Errorf("cross-package edge leaked: %+v", e)
		}
	}
}

// writePkg lays down one directory of .go files without a go.mod so the
// fallback loader handles it.
func writePkg(t *testing.T, files map[string]string) string {
	t.Helper()
	dir := t.TempDir()
	for name, src := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(src), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

func TestMalformedFileWarnsAndContinues(t *testing.T) {
	dir := writePkg(t, map[string]string{
		"good.go": `package demo

import "net/http"

func h(w http.ResponseWriter, r *http.Request) {}

func f() { http.HandleFunc("/ok", h) }
`,
		"bad.go": "package demo\n\nfunc broken( {\n",
	})
	var warnings []string
	old := warn
	warn = func(format string, args ...any) {
		warnings = append(warnings, format)
	}
	defer func() { warn = old }()

	ctx := loadDirFallback(dir, dir)
	if ctx == nil {
		t.Fatal("fallback returned no package for dir with one good file")
	}
	ctx.index()
	records := extractRoutes(ctx)
	if len(records) != 1 || records[0].ID != "ANY /ok" {
		t.Fatalf("good sibling file should still extract, got %+v", records)
	}
	if len(warnings) == 0 {
		t.Error("malformed file should produce a stderr warning")
	}
}

func TestRunEndToEnd(t *testing.T) {
	root := fixtureRoot(t)
	dir := t.TempDir()
	symPath := filepath.Join(dir, "symbols.jsonl")
	edgePath := filepath.Join(dir, "edges.jsonl")

	// Capture stdout: run() streams record JSONL there.
	oldStdout := os.Stdout
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	os.Stdout = w
	code := run([]string{root, "--symbols", symPath, "--edges", edgePath})
	w.Close()
	os.Stdout = oldStdout
	out, _ := io.ReadAll(r)
	if code != 0 {
		t.Fatalf("run exited %d", code)
	}
	lines := strings.Split(strings.TrimSpace(string(out)), "\n")
	if len(lines) == 0 {
		t.Fatal("no record JSONL on stdout")
	}
	for _, line := range lines {
		var m map[string]any
		if err := json.Unmarshal([]byte(line), &m); err != nil {
			t.Fatalf("stdout line not JSON: %v", err)
		}
		for _, field := range []string{
			"kind", "id", "file", "line", "symbol", "lang", "extractor", "attrs",
		} {
			if _, ok := m[field]; !ok {
				t.Errorf("record missing contract field %q: %s", field, line)
			}
		}
	}
	for _, sidecar := range []string{symPath, edgePath} {
		data, err := os.ReadFile(sidecar)
		if err != nil || len(data) == 0 {
			t.Errorf("sidecar %s missing or empty", sidecar)
		}
	}
}

func TestRunBadArgs(t *testing.T) {
	if code := run(nil); code != 2 {
		t.Errorf("missing root should exit 2, got %d", code)
	}
	if code := run([]string{"/nonexistent/dir/xyz"}); code != 2 {
		t.Errorf("bad root should exit 2, got %d", code)
	}
}
