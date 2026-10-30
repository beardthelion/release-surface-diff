package main

import (
	"go/ast"
	"go/token"
	pathpkg "path"
	"path/filepath"
	"strconv"
	"strings"
)

// Shared syntax helpers: the package index every pass uses (KTD7), the
// symbol table, and intra-package call edges (KTD8).

func newPkgCtx(fset *token.FileSet, root, name, pkgPath string) *pkgCtx {
	return &pkgCtx{
		fset:       fset,
		root:       root,
		name:       name,
		pkgPath:    pkgPath,
		imports:    map[*ast.File]map[string]string{},
		consts:     map[string]string{},
		funcs:      map[string]*ast.FuncDecl{},
		methods:    map[string][]*methodDecl{},
		types:      map[string]*ast.TypeSpec{},
		structs:    map[string]*ast.StructType{},
		routerVars: map[string]bool{},
	}
}

func (p *pkgCtx) addFile(f *ast.File) {
	p.files = append(p.files, f)
	if p.dir == "" {
		p.dir = filepath.Dir(p.position(f.Package).Filename)
	}
}

// index fills imports/consts/funcs/methods/types after all files are added.
func (p *pkgCtx) index() {
	if len(p.files) == 0 {
		return
	}
	p.name = p.files[0].Name.Name
	if p.dir != "" {
		rel, err := filepath.Rel(p.root, p.dir)
		if err == nil && rel != "." {
			p.relDir = filepath.ToSlash(rel)
		}
	}
	for _, part := range strings.Split(p.relDir, "/") {
		if part == "internal" {
			p.internal = true
		}
	}
	if strings.HasSuffix(p.pkgPath, "/internal") ||
		strings.Contains(p.pkgPath, "/internal/") {
		p.internal = true
	}
	for _, f := range p.files {
		p.indexFile(f)
	}
}

func (p *pkgCtx) indexFile(f *ast.File) {
	imports := map[string]string{}
	for _, spec := range f.Imports {
		path, err := strconv.Unquote(spec.Path.Value)
		if err != nil {
			continue
		}
		alias := pathpkg.Base(path)
		if spec.Name != nil {
			alias = spec.Name.Name
		}
		if alias == "." || alias == "_" {
			continue
		}
		imports[alias] = path
	}
	p.imports[f] = imports

	for _, decl := range f.Decls {
		switch d := decl.(type) {
		case *ast.FuncDecl:
			if d.Recv == nil {
				p.funcs[d.Name.Name] = d
			} else {
				recv := recvBase(d.Recv.List[0].Type)
				p.methods[d.Name.Name] = append(
					p.methods[d.Name.Name], &methodDecl{recv: recv, decl: d})
			}
		case *ast.GenDecl:
			p.indexGenDecl(d)
		}
	}
}

func (p *pkgCtx) indexGenDecl(d *ast.GenDecl) {
	for _, spec := range d.Specs {
		switch s := spec.(type) {
		case *ast.TypeSpec:
			p.types[s.Name.Name] = s
			if st, ok := s.Type.(*ast.StructType); ok {
				p.structs[s.Name.Name] = st
			}
		case *ast.ValueSpec:
			// Package-level `const|var name = "lit"` lets route paths
			// and rpc names resolve through an identifier.
			if len(s.Names) == 1 && len(s.Values) == 1 {
				if lit, ok := stringLit(s.Values[0]); ok {
					p.consts[s.Names[0].Name] = lit
				}
			}
			// `var x = chi.NewRouter()` marks a package router whose
			// registrations follow wherever it is mounted.
			for i, name := range s.Names {
				if i < len(s.Values) {
					if call, ok := s.Values[i].(*ast.CallExpr); ok &&
						isRouterCtorCall(call) {
						p.routerVars[name.Name] = true
					}
				}
			}
		}
	}
}

// recvBase strips *, [], and generic instantiation to the named type.
func recvBase(e ast.Expr) string {
	switch t := e.(type) {
	case *ast.Ident:
		return t.Name
	case *ast.StarExpr:
		return recvBase(t.X)
	case *ast.IndexExpr:
		return recvBase(t.X)
	case *ast.IndexListExpr:
		return recvBase(t.X)
	case *ast.SelectorExpr:
		return t.Sel.Name
	}
	return ""
}

// funcSymbol names a declaration the way the symbol table and call edges
// do: bare name for functions, Recv.Name for methods.
func funcSymbol(d *ast.FuncDecl) string {
	if d.Recv == nil || len(d.Recv.List) == 0 {
		return d.Name.Name
	}
	recv := recvBase(d.Recv.List[0].Type)
	if recv == "" {
		return d.Name.Name
	}
	return recv + "." + d.Name.Name
}

// stringLit unwraps a Go string literal; returns ("", false) otherwise.
func stringLit(e ast.Expr) (string, bool) {
	switch t := e.(type) {
	case *ast.BasicLit:
		if t.Kind == token.STRING {
			v, err := strconv.Unquote(t.Value)
			if err == nil {
				return v, true
			}
		}
	case *ast.ParenExpr:
		return stringLit(t.X)
	}
	return "", false
}

