package main

import (
	"go/ast"
	pathpkg "path"
	"regexp"
)

// RPC surface (R8): gRPC RegisterService and generated RegisterXxxServer
// calls, net/rpc Register/RegisterName, and geth-style
// `apis []rpc.API{{Namespace: "eth"}}` declarations. Method names are the
// canonical rpc record ids (KTD5).

var registerServerRe = regexp.MustCompile(`^Register(.+)Server$`)

// extractRPC scans declarations for registration calls and rpc.API
// composite literals.
func extractRPC(p *pkgCtx) []record {
	var out []record
	for _, f := range p.files {
		file := p.fileOf(f)
		for _, decl := range f.Decls {
			enclosing := declSymbol(decl)
			ast.Inspect(decl, func(n ast.Node) bool {
				if n == nil {
					return true
				}
				switch x := n.(type) {
				case *ast.CallExpr:
					out = append(out, p.rpcFromCall(f, file, enclosing, x)...)
				case *ast.CompositeLit:
					out = append(out, p.rpcFromLiteral(f, file, enclosing, x)...)
				}
				return true
			})
		}
	}
	return out
}

func declSymbol(decl ast.Decl) string {
	switch d := decl.(type) {
	case *ast.FuncDecl:
		return funcSymbol(d)
	case *ast.GenDecl:
		for _, spec := range d.Specs {
			if s := specSymbol(spec); s != "" {
				return s
			}
		}
	}
	return ""
}

func (p *pkgCtx) rpcFromCall(f *ast.File, file, enclosing string, call *ast.CallExpr) []record {
	sel, ok := call.Fun.(*ast.SelectorExpr)
	if !ok || len(call.Args) == 0 {
		return nil
	}
	name := sel.Sel.Name
	emit := func(id, source string, pos ast.Node) []record {
		if id == "" {
			return nil
		}
		return []record{{
			Kind:      "rpc",
			ID:        id,
			File:      file,
			Line:      p.lineOf(pos),
			Symbol:    enclosing,
			Lang:      "go",
			Extractor: extractorName,
			Attrs:     map[string]any{"source": source},
		}}
	}

	// s.RegisterService(&grpc.ServiceDesc{ServiceName: "foo.Bar"}, impl)
	if name == "RegisterService" {
		if sn := serviceNameOf(call.Args[0]); sn != "" {
			return emit(sn, "service_desc", call)
		}
		return emit(typeNameOf(call.Args[0]), "service_desc", call)
	}

	// Generated stubs: pb.RegisterUserServer(s, impl) -> "User".
	if m := registerServerRe.FindStringSubmatch(name); m != nil {
		return emit(m[1], "register_server", call)
	}

	// net/rpc and geth-style rpc packages only: receiver must resolve to an
	// import path whose base element is "rpc" so prometheus-style
	// registry.Register(...) calls stay out.
	if isRPCReceiver(p, f, sel.X) {
		switch name {
		case "RegisterName":
			if lit, ok := p.resolveString(call.Args[0], nil); ok {
				return emit(lit, "register_name", call)
			}
		case "Register":
			return emit(typeNameOf(call.Args[0]), "register", call)
		}
	}
	return nil
}

// isRPCReceiver reports whether expr is an identifier naming an imported
// package whose path ends in /rpc (net/rpc, go-ethereum/rpc, ...). The
// import map covers every import spec whether or not the path resolves,
// so a bare `rpc` identifier with no such import is a local value, not a
// package.
func isRPCReceiver(p *pkgCtx, f *ast.File, e ast.Expr) bool {
	id, ok := e.(*ast.Ident)
	if !ok {
		return false
	}
	path, imported := p.importPath(f, id)
	return imported && pathpkg.Base(path) == "rpc"
}

// serviceNameOf extracts ServiceName from a ServiceDesc literal.
func serviceNameOf(e ast.Expr) string {
	lit, ok := e.(*ast.CompositeLit)
	if !ok {
		if u, ok := e.(*ast.UnaryExpr); ok {
			lit, _ = u.X.(*ast.CompositeLit)
		}
	}
	if lit == nil {
		return ""
	}
	for _, elt := range lit.Elts {
		kv, ok := elt.(*ast.KeyValueExpr)
		if !ok {
			continue
		}
		if key, ok := kv.Key.(*ast.Ident); ok && key.Name == "ServiceName" {
			if v, ok := stringLit(kv.Value); ok {
				return v
			}
		}
	}
	return ""
}

// rpcFromLiteral handles geth-style `[]rpc.API{{Namespace: "eth"}}` and
// `rpc.API{Namespace: "eth"}` composite literals.
func (p *pkgCtx) rpcFromLiteral(f *ast.File, file, enclosing string, lit *ast.CompositeLit) []record {
	isAPI := func(e ast.Expr) bool {
		sel, ok := e.(*ast.SelectorExpr)
		if !ok || sel.Sel.Name != "API" {
			return false
		}
		return isRPCReceiver(p, f, sel.X)
	}

	var lits []*ast.CompositeLit
	switch t := lit.Type.(type) {
	case *ast.ArrayType:
		elt := t.Elt
		if star, ok := elt.(*ast.StarExpr); ok {
			elt = star.X
		}
		if !isAPI(elt) {
			return nil
		}
		for _, e := range lit.Elts {
			if inner, ok := e.(*ast.CompositeLit); ok {
				lits = append(lits, inner)
			}
		}
	case *ast.SelectorExpr:
		if !isAPI(t) {
			return nil
		}
		lits = append(lits, lit)
	default:
		return nil
	}

	var out []record
	for _, inner := range lits {
		for _, elt := range inner.Elts {
			kv, ok := elt.(*ast.KeyValueExpr)
			if !ok {
				continue
			}
			key, ok := kv.Key.(*ast.Ident)
			if !ok || key.Name != "Namespace" {
				continue
			}
			if ns, ok := p.resolveString(kv.Value, nil); ok && ns != "" {
				out = append(out, record{
					Kind:      "rpc",
					ID:        ns,
					File:      file,
					Line:      p.lineOf(kv),
					Symbol:    enclosing,
					Lang:      "go",
					Extractor: extractorName,
					Attrs:     map[string]any{"source": "api_namespace"},
				})
			}
		}
	}
	return out
}

// typeNameOf renders the named type behind an expression: &T{}, new(T),
// T{}, or a plain identifier.
func typeNameOf(e ast.Expr) string {
	switch t := e.(type) {
	case *ast.CompositeLit:
		return typeNameOf(t.Type)
	case *ast.UnaryExpr:
		return typeNameOf(t.X)
	case *ast.StarExpr:
		return typeNameOf(t.X)
	case *ast.ParenExpr:
		return typeNameOf(t.X)
	case *ast.IndexExpr:
		return typeNameOf(t.X)
	case *ast.IndexListExpr:
		return typeNameOf(t.X)
	case *ast.CallExpr:
		if id, ok := t.Fun.(*ast.Ident); ok && id.Name == "new" && len(t.Args) > 0 {
			return typeNameOf(t.Args[0])
		}
		return typeNameOf(t.Fun)
	case *ast.Ident:
		return t.Name
	case *ast.SelectorExpr:
		return t.Sel.Name
	}
	return ""
}
