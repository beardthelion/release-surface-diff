package main

import (
	"go/ast"
	"reflect"
	"strconv"
	"strings"
)

// Config-key surface (R8): struct fields carrying config tags
// (mapstructure/toml/yaml/json/env/ini) on types reachable from a config
// root. A root is a struct named Config/Cfg/Configuration (or *Config),
// or any struct in a package named config/conf/cfg. Nested struct fields
// produce dotted paths (KTD5).

var configTagKeys = []string{"mapstructure", "toml", "yaml", "json", "env", "ini"}

func extractConfigKeys(p *pkgCtx) []record {
	if len(p.structs) == 0 {
		return nil
	}
	// Name each struct type so field records can point at the owning decl.
	owner := map[*ast.StructType]string{}
	structs := map[string]*ast.StructType{}
	for name, st := range p.structs {
		owner[st] = name
		structs[name] = st
	}

	var roots []string
	for name := range structs {
		if isConfigRoot(p, name) {
			roots = append(roots, name)
		}
	}
	// A struct already reached as a nested field of a root is emitted
	// under that root's prefix; it is not also a standalone root.
	reached := map[string]bool{}
	for _, name := range roots {
		markReached(structs, structs[name], map[string]bool{name: true}, reached)
	}
	var out []record
	for _, name := range roots {
		if reached[name] && len(roots) > 1 {
			continue
		}
		out = append(out, p.walkStruct(structs, owner, structs[name], "", name,
			map[string]bool{name: true})...)
	}
	return out
}

func isConfigRoot(p *pkgCtx, name string) bool {
	switch p.name {
	case "config", "conf", "cfg":
		return true
	}
	return name == "Config" || name == "Cfg" || name == "Configuration" ||
		strings.HasSuffix(name, "Config") ||
		strings.HasSuffix(name, "Configuration")
}

// markReached records struct names referenced as field types inside st.
func markReached(structs map[string]*ast.StructType, st *ast.StructType,
	seen, reached map[string]bool) {
	if st == nil {
		return
	}
	for _, field := range st.Fields.List {
		if tagSkip(field.Tag) {
			continue
		}
		name, anon := structFieldType(field.Type)
		if anon != nil {
			markReached(structs, anon, seen, reached)
			continue
		}
		if name == "" || seen[name] {
			continue
		}
		inner, ok := structs[name]
		if !ok {
			continue
		}
		reached[name] = true
		seen[name] = true
		markReached(structs, inner, seen, reached)
	}
}

// structFieldType resolves a field's type to a local struct: either a
// named type (returns its name) or an anonymous struct literal.
func structFieldType(e ast.Expr) (string, *ast.StructType) {
	switch t := e.(type) {
	case *ast.StructType:
		return "", t
	case *ast.Ident:
		return t.Name, nil
	case *ast.StarExpr:
		return structFieldType(t.X)
	case *ast.ParenExpr:
		return structFieldType(t.X)
	}
	return "", nil
}

func tagSkip(tag *ast.BasicLit) bool {
	k, _, dash := tagValue(tag)
	return dash || k == "-"
}

// tagValue returns the first present config tag: (value, which, isDash).
func tagValue(tag *ast.BasicLit) (string, string, bool) {
	if tag == nil {
		return "", "", false
	}
	raw, err := strconv.Unquote(tag.Value)
	if err != nil {
		return "", "", false
	}
	st := reflect.StructTag(raw)
	for _, key := range configTagKeys {
		v, ok := st.Lookup(key)
		if !ok {
			continue
		}
		name := strings.SplitN(v, ",", 2)[0]
		if name == "-" {
			return "", key, true
		}
		return name, key, false
	}
	return "", "", false
}

// walkStruct emits one config_key record per tagged field, descending
// into nested structs to build dotted paths.
func (p *pkgCtx) walkStruct(structs map[string]*ast.StructType,
	owner map[*ast.StructType]string, st *ast.StructType,
	prefix, root string, visiting map[string]bool) []record {
	var out []record
	ownerName := owner[st]
	for _, field := range st.Fields.List {
		key, tagName, dash := tagValue(field.Tag)
		if dash {
			continue
		}
		fieldPos := p.position(field.Pos())
		file := p.relFile(fieldPos.Filename)

		emit := func(path string) {
			if path == "" {
				return
			}
			out = append(out, record{
				Kind:      "config_key",
				ID:        path,
				File:      file,
				Line:      fieldPos.Line,
				Symbol:    ownerName,
				Lang:      "go",
				Extractor: extractorName,
				Attrs:     map[string]any{"root": root, "tag": tagName},
			})
		}

		typeName, anon := structFieldType(field.Type)

		if len(field.Names) == 0 {
			// Embedded field: a tag makes it a named level, otherwise
			// its keys flatten into the parent prefix.
			inner := anon
			if inner == nil && typeName != "" && !visiting[typeName] {
				inner = structs[typeName]
			}
			if inner == nil {
				continue
			}
			next := prefix
			if key != "" {
				next = joinDotted(prefix, key)
				emit(next)
			}
			if typeName != "" {
				visiting[typeName] = true
			}
			out = append(out, p.walkStruct(structs, owner, inner,
				next, root, visiting)...)
			delete(visiting, typeName)
			continue
		}

		if key == "" {
			key = strings.ToLower(field.Names[0].Name)
		}
		path := joinDotted(prefix, key)
		emit(path)

		inner := anon
		if inner == nil && typeName != "" && !visiting[typeName] {
			inner = structs[typeName]
		}
		if inner != nil {
			if typeName != "" {
				visiting[typeName] = true
			}
			out = append(out, p.walkStruct(structs, owner, inner,
				path, root, visiting)...)
			delete(visiting, typeName)
		}
	}
	return out
}

func joinDotted(prefix, key string) string {
	if prefix == "" {
		return key
	}
	if key == "" {
		return prefix
	}
	return prefix + "." + key
}
