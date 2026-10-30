package main

import (
	"go/ast"
	"regexp"
	"strings"
)

// Route registration extraction (R8): net/http, gorilla/mux, chi, gin,
// echo. Syntax-only, so receivers are matched by shape, not type: a verb
// call whose path argument is a string literal (or resolvable const) is a
// route when the file imports a web framework. Middleware visible on the
// registration chain lands in attrs.auth_hint so the diff engine can flag
// chain drift (R12).

var httpVerbs = map[string]string{
	"Get": "GET", "GET": "GET",
	"Post": "POST", "POST": "POST",
	"Put": "PUT", "PUT": "PUT",
	"Delete": "DELETE", "DELETE": "DELETE", "Del": "DELETE",
	"Patch": "PATCH", "PATCH": "PATCH",
	"Head": "HEAD", "HEAD": "HEAD",
	"Options": "OPTIONS", "OPTIONS": "OPTIONS",
	"Trace": "TRACE", "TRACE": "TRACE",
	"Connect": "CONNECT", "CONNECT": "CONNECT",
	"Any": "ANY", "ANY": "ANY",
}

var verbWords = map[string]bool{
	"GET": true, "HEAD": true, "POST": true, "PUT": true,
	"PATCH": true, "DELETE": true, "CONNECT": true,
	"OPTIONS": true, "TRACE": true, "ANY": true,
}

// gorillaChainPassthrough are mux chain builders that refine a route
// without carrying the path or verb, so a Handler chain walk can step
// through them: r.Host("h").Path("/p").Methods("GET").Handler(f).
var gorillaChainPassthrough = map[string]bool{
	"Host": true, "Schemes": true, "Queries": true,
	"Headers": true, "HeadersRegexp": true, "MatcherFunc": true,
	"Name": true, "SkipClean": true, "UseEncodedPath": true,
	"BuildOnly": true, "Subrouter": true,
}

// frameworkPath maps an import path to a framework label.
func frameworkPath(path string) string {
	switch {
	case path == "net/http":
		return "net/http"
	case strings.Contains(path, "go-chi/chi"):
		return "chi"
	case strings.Contains(path, "gin-gonic/gin"):
		return "gin"
	case strings.Contains(path, "labstack/echo"):
		return "echo"
	case strings.Contains(path, "gorilla/mux"):
		return "mux"
	}
	return ""
}

// fileFramework picks the framework a file's imports indicate; used for
// the framework attr and as the gate for receiver-agnostic verb calls.
// The priority order is fixed so output stays deterministic (R16):
// specific routers beat omnipresent net/http.
func fileFramework(imports map[string]string) string {
	present := map[string]bool{}
	for _, path := range imports {
		if fw := frameworkPath(path); fw != "" {
			present[fw] = true
		}
	}
	for _, fw := range []string{"gin", "echo", "chi", "mux", "net/http"} {
		if present[fw] {
			return fw
		}
	}
	return ""
}

// routeScope tracks per-function registration state: variables bound to a
// path prefix (Group/Route results), middleware accumulated on a router
// variable via .Use/.With/Group extras, and local string constants.
type routeScope struct {
	pkg    *pkgCtx
	prefix map[string]string
	mw     map[string][]string
	consts map[string]string
}

func newRouteScope(p *pkgCtx) *routeScope {
	return &routeScope{
		pkg:    p,
		prefix: map[string]string{},
		mw:     map[string][]string{},
		consts: map[string]string{},
	}
}

func (s *routeScope) child() *routeScope {
	c := newRouteScope(s.pkg)
	for k, v := range s.prefix {
		c.prefix[k] = v
	}
	for k, v := range s.mw {
		c.mw[k] = append([]string(nil), v...)
	}
	for k, v := range s.consts {
		c.consts[k] = v
	}
	return c
}

