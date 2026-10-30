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

// Header/Query/Form accessors share the .Get/.Set shape with routers;
// a denied receiver chain or accessor-typed parameter must emit nothing
// even when the file imports a web framework.
func TestNonRouterVerbsEmitNothing(t *testing.T) {
	ctx := extractOne(t, map[string]string{
		"a.go": `package demo

import (
	"net/http"
	"net/url"

	"github.com/go-chi/chi/v5"
)

func h(w http.ResponseWriter, r *http.Request) {}

func f(req *http.Request, q url.Values, r chi.Router) {
	req.Header.Get("Content-Type")
	req.URL.Query().Get("include_related")
	q.Get("model")
	vals := req.URL.Query()
	vals.Get("filter")
	r.Get("/real", h)
}
`,
	})
	recs := extractRoutes(ctx)
	ids := recordIDs(recs)
	if !ids["route|GET /real"] {
		t.Errorf("router-typed param should emit, got %v", ids)
	}
	for _, r := range recs {
		if r.ID != "GET /real" {
			t.Errorf("non-router receiver emitted a bogus route: %+v", r)
		}
	}
}

// r.Mount("/base", sub) joins the mounted router's own registrations
// under the mount prefix, for local and package-level router vars.
func TestMountPrefixesJoin(t *testing.T) {
	ctx := extractOne(t, map[string]string{
		"a.go": `package demo

import (
	"net/http"

	"github.com/go-chi/chi/v5"
)

var apiMux = chi.NewRouter()

func h(w http.ResponseWriter, r *http.Request) {}

func init() {
	apiMux.Get("/pkg", h)
}

func mountAll(r chi.Router) {
	sub := chi.NewRouter()
	sub.Get("/local", h)
	r.Mount("/api/v2", sub)
	r.Mount("/api/pkg", apiMux)
}
`,
	})
	ids := recordIDs(extractRoutes(ctx))
	for _, want := range []string{
		"route|GET /api/v2/local",
		"route|GET /api/pkg/pkg",
		"route|ANY /api/v2",
		"route|ANY /api/pkg",
	} {
		if !ids[want] {
			t.Errorf("missing %s (mount prefix should join), got %v", want, ids)
		}
	}
	if ids["route|GET /local"] || ids["route|GET /pkg"] {
		t.Errorf("mounted router leaked an unprefixed route: %v", ids)
	}
}

// A helper that takes the router as a parameter inherits every prefix
// callers feed it, including a Mount that follows the call in statement
// order.
func TestHelperFuncPrefixPropagation(t *testing.T) {
	ctx := extractOne(t, map[string]string{
		"a.go": `package demo

import (
	"net/http"

	"github.com/go-chi/chi/v5"
)

func h(w http.ResponseWriter, r *http.Request) {}

func registerChatAPIRoutes(r chi.Router, dep int) {
	r.Post("/chats/{chat}/clear", h)
	r.Get("/chats", h)
}

func top() {
	api := chi.NewRouter()
	registerChatAPIRoutes(api, 0)
	root := chi.NewRouter()
	root.Mount("/api/v2", api)
	root.Mount("/api/experimental", api)
}
`,
	})
	ids := recordIDs(extractRoutes(ctx))
	for _, want := range []string{
		"route|POST /api/v2/chats/{chat}/clear",
		"route|POST /api/experimental/chats/{chat}/clear",
		"route|GET /api/v2/chats",
		"route|GET /api/experimental/chats",
	} {
		if !ids[want] {
			t.Errorf("missing %s (helper prefix should propagate), got %v", want, ids)
		}
	}
	if ids["route|POST /chats/{chat}/clear"] {
		t.Errorf("helper route leaked without a prefix: %v", ids)
	}
}

// r.Route("/authorize", func(r){ r.Get("/", ...) }) is the same surface
// as r.Get("/authorize"): joined paths normalize the trailing slash.
func TestRoutePrefixTrailingSlash(t *testing.T) {
	ctx := extractOne(t, map[string]string{
		"a.go": `package demo

import (
	"net/http"

	"github.com/go-chi/chi/v5"
)

func h(w http.ResponseWriter, r *http.Request) {}

func top(r chi.Router) {
	r.Route("/authorize", func(r chi.Router) {
		r.Get("/", h)
		r.Post("/token", h)
	})
	r.Get("/plain", h)
}
`,
	})
	ids := recordIDs(extractRoutes(ctx))
	for _, want := range []string{
		"route|GET /authorize",
		"route|POST /authorize/token",
		"route|GET /plain",
	} {
		if !ids[want] {
			t.Errorf("missing %s (trailing slash should normalize), got %v", want, ids)
		}
	}
	if ids["route|GET /authorize/"] {
		t.Errorf("trailing slash leaked into the route id: %v", ids)
	}
}

// Selector calls feed router params the same way bare calls do:
// api.registerChatAPIRoutes(r, mw) matches the same-package method
// (*API).registerChatAPIRoutes by name.
func TestMethodCallPrefixPropagation(t *testing.T) {
	ctx := extractOne(t, map[string]string{
		"a.go": `package demo

import (
	"net/http"

	"github.com/go-chi/chi/v5"
)

type API struct{}

func h(w http.ResponseWriter, r *http.Request) {}

func mw(next http.Handler) http.Handler { return next }

func (api *API) registerChatAPIRoutes(r chi.Router, m func(http.Handler) http.Handler) {
	r.Post("/chats/{chat}/clear", h)
	r.Get("/chats/by-workspace", h)
}

func (api *API) top(r chi.Router) {
	r.Route("/api/v2", func(r chi.Router) {
		api.registerChatAPIRoutes(r, mw)
	})
}
`,
	})
	ids := recordIDs(extractRoutes(ctx))
	for _, want := range []string{
		"route|POST /api/v2/chats/{chat}/clear",
		"route|GET /api/v2/chats/by-workspace",
	} {
		if !ids[want] {
			t.Errorf("missing %s (method call should feed router params), got %v", want, ids)
		}
	}
}

