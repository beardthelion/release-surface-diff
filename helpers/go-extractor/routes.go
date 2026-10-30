package main

import (
	"go/ast"
	"go/token"
	"regexp"
	"strconv"
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

var verbWords = func() map[string]bool {
	m := make(map[string]bool, len(httpVerbs))
	for _, v := range httpVerbs {
		m[v] = true
	}
	return m
}()

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

// routerVal is one resolved (prefix, middleware) pair a receiver can
// carry. An ident mounted under several prefixes produces one route per
// value. unknown marks a chain segment that failed to resolve (dynamic
// path args like "/"+route): registrations under it are suppressed
// since their ids would be wrong.
type routerVal struct {
	prefix  string
	mws     []string
	unknown bool
}

func dedupeVals(vals []routerVal) []routerVal {
	seen := map[string]bool{}
	out := vals[:0]
	for _, v := range vals {
		key := v.prefix + "\x00" + strings.Join(v.mws, ",") +
			"\x00" + strconv.FormatBool(v.unknown)
		if seen[key] {
			continue
		}
		seen[key] = true
		out = append(out, v)
	}
	return out
}

func appendDedupeVals(dst, src []routerVal) []routerVal {
	return dedupeVals(append(dst, src...))
}

// routeScope tracks per-function registration state: variables bound to a
// path prefix (Group/Route results), middleware accumulated on a router
// variable via .Use/.With/Group extras, local string constants, idents
// mounted elsewhere (a router var handed to r.Mount or fed into a helper
// function), field names assigned a router (x.Field = r inside a Route
// scope, coder's api.ExperimentalHandler), idents proven to be
// request/query accessors, and the enclosing function's parameter types.
type routeScope struct {
	pkg        *pkgCtx
	prefix     map[string]string
	mw         map[string][]string
	consts     map[string]string
	mounts     map[string][]routerVal
	fields     map[string][]routerVal
	denied     map[string]bool
	paramTypes map[string]string
	known      bool // false under an unresolvable Route/Group/Mount prefix
}

func newRouteScope(p *pkgCtx, paramTypes map[string]string) *routeScope {
	return &routeScope{
		pkg:        p,
		prefix:     map[string]string{},
		mw:         map[string][]string{},
		consts:     map[string]string{},
		mounts:     map[string][]routerVal{},
		fields:     map[string][]routerVal{},
		denied:     map[string]bool{},
		paramTypes: paramTypes,
		known:      true,
	}
}

func (s *routeScope) child() *routeScope {
	c := newRouteScope(s.pkg, s.paramTypes)
	for k, v := range s.prefix {
		c.prefix[k] = v
	}
	for k, v := range s.mw {
		c.mw[k] = append([]string(nil), v...)
	}
	for k, v := range s.consts {
		c.consts[k] = v
	}
	for k, v := range s.mounts {
		c.mounts[k] = v
	}
	for k, v := range s.fields {
		c.fields[k] = v
	}
	for k, v := range s.denied {
		c.denied[k] = v
	}
	c.known = s.known
	return c
}

// routerCtxs resolves a receiver expression to its (prefix, middleware)
// values: an Ident looks up scope bindings (mount registrations replace
// the raw prefix, since a mounted router is only reachable through its
// mount points); a chained With/Group/Route/Use/Mount call folds its own
// arguments in; a router-constructor call yields the empty prefix.
func (s *routeScope) routerCtxs(e ast.Expr) []routerVal {
	switch t := e.(type) {
	case *ast.Ident:
		if vs, ok := s.mounts[t.Name]; ok {
			return dedupeVals(vs)
		}
		if _, ok := s.prefix[t.Name]; ok {
			return []routerVal{{s.prefix[t.Name],
				append([]string(nil), s.mw[t.Name]...), false}}
		}
		if _, ok := s.mw[t.Name]; ok {
			return []routerVal{{"", append([]string(nil), s.mw[t.Name]...), false}}
		}
		if isRouterType(s.paramTypes[t.Name]) {
			return []routerVal{{"", nil, false}}
		}
		return nil
	case *ast.SelectorExpr:
		// x.Field resolves through field bindings: api.ExperimentalHandler
		// carries the prefix of the Route scope it was assigned in.
		if vs, ok := s.fields[t.Sel.Name]; ok {
			return dedupeVals(vs)
		}
		return nil
	case *ast.ParenExpr:
		return s.routerCtxs(t.X)
	case *ast.CallExpr:
		sel, ok := t.Fun.(*ast.SelectorExpr)
		if !ok {
			return nil
		}
		bases := s.routerCtxs(sel.X)
		if len(bases) == 0 {
			if isRouterCtorCall(t) {
				return []routerVal{{"", nil, false}}
			}
			return nil
		}
		out := make([]routerVal, 0, len(bases))
		for _, b := range bases {
			switch sel.Sel.Name {
			case "With", "Use":
				b.mws = append(append([]string(nil), b.mws...),
					s.mwNames(t.Args)...)
			case "Group", "Route", "Mount":
				if len(t.Args) > 0 {
					if lit, ok := s.stringOf(t.Args[0]); ok {
						b.prefix = joinPaths(b.prefix, lit)
					}
				}
				if sel.Sel.Name == "Group" && len(t.Args) > 1 {
					b.mws = append(b.mws, s.mwNames(t.Args[1:])...)
				}
			}
			out = append(out, b)
		}
		return out
	}
	return nil
}

// routerCtx is the single-prefix shorthand used where only the first
// resolved value matters.
func (s *routeScope) routerCtx(e ast.Expr) (string, []string) {
	vals := s.routerCtxs(e)
	if len(vals) == 0 {
		return "", nil
	}
	return vals[0].prefix, vals[0].mws
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
	if b, ok := e.(*ast.BinaryExpr); ok && b.Op == token.ADD {
		l, lok := s.stringOf(b.X)
		r, rok := s.stringOf(b.Y)
		if lok && rok {
			return l + r, true
		}
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

// ---- receiver typing -----------------------------------------------------

// deniedSelNames mark a receiver chain as a request/header/query accessor,
// never a router: req.Header.Get, r.URL.Query().Get, vals.Get.
var deniedSelNames = map[string]bool{
	"Header": true, "Query": true, "Form": true, "URL": true,
	"Values": true, "PostForm": true,
}

// typeExprName renders a parameter type the way isRouterType and
// deniedRecvType match it: "chi.Router", "*http.Request", "url.Values".
func typeExprName(e ast.Expr) string {
	switch t := e.(type) {
	case *ast.Ident:
		return t.Name
	case *ast.StarExpr:
		return "*" + typeExprName(t.X)
	case *ast.SelectorExpr:
		if x := typeExprName(t.X); x != "" {
			return x + "." + t.Sel.Name
		}
		return t.Sel.Name
	case *ast.IndexExpr:
		return typeExprName(t.X)
	case *ast.IndexListExpr:
		return typeExprName(t.X)
	}
	return ""
}

// paramTypeMap names the enclosing function's parameter types by
// parameter name; unnamed parameters contribute nothing.
func paramTypeMap(fd *ast.FuncDecl) map[string]string {
	m := map[string]string{}
	if fd.Type == nil || fd.Type.Params == nil {
		return m
	}
	for _, field := range fd.Type.Params.List {
		tn := typeExprName(field.Type)
		for _, n := range field.Names {
			m[n.Name] = tn
		}
	}
	return m
}

// isRouterType reports whether a parameter type names a router:
// chi.Router, *chi.Mux, *mux.Router, *gin.Engine, *gin.RouterGroup,
// *echo.Echo, *echo.Group, http.ServeMux, or any type whose name carries
// "Router" or "Mux".
func isRouterType(t string) bool {
	base := strings.TrimPrefix(t, "*")
	switch base {
	case "chi.Router", "chi.Mux", "mux.Router", "gin.Engine",
		"gin.RouterGroup", "echo.Echo", "echo.Group", "http.ServeMux":
		return true
	}
	name := base
	if i := strings.LastIndex(name, "."); i >= 0 {
		name = name[i+1:]
	}
	return strings.Contains(name, "Router") || strings.Contains(name, "Mux")
}

// deniedRecvType reports whether a parameter type names a request or
// accessor type: http.Header, url.Values, *http.Request, url.URL.
func deniedRecvType(t string) bool {
	base := strings.TrimPrefix(t, "*")
	switch base {
	case "http.Header", "url.Values", "http.Request", "url.URL":
		return true
	}
	name := base
	if i := strings.LastIndex(name, "."); i >= 0 {
		name = name[i+1:]
	}
	return deniedSelNames[name] || name == "Request"
}

// isRouterCtorCall reports whether a call constructs a router-ish value:
// chi.NewRouter, mux.NewRouter, http.NewServeMux, gin.Default, gin.New,
// echo.New.
func isRouterCtorCall(call *ast.CallExpr) bool {
	var name string
	switch fn := call.Fun.(type) {
	case *ast.SelectorExpr:
		name = fn.Sel.Name
	case *ast.Ident:
		name = fn.Name
	default:
		return false
	}
	switch name {
	case "New", "Default", "NewRouter", "NewMux", "NewServeMux",
		"NewEngine", "NewGroup":
		return true
	}
	return strings.Contains(name, "Router") || strings.Contains(name, "Mux")
}

// ---- statement walking ---------------------------------------------------

// paramFeed is one caller's contribution to a callee's router parameter.
// ident args are re-resolved through the mounts table at seed time so a
// Mount that follows the call in statement order still wins over the
// at-call prefix.
type paramFeed struct {
	caller *ast.FuncDecl
	ident  string
	vals   []routerVal
}

// routeIndex is the package-level collection result: idents handed to
// r.Mount (per enclosing function, or package-wide for package-level
// router vars), field names assigned a router inside a Route scope
// (coder's api.ExperimentalHandler = r), and the prefixes callers push
// into each function's router-typed parameters (coder-style
// registerXRoutes(r, ...) helpers).
type routeIndex struct {
	funcMounts  map[*ast.FuncDecl]map[string][]routerVal
	pkgMounts   map[string][]routerVal
	fieldMounts map[string][]routerVal
	paramFeeds  map[*ast.FuncDecl]map[string][]paramFeed
}

func newRouteIndex() *routeIndex {
	return &routeIndex{
		funcMounts:  map[*ast.FuncDecl]map[string][]routerVal{},
		pkgMounts:   map[string][]routerVal{},
		fieldMounts: map[string][]routerVal{},
		paramFeeds:  map[*ast.FuncDecl]map[string][]paramFeed{},
	}
}

// merge folds another package's collection results in: FuncDecl-keyed
// tables cannot collide across packages, and field/mount names merge by
// union so a handler built in one package is reachable from another
// (coderd's api.ExperimentalHandler read in enterprise/coderd).
func (idx *routeIndex) merge(other *routeIndex) {
	for fd, m := range other.funcMounts {
		dst := idx.funcMounts[fd]
		if dst == nil {
			dst = map[string][]routerVal{}
			idx.funcMounts[fd] = dst
		}
		for name, vals := range m {
			dst[name] = appendDedupeVals(dst[name], vals)
		}
	}
	for name, vals := range other.pkgMounts {
		idx.pkgMounts[name] = appendDedupeVals(idx.pkgMounts[name], vals)
	}
	for name, vals := range other.fieldMounts {
		idx.fieldMounts[name] = appendDedupeVals(idx.fieldMounts[name], vals)
	}
	for fd, m := range other.paramFeeds {
		dst := idx.paramFeeds[fd]
		if dst == nil {
			dst = map[string][]paramFeed{}
			idx.paramFeeds[fd] = dst
		}
		for name, feeds := range m {
			dst[name] = append(dst[name], feeds...)
		}
	}
}

// seed primes a function's scope with the collection results that apply
// to it: package-level mounts, its own local mounts, field bindings, and
// the prefixes its callers feed into its router parameters.
func (idx *routeIndex) seed(fd *ast.FuncDecl, scope *routeScope) {
	for name, vals := range idx.pkgMounts {
		scope.mounts[name] = dedupeVals(vals)
	}
	for name, vals := range idx.funcMounts[fd] {
		scope.mounts[name] = dedupeVals(vals)
	}
	for name, vals := range idx.fieldMounts {
		scope.fields[name] = dedupeVals(vals)
	}
	for name, feeds := range idx.paramFeeds[fd] {
		var out []routerVal
		for _, f := range feeds {
			mounts := idx.funcMounts[f.caller][f.ident]
			if len(mounts) == 0 && f.ident != "" {
				mounts = idx.pkgMounts[f.ident]
			}
			if len(mounts) > 0 {
				out = append(out, mounts...)
				continue
			}
			out = append(out, f.vals...)
		}
		if len(out) > 0 {
			scope.mounts[name] = dedupeVals(out)
		}
	}
}

// collectRouteBindings runs the statement walk in collection mode (no
// records emitted) so mounts and cross-function router arguments are
// known before extraction. Two rounds: a Mount or helper call that
// follows its router's uses in statement order only becomes visible to
// earlier sites once the index is seeded from the first round.
func collectRouteBindings(p *pkgCtx) *routeIndex {
	idx := newRouteIndex()
	for round := 0; round < 2; round++ {
		for _, f := range p.files {
			for _, decl := range f.Decls {
				fd, ok := decl.(*ast.FuncDecl)
				if !ok || fd.Body == nil {
					continue
				}
				w := &routeWalker{
					p: p, f: f, idx: idx, collect: true, fd: fd,
					enclosing: funcSymbol(fd),
					consumed:  map[ast.Node]bool{},
					seen:      map[string]bool{},
				}
				scope := newRouteScope(p, paramTypeMap(fd))
				idx.seed(fd, scope)
				w.walkBlock(fd.Body, scope)
			}
		}
	}
	return idx
}

// extractRoutes walks each function body in statement order so .Use calls
// that precede a registration are visible on the chain.
func extractRoutes(p *pkgCtx) []record {
	return extractRoutesIdx(p, collectRouteBindings(p))
}

// extractRoutesIdx is extractRoutes with a caller-built route index;
// extract() passes the merged cross-package index so field bindings and
// mounts found in one package resolve in another.
func extractRoutesIdx(p *pkgCtx, idx *routeIndex) []record {
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
				p: p, f: f, file: file, fw: fw, idx: idx, fd: fd,
				enclosing: funcSymbol(fd),
				consumed:  map[ast.Node]bool{},
				seen:      map[string]bool{},
			}
			scope := newRouteScope(p, paramTypeMap(fd))
			idx.seed(fd, scope)
			w.walkBlock(fd.Body, scope)
			out = append(out, w.out...)
		}
	}
	return out
}