// routerCtx resolves a receiver expression to (prefix, middlewares):
// an Ident looks up scope bindings; a chained With/Group/Route call folds
// its own arguments in.
func (s *routeScope) routerCtx(e ast.Expr) (string, []string) {
	switch t := e.(type) {
	case *ast.Ident:
		return s.prefix[t.Name], append([]string(nil), s.mw[t.Name]...)
	case *ast.SelectorExpr:
		return "", nil
	case *ast.CallExpr:
		sel, ok := t.Fun.(*ast.SelectorExpr)
		if !ok {
			return "", nil
		}
		base, mws := s.routerCtx(sel.X)
		switch sel.Sel.Name {
		case "With":
			return base, append(mws, s.mwNames(t.Args)...)
		case "Group":
			if len(t.Args) > 0 {
				if lit, ok := s.stringOf(t.Args[0]); ok {
					base = joinPaths(base, lit)
				}
				mws = append(mws, s.mwNames(t.Args[1:])...)
			}
			return base, mws
		case "Route", "Mount":
			if len(t.Args) > 0 {
				if lit, ok := s.stringOf(t.Args[0]); ok {
					base = joinPaths(base, lit)
				}
			}
			return base, mws
		}
		return base, mws
	}
	return "", nil
}

func (s *routeScope) mwNames(args []ast.Expr) []string {
	var names []string
	for _, a := range args {
		if n := exprName(a); n != "" {
			names = append(names, n)
		}
	}
	return names
}

func (s *routeScope) stringOf(e ast.Expr) (string, bool) {
	if lit, ok := stringLit(e); ok {
		return lit, true
	}
	if id, ok := e.(*ast.Ident); ok {
		if v, hit := s.consts[id.Name]; hit {
			return v, true
		}
		if s.pkg != nil {
			if v, hit := s.pkg.consts[id.Name]; hit {
				return v, true
			}
		}
	}
	return "", false
}

// ---- statement walking ---------------------------------------------------

// extractRoutes walks each function body in statement order so .Use calls
// that precede a registration are visible on the chain.
func extractRoutes(p *pkgCtx) []record {
	var out []record
	for _, f := range p.files {
		fw := fileFramework(p.imports[f])
		file := p.fileOf(f)
		for _, decl := range f.Decls {
			fd, ok := decl.(*ast.FuncDecl)
			if !ok || fd.Body == nil {
				continue
			}
			w := &routeWalker{
				p: p, f: f, file: file, fw: fw,
				enclosing: funcSymbol(fd),
				consumed:  map[ast.Node]bool{},
			}
			w.walkBlock(fd.Body, newRouteScope(p))
			out = append(out, w.out...)
		}
	}
	return out
}

type routeWalker struct {
	p         *pkgCtx
	f         *ast.File
	file      string
	fw        string
	enclosing string
	consumed  map[ast.Node]bool
	out       []record
}

func (w *routeWalker) walkBlock(b *ast.BlockStmt, scope *routeScope) {
	if b == nil {
		return
	}
	for _, stmt := range b.List {
		w.walkStmt(stmt, scope)
	}
}

func (w *routeWalker) walkStmt(stmt ast.Stmt, scope *routeScope) {
	switch s := stmt.(type) {
	case *ast.AssignStmt:
		w.bindAssign(s, scope)
	case *ast.DeclStmt:
		w.bindDecl(s, scope)
	case *ast.IfStmt:
		w.scanExpr(s.Cond, scope)
		w.walkBlock(s.Body, scope)
		w.walkStmt(s.Else, scope)
	case *ast.ForStmt:
		w.walkBlock(s.Body, scope)
	case *ast.RangeStmt:
		w.walkBlock(s.Body, scope)
	case *ast.SwitchStmt:
		w.walkBlock(s.Body, scope)
	case *ast.SelectStmt:
		w.walkBlock(s.Body, scope)
	case *ast.CaseClause:
		for _, inner := range s.Body {
			w.walkStmt(inner, scope)
		}
	case *ast.BlockStmt:
		w.walkBlock(s, scope)
	case *ast.GoStmt:
		w.scanExpr(s.Call, scope)
	case *ast.DeferStmt:
		w.scanExpr(s.Call, scope)
	default:
		// ExprStmt, ReturnStmt, SendStmt, IncDecStmt, LabeledStmt, etc.
		w.scanExpr(stmt, scope)
	}
}

