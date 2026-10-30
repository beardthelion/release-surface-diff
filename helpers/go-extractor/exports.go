package main

import (
	"go/ast"
	"go/token"
	"strings"
)

// Export surface (R8): exported top-level identifiers in non-internal
// packages. The canonical id is the package-qualified name
// (KTD5): pkgpath.Name, pkgpath.Recv.Name for methods.

func extractExports(p *pkgCtx) []record {
	if p.internal {
		return nil
	}
	qual := p.pkgPath
	if qual == "" {
		qual = p.name
	}
	var out []record
	for _, f := range p.files {
		file := p.fileOf(f)
		for _, decl := range f.Decls {
			switch d := decl.(type) {
			case *ast.FuncDecl:
				if !d.Name.IsExported() {
					continue
				}
				id := qual + "." + d.Name.Name
				declKind := "func"
				if d.Recv != nil && len(d.Recv.List) > 0 {
					recv := recvBase(d.Recv.List[0].Type)
					if !ast.IsExported(recv) {
						continue
					}
					id = qual + "." + recv + "." + d.Name.Name
					declKind = "method"
				}
				out = append(out, exportRecord(
					file, p.lineOf(d), funcSymbol(d), id, declKind))
			case *ast.GenDecl:
				if d.Tok != token.TYPE && d.Tok != token.VAR &&
					d.Tok != token.CONST {
					continue
				}
				declKind := strings.ToLower(d.Tok.String())
				for _, spec := range d.Specs {
					out = append(out, p.exportFromSpec(
						file, qual, declKind, spec)...)
				}
			}
		}
	}
	return out
}

func (p *pkgCtx) exportFromSpec(file, qual, declKind string, spec ast.Spec) []record {
	var out []record
	emit := func(name string, pos ast.Node, symbol string) {
		if !ast.IsExported(name) {
			return
		}
		out = append(out, exportRecord(
			file, p.lineOf(pos), symbol, qual+"."+name, declKind))
	}
	switch s := spec.(type) {
	case *ast.TypeSpec:
		emit(s.Name.Name, s, s.Name.Name)
	case *ast.ValueSpec:
		for _, n := range s.Names {
			emit(n.Name, s, n.Name)
		}
	}
	return out
}

func exportRecord(file string, line int, symbol, id, declKind string) record {
	return record{
		Kind:      "export",
		ID:        id,
		File:      file,
		Line:      line,
		Symbol:    symbol,
		Lang:      "go",
		Extractor: extractorName,
		Attrs:     map[string]any{"decl": declKind},
	}
}