type routeWalker struct {
	p         *pkgCtx
	f         *ast.File
	fd        *ast.FuncDecl
	file      string
	fw        string
	idx       *routeIndex
	collect   bool
	enclosing string
	consumed  map[ast.Node]bool
	seen      map[string]bool
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
		if w.walkLiteralRange(s, scope) {
			break
		}
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

// walkLiteralRange unrolls `for _, v := range []string{lit, ...}` loops so
// registrations that concat the loop var into a path ("/"+route) resolve
// to real mounts: one walk per literal element.
func (w *routeWalker) walkLiteralRange(s *ast.RangeStmt, scope *routeScope) bool {
	cl, ok := s.X.(*ast.CompositeLit)
	if !ok {
		return false
	}
	var names []string
	for _, e := range []ast.Expr{s.Key, s.Value} {
		if id, ok := e.(*ast.Ident); ok && id.Name != "_" {
			names = append(names, id.Name)
		} else {
			names = append(names, "")
		}
	}
	var lits []string
	for _, e := range cl.Elts {
		lit, ok := stringLit(e)
		if !ok {
			return false
		}
		lits = append(lits, lit)
	}
	if len(lits) == 0 {
		return false
	}
	for _, lit := range lits {
		child := scope.child()
		for _, n := range names {
			if n != "" {
				child.consts[n] = lit
			}
		}
		w.walkBlock(s.Body, child)
	}
	return true
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
			// x.Field = routerExpr: the field name becomes a router
			// binding carrying the enclosing prefix (coder's
			// api.ExperimentalHandler = r inside
			// r.Route("/api/experimental", ...)).
			if sel, ok := s.Lhs[i].(*ast.SelectorExpr); ok {
				if vals := scope.routerCtxs(rhs); len(vals) > 0 {
					scope.fields[sel.Sel.Name] =
						appendDedupeVals(scope.fields[sel.Sel.Name], vals)
					if w.idx != nil {
						w.idx.fieldMounts[sel.Sel.Name] =
							appendDedupeVals(
								w.idx.fieldMounts[sel.Sel.Name], vals)
					}
				}
			}
			continue
		}
		if lit, ok := w.p.resolveString(rhs, scope.consts); ok {
			scope.consts[lhs.Name] = lit
			continue
		}
		if w.recvDenied(scope, rhs) {
			// x := r.URL.Query() (or a copy of a request-typed param):
			// verbs on x are header/query access, not routes.
			scope.denied[lhs.Name] = true
			continue
		}
		call, ok := rhs.(*ast.CallExpr)
		if !ok {
			continue
		}
		bound := false
		if sel, ok := call.Fun.(*ast.SelectorExpr); ok {
			switch sel.Sel.Name {
			case "Group", "Route", "Mount":
				prefix, mws := scope.routerCtx(sel.X)
				if len(call.Args) > 0 {
					if lit, ok := scope.stringOf(call.Args[0]); ok {
						prefix = joinPaths(prefix, lit)
					}
					if sel.Sel.Name == "Group" && len(call.Args) > 1 {
						mws = append(mws, scope.mwNames(call.Args[1:])...)
					}
				}
				scope.prefix[lhs.Name] = prefix
				scope.mw[lhs.Name] = mws
				bound = true
			case "With":
				prefix, mws := scope.routerCtx(sel.X)
				mws = append(mws, scope.mwNames(call.Args)...)
				scope.prefix[lhs.Name] = prefix
				scope.mw[lhs.Name] = mws
				bound = true
			}
		}
		// x := chi.NewRouter() / http.NewServeMux() / echo.New():
		// a bound router with no prefix yet.
		if !bound && isRouterCtorCall(call) {
			if _, ok := scope.prefix[lhs.Name]; !ok {
				scope.prefix[lhs.Name] = ""
			}
			if _, ok := scope.mw[lhs.Name]; !ok {
				scope.mw[lhs.Name] = nil
			}
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
					continue
				}
				if w.recvDenied(scope, vs.Values[i]) {
					scope.denied[name.Name] = true
					continue
				}
				if call, ok := vs.Values[i].(*ast.CallExpr); ok &&
					isRouterCtorCall(call) {
					if _, ok := scope.prefix[name.Name]; !ok {
						scope.prefix[name.Name] = ""
					}
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
	if w.collect {
		w.collectCall(call, scope)
	}
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
	case "Group":
		w.groupCall(call, scope)
	}
}