// bindAssign records `x := <call>` bindings that matter to route scope:
// router Group/With chains and plain string constants.
func (w *routeWalker) bindAssign(s *ast.AssignStmt, scope *routeScope) {
	for i, rhs := range s.Rhs {
		if i >= len(s.Lhs) {
			break
		}
		lhs, ok := s.Lhs[i].(*ast.Ident)
		if !ok {
			continue
		}
		if lit, ok := w.p.resolveString(rhs, scope.consts); ok {
			scope.consts[lhs.Name] = lit
			continue
		}
		call, ok := rhs.(*ast.CallExpr)
		if !ok {
			continue
		}
		sel, ok := call.Fun.(*ast.SelectorExpr)
		if !ok {
			continue
		}
		switch sel.Sel.Name {
		case "Group":
			prefix, mws := scope.routerCtx(sel.X)
			if len(call.Args) > 0 {
				if lit, ok := scope.stringOf(call.Args[0]); ok {
					prefix = joinPaths(prefix, lit)
				}
				mws = append(mws, scope.mwNames(call.Args[1:])...)
			}
			scope.prefix[lhs.Name] = prefix
			scope.mw[lhs.Name] = mws
		case "With":
			prefix, mws := scope.routerCtx(sel.X)
			mws = append(mws, scope.mwNames(call.Args)...)
			scope.prefix[lhs.Name] = prefix
			scope.mw[lhs.Name] = mws
		}
	}
	w.scanExpr(s, scope)
}

func (w *routeWalker) bindDecl(s *ast.DeclStmt, scope *routeScope) {
	gen, ok := s.Decl.(*ast.GenDecl)
	if !ok {
		return
	}
	for _, spec := range gen.Specs {
		vs, ok := spec.(*ast.ValueSpec)
		if !ok {
			continue
		}
		for i, name := range vs.Names {
			if i < len(vs.Values) {
				if lit, ok := w.p.resolveString(vs.Values[i], scope.consts); ok {
					scope.consts[name.Name] = lit
				}
			}
		}
	}
	w.scanExpr(s, scope)
}

// scanExpr finds registration calls inside any expression or statement.
func (w *routeWalker) scanExpr(n ast.Node, scope *routeScope) {
	if n == nil {
		return
	}
	ast.Inspect(n, func(node ast.Node) bool {
		if node == nil {
			return true
		}
		if w.consumed[node] {
			return false
		}
		switch x := node.(type) {
		case *ast.CallExpr:
			w.tryRouteCall(x, scope)
		case *ast.ExprStmt:
			w.noteUse(x.X, scope)
		}
		return true
	})
}

// noteUse handles statement-level `X.Use(mw)` accumulation; route calls
// inside the same statement are caught by the CallExpr visit.
func (w *routeWalker) noteUse(e ast.Expr, scope *routeScope) {
	call, ok := e.(*ast.CallExpr)
	if !ok {
		return
	}
	sel, ok := call.Fun.(*ast.SelectorExpr)
	if !ok {
		return
	}
	if sel.Sel.Name == "Use" {
		if id, ok := sel.X.(*ast.Ident); ok {
			scope.mw[id.Name] = append(scope.mw[id.Name], scope.mwNames(call.Args)...)
		}
	}
}

// tryRouteCall dispatches on the callee selector shape.
func (w *routeWalker) tryRouteCall(call *ast.CallExpr, scope *routeScope) {
	sel, ok := call.Fun.(*ast.SelectorExpr)
	if !ok {
		return
	}
	name := sel.Sel.Name

	// gorilla verb chain: <route call>.Methods("GET", ...)
	if name == "Methods" {
		if inner, ok := sel.X.(*ast.CallExpr); ok {
			var verbs []string
			for _, a := range call.Args {
				if lit, ok := scope.stringOf(a); ok && verbWords[strings.ToUpper(lit)] {
					verbs = append(verbs, strings.ToUpper(lit))
				}
			}
			w.routeFromInner(inner, scope, verbs)
			w.consumed[inner] = true
		}
		return
	}

	// gorilla path chain: r.Path("/p")(.Methods(...)).Handler(h)
	if (name == "Handler" || name == "HandlerFunc") && isCall(sel.X) {
		w.routeFromChain(call, scope)
		return
	}

	if verbs, isVerb := httpVerbs[name]; isVerb && len(call.Args) >= 1 {
		w.verbCall(call, scope, verbs)
		return
	}

	switch name {
	case "Handle", "HandleFunc":
		w.handleCall(call, scope)
	case "Method", "MethodFunc", "Add":
		w.methodCall(call, scope)
	case "Mount":
		w.mountCall(call, scope)
	case "Route":
		w.routeCall(call, scope)
	}
}

func isCall(e ast.Expr) bool {
	_, ok := e.(*ast.CallExpr)
	return ok
}

// gated reports whether a receiver-agnostic registration shape is worth
// trusting: the file imports a web framework, or the receiver is a
// scope-bound router variable.
func (w *routeWalker) gated(scope *routeScope, recv ast.Expr) bool {
	if w.fw != "" {
		return true
	}
	if id, ok := recv.(*ast.Ident); ok {
		_, isRouter := scope.prefix[id.Name]
		_, hasMW := scope.mw[id.Name]
		return isRouter || hasMW
	}
	return false
}

