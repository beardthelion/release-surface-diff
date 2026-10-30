package main

import (
	"go/ast"
	pathpkg "path"
	"strings"
)

// CLI flag surface (R8): stdlib flag.* and spf13/pflag registrations,
// flag.FlagSet methods, cobra Flags() chains, and urfave/cli *Flag
// literals. The canonical id is --name (KTD5).

// flagNameArg indexes which call argument carries the flag name: 0 for
// flag.String-style getters, 1 for *Var*/Var/VarP forms that take the
// destination pointer first.
var flagNameArg = map[string]int{
	"Bool": 0, "BoolP": 0, "Count": 0, "CountP": 0,
	"Duration": 0, "DurationP": 0,
	"Float32": 0, "Float32P": 0, "Float64": 0, "Float64P": 0,
	"Func": 0,
	"Int":  0, "IntP": 0, "Int8": 0, "Int16": 0, "Int32": 0, "Int32P": 0,
	"Int64": 0, "Int64P": 0,
	"IP": 0, "IPP": 0, "IPNet": 0, "IPNetP": 0,
	"String": 0, "StringP": 0,
	"StringArray": 0, "StringArrayP": 0,
	"StringSlice": 0, "StringSliceP": 0,
	"StringToString": 0, "StringToStringP": 0,
	"Uint": 0, "UintP": 0, "Uint8": 0, "Uint16": 0, "Uint32": 0,
	"Uint64": 0, "Uint64P": 0,
	"BoolVar": 1, "BoolVarP": 1, "CountVar": 1, "CountVarP": 1,
	"DurationVar": 1, "DurationVarP": 1,
	"Float32Var": 1, "Float32VarP": 1, "Float64Var": 1, "Float64VarP": 1,
	"IntVar": 1, "IntVarP": 1, "Int32Var": 1, "Int64Var": 1, "Int64VarP": 1,
	"IPVar": 1, "IPVarP": 1, "IPNetVar": 1, "IPNetVarP": 1,
	"StringVar": 1, "StringVarP": 1,
	"StringArrayVar": 1, "StringArrayVarP": 1,
	"StringSliceVar": 1, "StringSliceVarP": 1,
	"StringToStringVar": 1, "StringToStringVarP": 1,
	"TextVar": 1, "UintVar": 1, "UintVarP": 1,
	"Uint64Var": 1, "Uint64VarP": 1,
	"Var": 1, "VarP": 1,
}

// cobraSources are the FlagSet-returning selectors a flag-adder call can
// hang off: cmd.Flags(), cmd.PersistentFlags(), and friends.
var cobraSources = map[string]bool{
	"Flags": true, "PersistentFlags": true, "LocalFlags": true,
	"InheritedFlags": true, "LocalNonPersistentFlags": true,
}

func isFlagPkg(path string) bool {
	return path == "flag" || strings.HasSuffix(path, "/pflag")
}

func extractCLIFlags(p *pkgCtx) []record {
	var out []record
	for _, f := range p.files {
		file := p.fileOf(f)
		flagVars := p.flagSetVars(f)
		for _, decl := range f.Decls {
			enclosing := declSymbol(decl)
			ast.Inspect(decl, func(n ast.Node) bool {
				if n == nil {
					return true
				}
				switch x := n.(type) {
				case *ast.CallExpr:
					out = append(out,
						p.flagFromCall(f, file, enclosing, flagVars, x)...)
				case *ast.CompositeLit:
					out = append(out,
						p.flagFromLiteral(f, file, enclosing, x)...)
				}
				return true
			})
		}
	}
	return out
}