// resolveString resolves a literal or a package-level string const/var.
func (p *pkgCtx) resolveString(e ast.Expr, locals map[string]string) (string, bool) {
	if lit, ok := stringLit(e); ok {
		return lit, true
	}
	if id, ok := e.(*ast.Ident); ok {
		if locals != nil {
			if v, hit := locals[id.Name]; hit {
				return v, true
			}
		}
		if v, hit := p.consts[id.Name]; hit {
			return v, true
		}
	}
	return "", false
}

// exprName renders an expression's callable name for auth hints:
// Ident -> name, pkg.Fn -> "pkg.Fn", calls -> the callee's name.
func exprName(e ast.Expr) string {
	switch t := e.(type) {
	case *ast.Ident:
		return t.Name
	case *ast.SelectorExpr:
		if base := exprName(t.X); base != "" {
			return base + "." + t.Sel.Name
		}
		return t.Sel.Name
	case *ast.CallExpr:
		return exprName(t.Fun)
	case *ast.StarExpr:
		return exprName(t.X)
	case *ast.ParenExpr:
		return exprName(t.X)
	case *ast.FuncLit:
		return "func"
	}
	return ""
}

// ---- spec naming ---------------------------------------------------------

func specSymbol(spec ast.Spec) string {
	var name string
	switch s := spec.(type) {
	case *ast.TypeSpec:
		name = s.Name.Name
	case *ast.ValueSpec:
		if len(s.Names) > 0 {
			name = s.Names[0].Name
		}
	}
	if name == "_" {
		return ""
	}
	return name
}

// ---- symbol table (KTD7) -------------------------------------------------

// symbolSpans emits one span per FuncDecl and per var/const/type spec so
// guard hunks, body_diff, and body hashing all read from one table.
func (p *pkgCtx) symbolSpans() []symbolSpan {
	var out []symbolSpan
	for _, f := range p.files {
		file := p.fileOf(f)
		for _, decl := range f.Decls {
			switch d := decl.(type) {
			case *ast.FuncDecl:
				out = append(out, symbolSpan{
					File:      file,
					Symbol:    funcSymbol(d),
					StartLine: p.position(d.Pos()).Line,
					EndLine:   p.position(d.End()).Line,
				})
			case *ast.GenDecl:
				if d.Tok == token.IMPORT {
					continue
				}
				for _, spec := range d.Specs {
					name := specSymbol(spec)
					if name == "" {
						continue
					}
					out = append(out, symbolSpan{
						File:      file,
						Symbol:    name,
						StartLine: p.position(spec.Pos()).Line,
						EndLine:   p.position(spec.End()).Line,
					})
				}
			}
		}
	}
	return out
}

// ---- call edges (KTD8) ---------------------------------------------------

// callEdges emits (caller, callee) pairs resolvable inside this package:
// bare calls to declared functions, and x.M() where exactly one method M
// exists in the package. Calls through import aliases are cross-package
// and out of scope for the reach bonus.
func (p *pkgCtx) callEdges() []callEdge {
	var out []callEdge
	for _, f := range p.files {
		for _, decl := range f.Decls {
			caller := ""
			var body ast.Node
			switch d := decl.(type) {
			case *ast.FuncDecl:
				caller = funcSymbol(d)
				if d.Body != nil {
					body = d.Body
				}
			case *ast.GenDecl:
				for _, spec := range d.Specs {
					if vs, ok := spec.(*ast.ValueSpec); ok {
						caller = specSymbol(vs)
						for _, v := range vs.Values {
							out = append(out, p.callsIn(f, v, caller)...)
						}
					}
				}
				continue
			}
			if body != nil {
				out = append(out, p.callsIn(f, body, caller)...)
			}
		}
	}
	return out
}

func (p *pkgCtx) callsIn(f *ast.File, n ast.Node, caller string) []callEdge {
	var out []callEdge
	if caller == "" {
		return out
	}
	ast.Inspect(n, func(node ast.Node) bool {
		call, ok := node.(*ast.CallExpr)
		if !ok {
			return true
		}
		if callee := p.resolveCallee(f, call); callee != "" && callee != caller {
			out = append(out, callEdge{Caller: caller, Callee: callee})
		}
		return true
	})
	return out
}

func (p *pkgCtx) resolveCallee(f *ast.File, call *ast.CallExpr) string {
	switch fn := call.Fun.(type) {
	case *ast.Ident:
		if _, ok := p.funcs[fn.Name]; ok {
			return fn.Name
		}
	case *ast.SelectorExpr:
		recv, ok := fn.X.(*ast.Ident)
		if !ok {
			return ""
		}
		if _, imported := p.imports[f][recv.Name]; imported {
			return "" // pkg.Fn: cross-package
		}
		cands := p.methods[fn.Sel.Name]
		if len(cands) == 1 {
			return cands[0].recv + "." + fn.Sel.Name
		}
	}
	return ""
}