// verbCall covers chi r.Get / gin group.POST / echo e.GET shapes:
// first arg is the path, later args are middleware plus the handler.
func (w *routeWalker) verbCall(call *ast.CallExpr, scope *routeScope, verb string) {
	sel := call.Fun.(*ast.SelectorExpr)
	if !w.gated(scope, sel.X) {
		return
	}
	path, ok := w.p.resolveString(call.Args[0], scope.consts)
	if !ok || path == "" {
		return
	}
	prefix, mws := scope.routerCtx(sel.X)
	template := canonicalTemplate(joinPaths(prefix, path))
	var handler ast.Expr
	if len(call.Args) > 1 {
		handler = call.Args[len(call.Args)-1]
		mws = append(mws, w.wrapperHints(call.Args[1:len(call.Args)-1])...)
	}
	mws, handler = w.unwrapHandler(handler, mws)
	w.emit(call, verb, template, handler, mws)
}

// handleCall covers http.HandleFunc("/p", h), mux.HandleFunc, and gin's
// Handle(method, path, ...). The first argument decides which.
func (w *routeWalker) handleCall(call *ast.CallExpr, scope *routeScope) {
	if len(call.Args) < 2 {
		return
	}
	sel := call.Fun.(*ast.SelectorExpr)
	if !w.gated(scope, sel.X) {
		return
	}
	arg0, ok := w.p.resolveString(call.Args[0], scope.consts)
	if !ok {
		return
	}
	verb := "ANY"
	path := arg0
	handlerIdx := 1
	if m, rest, found := strings.Cut(arg0, " "); found && verbWords[strings.ToUpper(m)] {
		// Go 1.22 ServeMux "METHOD /path" pattern.
		verb = strings.ToUpper(m)
		path = rest
	} else if verbWords[strings.ToUpper(arg0)] && len(call.Args) >= 3 {
		// gin-style Handle("GET", "/path", h).
		if lit, ok := w.p.resolveString(call.Args[1], scope.consts); ok {
			verb = strings.ToUpper(arg0)
			path = lit
			handlerIdx = 2
		}
	}
	if !strings.HasPrefix(path, "/") {
		return
	}
	prefix, mws := scope.routerCtx(sel.X)
	template := canonicalTemplate(joinPaths(prefix, path))
	var handler ast.Expr
	if len(call.Args) > handlerIdx {
		handler = call.Args[len(call.Args)-1]
		mws = append(mws, w.wrapperHints(call.Args[handlerIdx:len(call.Args)-1])...)
	}
	mws, handler = w.unwrapHandler(handler, mws)
	w.emit(call, verb, template, handler, mws)
}

// methodCall covers chi r.Method("GET", "/p", h), r.MethodFunc, and
// echo e.Add("GET", "/p", h).
func (w *routeWalker) methodCall(call *ast.CallExpr, scope *routeScope) {
	if len(call.Args) < 3 {
		return
	}
	sel := call.Fun.(*ast.SelectorExpr)
	if !w.gated(scope, sel.X) {
		return
	}
	m, ok := w.p.resolveString(call.Args[0], scope.consts)
	if !ok || !verbWords[strings.ToUpper(m)] {
		return
	}
	path, ok := w.p.resolveString(call.Args[1], scope.consts)
	if !ok {
		return
	}
	prefix, mws := scope.routerCtx(sel.X)
	template := canonicalTemplate(joinPaths(prefix, path))
	handler := call.Args[len(call.Args)-1]
	mws = append(mws, w.wrapperHints(call.Args[2:len(call.Args)-1])...)
	mws, handler = w.unwrapHandler(handler, mws)
	w.emit(call, strings.ToUpper(m), template, handler, mws)
}

// mountCall covers chi r.Mount("/p", sub): the subtree is a surface entry
// even though its inner routes live elsewhere.
func (w *routeWalker) mountCall(call *ast.CallExpr, scope *routeScope) {
	if len(call.Args) < 2 {
		return
	}
	sel := call.Fun.(*ast.SelectorExpr)
	if !w.gated(scope, sel.X) {
		return
	}
	path, ok := w.p.resolveString(call.Args[0], scope.consts)
	if !ok {
		return
	}
	prefix, mws := scope.routerCtx(sel.X)
	template := canonicalTemplate(joinPaths(prefix, path))
	mws, handler := w.unwrapHandler(call.Args[1], mws)
	w.emit(call, "ANY", template, handler, mws)
}

