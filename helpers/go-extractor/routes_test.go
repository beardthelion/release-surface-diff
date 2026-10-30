package main

import (
	"testing"
)

func TestCanonicalTemplate(t *testing.T) {
	cases := map[string]string{
		"/users/:id":         "/users/{id}",
		"/a/<id>":            "/a/{id}",
		"/a/<int:id>":        "/a/{id}",
		"/items/{id:[0-9]+}": "/items/{id}",
		"/files/{path...}":   "/files/{path}",
		"/exact/{$}":         "/exact/",
		"/wild/*rest":        "/wild/{rest}",
		"/users/{id}":        "/users/{id}",
		"/plain/path":        "/plain/path",
		"/chi/:a/:b":         "/chi/{a}/{b}",
	}
	for in, want := range cases {
		if got := canonicalTemplate(in); got != want {
			t.Errorf("canonicalTemplate(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestJoinPaths(t *testing.T) {
	cases := [][3]string{
		{"", "/a", "/a"},
		{"/api", "/users", "/api/users"},
		{"/api/", "/users", "/api/users"},
		{"/api", "users", "/api/users"},
		{"/api", "", "/api"},
	}
	for _, c := range cases {
		if got := joinPaths(c[0], c[1]); got != c[2] {
			t.Errorf("joinPaths(%q,%q) = %q, want %q", c[0], c[1], got, c[2])
		}
	}
}

// extractOne loads a temp dir of sources through the fallback loader and
// returns all extracted records.
func extractOne(t *testing.T, files map[string]string) *pkgCtx {
	t.Helper()
	dir := writePkg(t, files)
	ctx := loadDirFallback(dir, dir)
	if ctx == nil {
		t.Fatal("no package loaded")
	}
	ctx.index()
	return ctx
}

func TestVariablePathResolves(t *testing.T) {
	ctx := extractOne(t, map[string]string{
		"a.go": `package demo

import "net/http"

const apiPath = "/const"

var varPath = "/var"

func h(w http.ResponseWriter, r *http.Request) {}

func f() {
	local := "/local"
	mux := http.NewServeMux()
	mux.HandleFunc(apiPath, h)
	mux.HandleFunc(varPath, h)
	mux.HandleFunc(local, h)
}
`,
	})
	ids := recordIDs(extractRoutes(ctx))
	for _, want := range []string{"ANY /const", "ANY /var", "ANY /local"} {
		if !ids["route|"+want] {
			t.Errorf("missing route %s (variable path should resolve)", want)
		}
	}
}

func TestNoFrameworkImportNoRoute(t *testing.T) {
	ctx := extractOne(t, map[string]string{
		"a.go": `package demo

func h() {}

func f(x Getter) {
	x.Get("/looks-like-a-route", h)
}

type Getter interface {
	Get(string, func())
}
`,
	})
	if recs := extractRoutes(ctx); len(recs) != 0 {
		t.Errorf("verb call without a framework import should not emit: %+v", recs)
	}
}

func TestGorillaMethodsChain(t *testing.T) {
	ctx := extractOne(t, map[string]string{
		"a.go": `package demo

import "github.com/gorilla/mux"

func h() {}

func f(r *mux.Router) {
	r.HandleFunc("/a", h).Methods("GET", "POST")
}
`,
	})
	ids := recordIDs(extractRoutes(ctx))
	if !ids["route|GET /a"] || !ids["route|POST /a"] {
		t.Errorf("Methods chain should emit one record per verb, got %v", ids)
	}
}