func isCall(e ast.Expr) bool {
	_, ok := e.(*ast.CallExpr)
	return ok
}

// gated reports whether a registration call is worth trusting: the
// receiver must resolve to a router and must not look like a
// header/query/request accessor. method is the call's selector name,
// needed so net/http package functions only register through
// Handle/HandleFunc (http.Get is a client call, not a route).
func (w *routeWalker) gated(scope *routeScope, recv ast.Expr, method string) bool {
	if w.recvDenied(scope, recv) {
		return false
	}
	return w.recvIsRouter(scope, recv, method)
}

// recvDenied reports whether the receiver expression chain marks a
// request/header/query accessor: a denied selector anywhere in the chain
// (r.Header, req.URL.Query(), x.Form), an ident bound from such a chain,
// or an ident whose parameter type is a request/accessor type.
func (w *routeWalker) recvDenied(scope *routeScope, e ast.Expr) bool {
	switch t := e.(type) {
	case *ast.Ident:
		if scope.denied[t.Name] {
			return true
		}
		return deniedRecvType(scope.paramTypes[t.Name])
	case *ast.SelectorExpr:
		if deniedSelNames[t.Sel.Name] {
			return true
		}
		return w.recvDenied(scope, t.X)
	case *ast.CallExpr:
		return w.recvDenied(scope, t.Fun)
	case *ast.ParenExpr:
		return w.recvDenied(scope, t.X)
	case *ast.StarExpr:
		return w.recvDenied(scope, t.X)
	case *ast.IndexExpr:
		return w.recvDenied(scope, t.X)
	}
	return false
}