// flagSetVars finds `x := flag.NewFlagSet(...)` / pflag.NewFlagSet
// bindings so FlagSet methods on local vars count as registrations.
func (p *pkgCtx) flagSetVars(f *ast.File) map[string]bool {
	vars := map[string]bool{}
	ast.Inspect(f, func(n ast.Node) bool {
		as, ok := n.(*ast.AssignStmt)
		if !ok {
			return true
		}
		for i, rhs := range as.Rhs {
			call, ok := rhs.(*ast.CallExpr)
			if !ok {
				continue
			}
			sel, ok := call.Fun.(*ast.SelectorExpr)
			if !ok || sel.Sel.Name != "NewFlagSet" {
				continue
			}
			pkg, ok := sel.X.(*ast.Ident)
			if !ok {
				continue
			}
			if path, imported := p.importPath(f, pkg); imported && isFlagPkg(path) {
				if i < len(as.Lhs) {
					if id, ok := as.Lhs[i].(*ast.Ident); ok {
						vars[id.Name] = true
					}
				}
			}
		}
		return true
	})
	return vars
}

func (p *pkgCtx) flagFromCall(f *ast.File, file, enclosing string,
	flagVars map[string]bool, call *ast.CallExpr) []record {
	sel, ok := call.Fun.(*ast.SelectorExpr)
	if !ok {
		return nil
	}
	argIdx, isAdder := flagNameArg[sel.Sel.Name]
	if !isAdder || len(call.Args) <= argIdx {
		return nil
	}
	registrar := ""
	switch x := sel.X.(type) {
	case *ast.Ident:
		if path, imported := p.importPath(f, x); imported && isFlagPkg(path) {
			registrar = pathpkg.Base(path) // "flag" or "pflag"
		} else if flagVars[x.Name] {
			registrar = "flag.FlagSet"
		}
	case *ast.CallExpr:
		// cobra: cmd.Flags().String("name", ...) and friends
		if inner, ok := x.Fun.(*ast.SelectorExpr); ok && cobraSources[inner.Sel.Name] {
			registrar = "cobra"
		}
	case *ast.SelectorExpr:
		// flag.CommandLine.String("name", ...)
		if pkg, ok := x.X.(*ast.Ident); ok && x.Sel.Name == "CommandLine" {
			if path, imported := p.importPath(f, pkg); imported && isFlagPkg(path) {
				registrar = "flag"
			}
		}
	}
	if registrar == "" {
		return nil
	}
	name, ok := p.resolveString(call.Args[argIdx], nil)
	if !ok || name == "" {
		return nil
	}
	return []record{cliFlagRecord(file, p.lineOf(call), enclosing, name, registrar)}
}

// flagFromLiteral handles urfave/cli declarations:
// []cli.Flag{&cli.StringFlag{Name: "config, c", ...}}.
func (p *pkgCtx) flagFromLiteral(f *ast.File, file, enclosing string,
	lit *ast.CompositeLit) []record {
	sel, ok := lit.Type.(*ast.SelectorExpr)
	if !ok || !strings.HasSuffix(sel.Sel.Name, "Flag") {
		return nil
	}
	pkg, ok := sel.X.(*ast.Ident)
	if !ok {
		return nil
	}
	path, imported := p.importPath(f, pkg)
	if !imported || !strings.Contains(path, "urfave/cli") {
		return nil
	}
	var out []record
	for _, elt := range lit.Elts {
		kv, ok := elt.(*ast.KeyValueExpr)
		if !ok {
			continue
		}
		key, ok := kv.Key.(*ast.Ident)
		if !ok || key.Name != "Name" {
			continue
		}
		raw, ok := p.resolveString(kv.Value, nil)
		if !ok {
			continue
		}
		// urfave v1 spells "name, alias"; the first part is canonical.
		name := strings.TrimSpace(strings.SplitN(raw, ",", 2)[0])
		if name != "" {
			out = append(out,
				cliFlagRecord(file, p.lineOf(kv), enclosing, name, "urfave-cli"))
		}
	}
	return out
}

func cliFlagRecord(file string, line int, enclosing, name, registrar string) record {
	return record{
		Kind:      "cli_flag",
		ID:        "--" + strings.TrimLeft(name, "-"),
		File:      file,
		Line:      line,
		Symbol:    enclosing,
		Lang:      "go",
		Extractor: extractorName,
		Attrs:     map[string]any{"registrar": registrar},
	}
}