// routeCall covers chi r.Route("/p", func(r chi.Router) { ... }): the
// sub-router's registrations join under the prefix. A non-literal
// function still leaves an ANY record for the mount point.
func (w *routeWalker) routeCall(call *ast.CallExpr, scope *routeScope) {
	if len(call.Args) < 2 {
		return
	}
	sel := call.Fun.(*ast.SelectorExpr)
	if !w.gated(scope, sel.X) {
		return
	}
	path, ok := w.p.resolveString(call.Args[0], scope.consts)
	if !ok {
		return
	}
	prefix, mws := scope.routerCtx(sel.X)
	template := joinPaths(prefix, path)
	if lit, ok := call.Args[1].(*ast.FuncLit); ok && lit.Body != nil {
		child := scope.child()
		if len(lit.Type.Params.List) > 0 && len(lit.Type.Params.List[0].Names) > 0 {
			inner := lit.Type.Params.List[0].Names[0].Name
			child.prefix[inner] = template
			child.mw[inner] = mws
		}
		w.consumed[lit] = true
		w.walkBlock(lit.Body, child)
		return
	}
	mws, handler := w.unwrapHandler(call.Args[1], mws)
	w.emit(call, "ANY", canonicalTemplate(template), handler, mws)
}

// routeFromInner emits the call under a .Methods(...) chain with the
// listed verbs.
func (w *routeWalker) routeFromInner(inner *ast.CallExpr, scope *routeScope, verbs []string) {
	sel, ok := inner.Fun.(*ast.SelectorExpr)
	if !ok || len(verbs) == 0 {
		return
	}
	name := sel.Sel.Name
	switch name {
	case "Handle", "HandleFunc":
	default:
		return
	}
	if len(inner.Args) < 2 {
		return
	}
	arg0, _ := w.p.resolveString(inner.Args[0], scope.consts)
	if !w.gated(scope, sel.X) {
		return
	}
	path := arg0
	if m, rest, found := strings.Cut(arg0, " "); found && verbWords[strings.ToUpper(m)] {
		path = rest
	}
	if !strings.HasPrefix(path, "/") {
		return
	}
	prefix, mws := scope.routerCtx(sel.X)
	template := canonicalTemplate(joinPaths(prefix, path))
	handler := inner.Args[len(inner.Args)-1]
	mws, handler = w.unwrapHandler(handler, mws)
	for _, verb := range verbs {
		w.emit(inner, verb, template, handler, mws)
	}
}

// routeFromChain handles gorilla chains ending in .Handler/.HandlerFunc:
// r.Path("/p").Methods("GET").Handler(h) in any order.
func (w *routeWalker) routeFromChain(call *ast.CallExpr, scope *routeScope) {
	sel := call.Fun.(*ast.SelectorExpr)
	var path string
	var verbs []string
	cur := sel.X
	for {
		inner, ok := cur.(*ast.CallExpr)
		if !ok {
			break // reached the router expression
		}
		isel, ok := inner.Fun.(*ast.SelectorExpr)
		if !ok {
			return
		}
		switch isel.Sel.Name {
		case "Path", "PathPrefix":
			if len(inner.Args) > 0 {
				if lit, ok := w.p.resolveString(inner.Args[0], scope.consts); ok {
					path = lit
				}
			}
		case "Methods":
			for _, a := range inner.Args {
				if lit, ok := scope.stringOf(a); ok && verbWords[strings.ToUpper(lit)] {
					verbs = append(verbs, strings.ToUpper(lit))
				}
			}
		default:
			// Other gorilla chain builders (Host, Schemes, Queries,
			// Headers, Name, MatcherFunc, ...) refine the route
			// without changing its template; keep walking.
			if !gorillaChainPassthrough[isel.Sel.Name] {
				return
			}
		}
		cur = isel.X
	}
	if path == "" || !w.gated(scope, cur) {
		return
	}
	if len(verbs) == 0 {
		verbs = []string{"ANY"}
	}
	prefix, mws := scope.routerCtx(cur)
	template := canonicalTemplate(joinPaths(prefix, path))
	var handler ast.Expr
	if len(call.Args) > 0 {
		handler = call.Args[0]
	}
	mws, handler = w.unwrapHandler(handler, mws)
	for _, verb := range verbs {
		w.emit(call, verb, template, handler, mws)
	}
}