// recvIsRouter reports whether the receiver resolves to a router: a
// scope-bound router variable (Group/Route/With chains, Use, ctor
// bindings, mount feeds), a router-typed parameter, a selector on such a
// receiver (or a field named *Router/*Mux), a chained call that produces
// a router, or the net/http package itself for Handle/HandleFunc.
func (w *routeWalker) recvIsRouter(scope *routeScope, e ast.Expr, method string) bool {
	switch t := e.(type) {
	case *ast.Ident:
		if _, ok := scope.mounts[t.Name]; ok {
			return true
		}
		if _, ok := scope.prefix[t.Name]; ok {
			return true
		}
		if _, ok := scope.mw[t.Name]; ok {
			return true
		}
		if isRouterType(scope.paramTypes[t.Name]) {
			return true
		}
		if path, ok := w.p.imports[w.f][t.Name]; ok {
			// Package-level registration lands on net/http's
			// DefaultServeMux; other package calls (http.Get,
			// url.Parse) are not registrations.
			return path == "net/http" &&
				(method == "Handle" || method == "HandleFunc")
		}
		return false
	case *ast.SelectorExpr:
		n := t.Sel.Name
		if _, ok := scope.fields[n]; ok {
			return true
		}
		if strings.Contains(n, "Router") || strings.Contains(n, "Mux") {
			return true
		}
		return w.recvIsRouter(scope, t.X, method)
	case *ast.CallExpr:
		if isRouterCtorCall(t) {
			return true
		}
		if sel, ok := t.Fun.(*ast.SelectorExpr); ok {
			switch sel.Sel.Name {
			case "Group", "Route", "With", "Use", "Mount":
				return w.recvIsRouter(scope, sel.X, method)
			}
		}
		return false
	case *ast.ParenExpr:
		return w.recvIsRouter(scope, t.X, method)
	}
	return false
}