// x.Field = r inside a Route scope binds the field name to that prefix:
// api.ExperimentalHandler picks up /api/experimental wherever the field
// is used as a receiver, including through a deeper selector chain.
func TestFieldAssignedRouterBinds(t *testing.T) {
	ctx := extractOne(t, map[string]string{
		"a.go": `package demo

import (
	"net/http"

	"github.com/go-chi/chi/v5"
)

type inner struct{ ExperimentalHandler chi.Router }
type api struct{ AGPL inner }

func h(w http.ResponseWriter, r *http.Request) {}

func build(a *api, r chi.Router) {
	r.Route("/api/experimental", func(r chi.Router) {
		a.AGPL.ExperimentalHandler = r
	})
}

func ent(a *api) {
	a.AGPL.ExperimentalHandler.Group(func(r chi.Router) {
		r.Route("/ai-gateway", func(r chi.Router) {
			r.Get("/keys", h)
			r.Delete("/keys/{key}", h)
		})
		r.Get("/organizations/{organization}/ai/spend/users", h)
	})
}
`,
	})
	ids := recordIDs(extractRoutes(ctx))
	for _, want := range []string{
		"route|GET /api/experimental/ai-gateway/keys",
		"route|DELETE /api/experimental/ai-gateway/keys/{key}",
		"route|GET /api/experimental/organizations/{organization}/ai/spend/users",
	} {
		if !ids[want] {
			t.Errorf("missing %s (field-bound router should carry prefix), got %v", want, ids)
		}
	}
	if ids["route|GET /ai-gateway/keys"] {
		t.Errorf("field-bound router leaked an unprefixed route: %v", ids)
	}
}

// Route arg2 can be a call to a factory returning func(r chi.Router):
// the returned literal's registrations join under the mount prefix.
func TestRouteFactoryCallArg(t *testing.T) {
	ctx := extractOne(t, map[string]string{
		"a.go": `package demo

import (
	"net/http"

	"github.com/go-chi/chi/v5"
)

func h(w http.ResponseWriter, r *http.Request) {}

func aiProvidersHandler(dep int) func(r chi.Router) {
	return func(r chi.Router) {
		r.Get("/", h)
		r.Get("/{provider}/models", h)
	}
}

func top(r chi.Router) {
	r.Route("/api/v2", func(r chi.Router) {
		r.Route("/ai/providers", aiProvidersHandler(0))
	})
}
`,
	})
	ids := recordIDs(extractRoutes(ctx))
	for _, want := range []string{
		"route|GET /api/v2/ai/providers",
		"route|GET /api/v2/ai/providers/{provider}/models",
	} {
		if !ids[want] {
			t.Errorf("missing %s (factory literal should mount), got %v", want, ids)
		}
	}
}

func TestNormRoutePath(t *testing.T) {
	cases := map[string]string{
		"/":             "/",
		"/authorize":    "/authorize",
		"/authorize/":   "/authorize",
		"/a//":          "/a",
		"/api/v2/chats": "/api/v2/chats",
	}
	for in, want := range cases {
		if got := normRoutePath(in); got != want {
			t.Errorf("normRoutePath(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestNestedFactoryMount(t *testing.T) {
	ctx := extractOne(t, map[string]string{
		"factory.go": `package demo

import (
	"net/http"

	"github.com/go-chi/chi/v5"
)

func h(w http.ResponseWriter, r *http.Request) {}

func inner(mw func(http.Handler) http.Handler) func(r chi.Router) {
	return func(r chi.Router) {
		r.Get("/sessions", h)
		r.Get("/models", h)
	}
}

func outer(mw func(http.Handler) http.Handler) func(r chi.Router) {
	return inner(mw)
}
`,
		"a.go": `package demo

import "github.com/go-chi/chi/v5"

func top(r chi.Router) {
	r.Route("/api/v2", func(r chi.Router) {
		r.Route("/aibridge", outer(nil))
		r.Route("/ai-gateway", outer(nil))
	})
}
`,
	})
	recs := extractRoutes(ctx)
	ids := recordIDs(recs)
	for _, want := range []string{
		"route|GET /api/v2/aibridge/sessions",
		"route|GET /api/v2/aibridge/models",
		"route|GET /api/v2/ai-gateway/sessions",
		"route|GET /api/v2/ai-gateway/models",
	} {
		if !ids[want] {
			t.Errorf("missing %s (nested factory should mount), got %v", want, ids)
		}
	}
	for _, id := range []string{"route|GET /sessions", "route|GET /models"} {
		if ids[id] {
			t.Errorf("prefixless record %s leaked", id)
		}
	}
	var sawSessions bool
	for _, r := range recs {
		if r.ID != "GET /api/v2/aibridge/sessions" {
			continue
		}
		sawSessions = true
		if r.File != "factory.go" {
			t.Errorf("file = %s, want factory.go", r.File)
		}
		if r.Line != 13 {
			t.Errorf("line = %d, want 13 (the r.Get call in factory.go)", r.Line)
		}
	}
	if !sawSessions {
		t.Error("GET /api/v2/aibridge/sessions record not emitted")
	}
}