// wrapperHints names middleware passed between path and handler args
// (gin-style variadic handlers).
func (w *routeWalker) wrapperHints(args []ast.Expr) []string {
	var names []string
	for _, a := range args {
		if n := exprName(a); n != "" && n != "func" {
			names = append(names, n)
		}
	}
	return names
}

// unwrapHandler walks wrapper calls like auth(hello) or
// mw.Chain(inner): each wrapper name is an auth hint, the innermost
// argument is the handler.
func (w *routeWalker) unwrapHandler(e ast.Expr, mws []string) ([]string, ast.Expr) {
	for {
		call, ok := e.(*ast.CallExpr)
		if !ok || len(call.Args) == 0 {
			return mws, e
		}
		if n := exprName(call.Fun); n != "" {
			mws = append(mws, n)
		}
		e = call.Args[len(call.Args)-1]
	}
}

// handlerSymbol resolves the handler expression to a symbol-table name.
func (w *routeWalker) handlerSymbol(e ast.Expr) string {
	switch t := e.(type) {
	case *ast.Ident:
		return t.Name
	case *ast.SelectorExpr:
		cands := w.p.methods[t.Sel.Name]
		if len(cands) == 1 {
			return cands[0].recv + "." + t.Sel.Name
		}
		return t.Sel.Name
	case *ast.CallExpr:
		if len(t.Args) > 0 {
			return w.handlerSymbol(t.Args[len(t.Args)-1])
		}
	case *ast.ParenExpr:
		return w.handlerSymbol(t.X)
	case *ast.StarExpr:
		return w.handlerSymbol(t.X)
	case *ast.IndexExpr:
		return w.handlerSymbol(t.X)
	}
	return ""
}

func (w *routeWalker) emit(call ast.Node, verb, template string, handler ast.Expr, mws []string) {
	symbol := w.enclosing
	if handler != nil {
		if s := w.handlerSymbol(handler); s != "" {
			symbol = s
		}
	}
	attrs := map[string]any{
		"method":    verb,
		"template":  template,
		"framework": w.fw,
	}
	if len(mws) > 0 {
		attrs["auth_hint"] = strings.Join(mws, ",")
	}
	w.out = append(w.out, record{
		Kind:      "route",
		ID:        verb + " " + template,
		File:      w.file,
		Line:      w.p.lineOf(call),
		Symbol:    symbol,
		Lang:      "go",
		Extractor: extractorName,
		Attrs:     attrs,
	})
}

// ---- template canonicalization -------------------------------------------

var (
	angleParam = regexp.MustCompile(`^<(?:[^:<>]+:)?([^<>]+)>$`)
	curlyParam = regexp.MustCompile(`^\{([^}:]+?)(\.\.\.)?(?::[^}]*)?\}$`)
)

// canonicalTemplate normalizes router-specific parameter syntax to the
// {name} canonical form (KTD5): :id, *wild, <id>, {id:regex}, {id...},
// and the Go 1.22 {$} exact-match marker.
func canonicalTemplate(raw string) string {
	if raw == "" {
		return raw
	}
	segs := strings.Split(raw, "/")
	var out []string
	for _, seg := range segs {
		switch {
		case seg == "":
			out = append(out, seg)
		case seg == "{$}":
			// Go 1.22 exact-match marker: the trailing slash is the
			// surface, so the segment collapses to empty, not away.
			out = append(out, "")
		case strings.HasPrefix(seg, ":"):
			out = append(out, "{"+seg[1:]+"}")
		case strings.HasPrefix(seg, "*"):
			out = append(out, "{"+seg[1:]+"}")
		default:
			if m := angleParam.FindStringSubmatch(seg); m != nil {
				out = append(out, "{"+m[1]+"}")
			} else if m := curlyParam.FindStringSubmatch(seg); m != nil {
				out = append(out, "{"+m[1]+"}")
			} else {
				out = append(out, seg)
			}
		}
	}
	return strings.Join(out, "/")
}

// joinPaths combines a router prefix and a local path with one slash.
func joinPaths(prefix, p string) string {
	if prefix == "" {
		return p
	}
	if p == "" {
		return prefix
	}
	return strings.TrimRight(prefix, "/") + "/" + strings.TrimLeft(p, "/")
}