// emitEach resolves the receiver's router values and emits one record
// per distinct (prefix, middleware) pair.
func (w *routeWalker) emitEach(call ast.Node, verb, path string,
	handler ast.Expr, extraNames []string,
	scope *routeScope, recv ast.Expr) {
	if !scope.known {
		return
	}
	vals := scope.routerCtxs(recv)
	if len(vals) == 0 {
		vals = []routerVal{{"", nil, false}}
	}
	for _, v := range dedupeVals(vals) {
		template := canonicalTemplate(joinPaths(v.prefix, path))
		mws := append(append([]string(nil), v.mws...), extraNames...)
		mws, h := w.unwrapHandler(handler, mws)
		w.emit(call, verb, template, h, mws)
	}
}

// verbCall covers chi r.Get / gin group.POST / echo e.GET shapes:
// first arg is the path, later args are middleware plus the handler. A
// path that does not resolve to a "/" literal is never a plain route
// (it kills req.Header.Get-style false positives).
func (w *routeWalker) verbCall(call *ast.CallExpr, scope *routeScope, verb string) {
	sel := call.Fun.(*ast.SelectorExpr)
	if !w.gated(scope, sel.X, sel.Sel.Name) {
		return
	}
	path, ok := w.p.resolveString(call.Args[0], scope.consts)
	if !ok || !strings.HasPrefix(path, "/") {
		return
	}
	var handler ast.Expr
	var extra []string
	if len(call.Args) > 1 {
		handler = call.Args[len(call.Args)-1]
		extra = w.wrapperHints(call.Args[1 : len(call.Args)-1])
	}
	w.emitEach(call, verb, path, handler, extra, scope, sel.X)
}

// handleCall covers http.HandleFunc("/p", h), mux.HandleFunc, and gin's
// Handle(method, path, ...). The first argument decides which.
func (w *routeWalker) handleCall(call *ast.CallExpr, scope *routeScope) {
	if len(call.Args) < 2 {
		return
	}
	sel := call.Fun.(*ast.SelectorExpr)
	if !w.gated(scope, sel.X, sel.Sel.Name) {
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
	var handler ast.Expr
	var extra []string
	if len(call.Args) > handlerIdx {
		handler = call.Args[len(call.Args)-1]
		extra = w.wrapperHints(call.Args[handlerIdx : len(call.Args)-1])
	}
	w.emitEach(call, verb, path, handler, extra, scope, sel.X)
}

// methodCall covers chi r.Method("GET", "/p", h), r.MethodFunc, and
// echo e.Add("GET", "/p", h).
func (w *routeWalker) methodCall(call *ast.CallExpr, scope *routeScope) {
	if len(call.Args) < 3 {
		return
	}
	sel := call.Fun.(*ast.SelectorExpr)
	if !w.gated(scope, sel.X, sel.Sel.Name) {
		return
	}
	m, ok := w.p.resolveString(call.Args[0], scope.consts)
	if !ok || !verbWords[strings.ToUpper(m)] {
		return
	}
	path, ok := w.p.resolveString(call.Args[1], scope.consts)
	if !ok || !strings.HasPrefix(path, "/") {
		return
	}
	handler := call.Args[len(call.Args)-1]
	extra := w.wrapperHints(call.Args[2 : len(call.Args)-1])
	w.emitEach(call, strings.ToUpper(m), path, handler, extra, scope, sel.X)
}

// mountCall covers chi r.Mount("/p", sub): the mount point is a surface
// entry, an ident argument registers its own routes under the prefix
// (collection pass), and a func literal is walked like a sub-router.
func (w *routeWalker) mountCall(call *ast.CallExpr, scope *routeScope) {
	if len(call.Args) < 2 {
		return
	}
	sel := call.Fun.(*ast.SelectorExpr)
	if !w.gated(scope, sel.X, "Mount") {
		return
	}
	path, ok := w.p.resolveString(call.Args[0], scope.consts)
	if !ok || !strings.HasPrefix(path, "/") {
		w.walkUnknownSubrouter(call.Args[1], scope)
		return
	}
	vals := scope.routerCtxs(sel.X)
	if len(vals) == 0 {
		vals = []routerVal{{"", nil, false}}
	}
	mounted := make([]routerVal, 0, len(vals))
	for _, v := range vals {
		mounted = append(mounted,
			routerVal{joinPaths(v.prefix, path), v.mws, v.unknown})
	}
	mounted = dedupeVals(mounted)
	w.recordMount(scope, mounted, call.Args[1])
	for _, v := range mounted {
		if lit := w.subrouterLit(call.Args[1]); lit != nil {
			w.walkSubrouter(v.prefix, v.mws, lit, scope)
		}
		mws, handler := w.unwrapHandler(call.Args[1],
			append([]string(nil), v.mws...))
		w.emit(call, "ANY", canonicalTemplate(v.prefix), handler, mws)
	}
	w.feedRouterArg(call.Args[1], mounted)
}

// subrouterLit resolves a Route/Mount handler argument to the function
// literal whose body registers sub-routes: a direct func literal, or a
// call to a same-package factory returning func(r <router>) (coder's
// r.Route("/ai/providers", aiProvidersHandler(api, mw))).
func (w *routeWalker) subrouterLit(e ast.Expr) *ast.FuncLit {
	switch t := e.(type) {
	case *ast.FuncLit:
		if t.Body != nil {
			return t
		}
	case *ast.CallExpr:
		return w.routerFactoryLit(t)
	}
	return nil
}

// routerFactoryLit finds the func literal a same-package factory
// function returns, when its declared result is func(router, ...).
// One level only: the factory's own registration body is never
// re-entered through further calls.
func (w *routeWalker) routerFactoryLit(call *ast.CallExpr) *ast.FuncLit {
	id, ok := call.Fun.(*ast.Ident)
	if !ok {
		return nil
	}
	fd, ok := w.p.funcs[id.Name]
	if !ok || fd.Body == nil || fd.Type.Results == nil ||
		len(fd.Type.Results.List) == 0 {
		return nil
	}
	ft, ok := fd.Type.Results.List[0].Type.(*ast.FuncType)
	if !ok || ft.Params == nil || len(ft.Params.List) == 0 {
		return nil
	}
	if !isRouterType(typeExprName(ft.Params.List[0].Type)) {
		return nil
	}
	var lit *ast.FuncLit
	ast.Inspect(fd.Body, func(n ast.Node) bool {
		if lit != nil {
			return false
		}
		ret, ok := n.(*ast.ReturnStmt)
		if !ok {
			return true
		}
		for _, r := range ret.Results {
			if l, ok := r.(*ast.FuncLit); ok && l.Body != nil {
				lit = l
				return false
			}
		}
		return true
	})
	return lit
}

// recordMount registers a Mount argument's router-bound ident under the
// resolved mount prefixes so the ident's own registrations join under
// them. Package-level router vars collect package-wide; locals collect
// per enclosing function.
func (w *routeWalker) recordMount(scope *routeScope, vals []routerVal, arg ast.Expr) {
	if w.idx == nil || len(vals) == 0 {
		return
	}
	id, ok := arg.(*ast.Ident)
	if !ok {
		return
	}
	if w.p.routerVars[id.Name] {
		w.idx.pkgMounts[id.Name] =
			appendDedupeVals(w.idx.pkgMounts[id.Name], vals)
		return
	}
	if len(scope.routerCtxs(arg)) == 0 {
		return
	}
	m := w.idx.funcMounts[w.fd]
	if m == nil {
		m = map[string][]routerVal{}
		w.idx.funcMounts[w.fd] = m
	}
	m[id.Name] = appendDedupeVals(m[id.Name], vals)
}

// collectCall records cross-function router propagation: a call
// name(routerExpr, ...) or x.name(routerExpr, ...) to a function or
// method in this package feeds every resolved (prefix, middleware)
// into the callee's matching parameter. Selector calls match methods by
// name and ignore the receiver (api.registerChatAPIRoutes(r, mw)).
func (w *routeWalker) collectCall(call *ast.CallExpr, scope *routeScope) {
	if w.idx == nil {
		return
	}
	var callees []*ast.FuncDecl
	switch fn := call.Fun.(type) {
	case *ast.Ident:
		if fd, ok := w.p.funcs[fn.Name]; ok {
			callees = append(callees, fd)
		}
	case *ast.SelectorExpr:
		// pkg.Fn is cross-package and out of scope.
		if id, ok := fn.X.(*ast.Ident); ok {
			if _, imported := w.p.imports[w.f][id.Name]; imported {
				return
			}
		}
		for _, md := range w.p.methods[fn.Sel.Name] {
			callees = append(callees, md.decl)
		}
	}
	for _, callee := range callees {
		w.feedParams(call, callee, scope)
	}
}

// feedParams pushes the resolved router values of each call argument
// into the callee's parameter at the same position.
func (w *routeWalker) feedParams(call *ast.CallExpr, callee *ast.FuncDecl,
	scope *routeScope) {
	if callee.Type == nil || callee.Type.Params == nil {
		return
	}
	var names []string
	for _, field := range callee.Type.Params.List {
		if len(field.Names) == 0 {
			names = append(names, "")
			continue
		}
		for _, n := range field.Names {
			names = append(names, n.Name)
		}
	}
	for i, arg := range call.Args {
		if i >= len(names) || names[i] == "" {
			continue
		}
		vals := scope.routerCtxs(arg)
		if len(vals) == 0 {
			continue
		}
		feed := paramFeed{caller: w.fd, vals: vals}
		if aid, ok := arg.(*ast.Ident); ok {
			feed.ident = aid.Name
		}
		m := w.idx.paramFeeds[callee]
		if m == nil {
			m = map[string][]paramFeed{}
			w.idx.paramFeeds[callee] = m
		}
		m[names[i]] = append(m[names[i]], feed)
	}
}

// routeCall covers chi r.Route("/p", func(r chi.Router) { ... }): the
// sub-router's registrations join under the prefix. A non-literal
// function still leaves an ANY record for the mount point.
func (w *routeWalker) routeCall(call *ast.CallExpr, scope *routeScope) {
	if len(call.Args) < 2 {
		return
	}
	sel := call.Fun.(*ast.SelectorExpr)
	if !w.gated(scope, sel.X, "Route") {
		return
	}
	path, ok := w.p.resolveString(call.Args[0], scope.consts)
	if !ok || !strings.HasPrefix(path, "/") {
		w.walkUnknownSubrouter(call.Args[1], scope)
		return
	}
	vals := scope.routerCtxs(sel.X)
	if len(vals) == 0 {
		vals = []routerVal{{"", nil, false}}
	}
	var joined []routerVal
	for _, v := range dedupeVals(vals) {
		template := joinPaths(v.prefix, path)
		joined = append(joined, routerVal{template, v.mws, v.unknown})
		if lit := w.subrouterLit(call.Args[1]); lit != nil {
			w.walkSubrouter(template, v.mws, lit, scope)
			continue
		}
		mws, handler := w.unwrapHandler(call.Args[1],
			append([]string(nil), v.mws...))
		w.emit(call, "ANY", canonicalTemplate(template), handler, mws)
	}
	w.feedRouterArg(call.Args[1], joined)
}

// groupCall covers chi r.Group(func(r chi.Router) { ... }): the literal's
// registrations keep the receiver's prefix and pick up its middleware.
// gin's r.Group("/p", mw...) returns a RouterGroup handled through
// binding and chain resolution instead.
func (w *routeWalker) groupCall(call *ast.CallExpr, scope *routeScope) {
	if len(call.Args) < 1 {
		return
	}
	sel := call.Fun.(*ast.SelectorExpr)
	if !w.gated(scope, sel.X, "Group") {
		return
	}
	lit, ok := call.Args[0].(*ast.FuncLit)
	if !ok || lit.Body == nil {
		return
	}
	extra := scope.mwNames(call.Args[1:])
	vals := scope.routerCtxs(sel.X)
	if len(vals) == 0 {
		vals = []routerVal{{"", nil, false}}
	}
	for _, v := range dedupeVals(vals) {
		mws := append(append([]string(nil), v.mws...), extra...)
		w.walkSubrouter(v.prefix, mws, lit, scope)
	}
}

// walkUnknownSubrouter descends into a Route/Mount func literal whose
// mount path could not be resolved (dynamic args like "/"+route or
// fmt.Sprintf). The body is walked so later literals are not revisited,
// but every emission inside is suppressed: its ids would be wrong.
func (w *routeWalker) walkUnknownSubrouter(e ast.Expr, scope *routeScope) {
	lit := w.subrouterLit(e)
	if lit == nil || lit.Body == nil {
		return
	}
	child := scope.child()
	child.known = false
	if len(lit.Type.Params.List) > 0 &&
		len(lit.Type.Params.List[0].Names) > 0 {
		inner := lit.Type.Params.List[0].Names[0].Name
		child.prefix[inner] = ""
		delete(child.mounts, inner)
	}
	w.consumed[lit] = true
	w.walkBlock(lit.Body, child)
}

// namedRouterFunc resolves a function value argument (ident or selector,
// no call) to a same-package func/method whose first param is router
// typed: r.Route("/p", api.registerRoutes).
func (w *routeWalker) namedRouterFunc(e ast.Expr) *ast.FuncDecl {
	var fd *ast.FuncDecl
	switch t := e.(type) {
	case *ast.Ident:
		fd = w.p.funcs[t.Name]
	case *ast.SelectorExpr:
		if id, ok := t.X.(*ast.Ident); ok {
			if _, imported := w.p.imports[w.f][id.Name]; imported {
				return nil
			}
		}
		for _, md := range w.p.methods[t.Sel.Name] {
			fd = md.decl
			break
		}
	}
	if fd == nil || fd.Body == nil || fd.Type.Params == nil ||
		len(fd.Type.Params.List) == 0 {
		return nil
	}
	if !isRouterType(typeExprName(fd.Type.Params.List[0].Type)) {
		return nil
	}
	return fd
}

// feedRouterArg records a Route/Mount handler argument that is a
// function value naming a same-package router function, so the callee's
// own extraction pass emits records under the joined prefix.
func (w *routeWalker) feedRouterArg(e ast.Expr, vals []routerVal) {
	if w.idx == nil || len(vals) == 0 {
		return
	}
	fd := w.namedRouterFunc(e)
	if fd == nil {
		return
	}
	name := ""
	if len(fd.Type.Params.List[0].Names) > 0 {
		name = fd.Type.Params.List[0].Names[0].Name
	}
	if name == "" {
		return
	}
	m := w.idx.paramFeeds[fd]
	if m == nil {
		m = map[string][]paramFeed{}
		w.idx.paramFeeds[fd] = m
	}
	m[name] = append(m[name], paramFeed{caller: w.fd, vals: vals})
}

// walkSubrouter descends into a Route/Group/Mount function literal with
// its first parameter bound to the joined prefix.
func (w *routeWalker) walkSubrouter(template string, mws []string,
	lit *ast.FuncLit, scope *routeScope) {
	child := scope.child()
	if len(lit.Type.Params.List) > 0 &&
		len(lit.Type.Params.List[0].Names) > 0 {
		inner := lit.Type.Params.List[0].Names[0].Name
		child.prefix[inner] = template
		child.mw[inner] = append([]string(nil), mws...)
		delete(child.mounts, inner)
	}
	w.consumed[lit] = true
	w.walkBlock(lit.Body, child)
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
	if !w.gated(scope, sel.X, name) {
		return
	}
	path := arg0
	if m, rest, found := strings.Cut(arg0, " "); found && verbWords[strings.ToUpper(m)] {
		path = rest
	}
	if !strings.HasPrefix(path, "/") {
		return
	}
	handler := inner.Args[len(inner.Args)-1]
	for _, verb := range verbs {
		w.emitEach(inner, verb, path, handler, nil, scope, sel.X)
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
	if path == "" || !w.gated(scope, cur, sel.Sel.Name) {
		return
	}
	if len(verbs) == 0 {
		verbs = []string{"ANY"}
	}
	var handler ast.Expr
	if len(call.Args) > 0 {
		handler = call.Args[0]
	}
	for _, verb := range verbs {
		w.emitEach(call, verb, path, handler, nil, scope, cur)
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
	if w.collect {
		return
	}
	// "/authorize" + "/" and a plain "/authorize" are the same route:
	// the id drops the trailing slash unless the path is exactly "/".
	template = normRoutePath(template)
	symbol := w.enclosing
	if handler != nil {
		if s := w.handlerSymbol(handler); s != "" {
			symbol = s
		}
	}
	line := w.p.lineOf(call)
	key := verb + "|" + template + "|" + symbol + "|" + strconv.Itoa(line)
	if w.seen[key] {
		return
	}
	w.seen[key] = true
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
		Line:      line,
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

// joinPaths combines a router prefix and a local path into one
// slash-joined path: empty segments drop, duplicate slashes collapse.
func joinPaths(prefix, p string) string {
	var segs []string
	for _, s := range strings.Split(prefix+"/"+p, "/") {
		if s != "" {
			segs = append(segs, s)
		}
	}
	if len(segs) == 0 {
		return "/"
	}
	return "/" + strings.Join(segs, "/")
}

// normRoutePath strips the trailing slash a joined path can pick up
// ("/authorize" + "/"), keeping the bare root "/".
func normRoutePath(p string) string {
	for len(p) > 1 && strings.HasSuffix(p, "/") {
		p = p[:len(p)-1]
	}
	return p
}
