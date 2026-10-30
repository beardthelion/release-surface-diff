// Command go-extractor walks a Go source tree and emits the repo's
// normalized surface records as JSONL on stdout, plus two sidecars:
// a symbol table (--symbols) and intra-package call edges (--edges).
//
// Contract (adapters/helpers.py):
//
//	go-extractor <root> --symbols <path> --edges <path>
//
// stdout carries one record per line with the fields
// {kind,id,file,line,symbol,lang,extractor,attrs}. Package load failures
// degrade per directory (warning to stderr, keep going), never per run.
package main

import (
	"encoding/json"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"golang.org/x/tools/go/packages"
)

// extractorName matches the "name@version" convention other adapters use.
const extractorName = "go-extractor@0.1.0"

// record mirrors records.py Record (R11). Field order here is irrelevant;
// encoding/json emits keys sorted only by struct order, so records.py's
// from_dict is the canonicalizer downstream.
type record struct {
	Kind      string         `json:"kind"`
	ID        string         `json:"id"`
	File      string         `json:"file"`
	Line      int            `json:"line"`
	Symbol    string         `json:"symbol"`
	Lang      string         `json:"lang"`
	Extractor string         `json:"extractor"`
	Attrs     map[string]any `json:"attrs"`
}

// symbolSpan mirrors records.py SymbolSpan (KTD7).
type symbolSpan struct {
	File      string `json:"file"`
	Symbol    string `json:"symbol"`
	StartLine int    `json:"start_line"`
	EndLine   int    `json:"end_line"`
}

// callEdge mirrors records.py CallEdge (KTD8).
type callEdge struct {
	Caller string `json:"caller"`
	Callee string `json:"callee"`
}

var warn = func(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "go-extractor: "+format+"\n", args...)
}

// pkgCtx is one Go package's syntax plus the indexes every extractor pass
// shares. It is built either from packages.Load output (module trees) or
// from a hand-parsed directory (trees without go.mod).
type pkgCtx struct {
	fset    *token.FileSet
	root    string // absolute scan root
	dir     string // absolute package dir
	relDir  string // root-relative posix dir, "" at the root
	name    string // package clause
	pkgPath string // import path when known, "" otherwise
	files   []*ast.File

	imports  map[*ast.File]map[string]string // file -> alias -> import path
	consts   map[string]string               // package-level string consts/vars
	funcs    map[string]*ast.FuncDecl        // free functions by name
	methods  map[string][]*methodDecl        // method name -> declarations
	types    map[string]*ast.TypeSpec
	structs  map[string]*ast.StructType
	internal bool // under an internal/ path element
}

type methodDecl struct {
	recv string
	decl *ast.FuncDecl
}

func (p *pkgCtx) position(pos token.Pos) token.Position {
	return p.fset.PositionFor(pos, false)
}

// relFile converts an absolute filename to the root-relative posix path the
// record contract wants.
func (p *pkgCtx) relFile(filename string) string {
	rel, err := filepath.Rel(p.root, filename)
	if err != nil {
		return filepath.ToSlash(filename)
	}
	return filepath.ToSlash(rel)
}

func (p *pkgCtx) fileOf(f *ast.File) string {
	return p.relFile(p.position(f.Package).Filename)
}

func (p *pkgCtx) lineOf(n ast.Node) int {
	return p.position(n.Pos()).Line
}

// importPath resolves an identifier to an import path when it names an
// imported package in this file; ("", false) for ordinary identifiers.
func (p *pkgCtx) importPath(f *ast.File, ident *ast.Ident) (string, bool) {
	path, ok := p.imports[f][ident.Name]
	return path, ok
}

// pathBase is the last element of an import path.
func pathBase(path string) string {
	if i := strings.LastIndex(path, "/"); i >= 0 {
		return path[i+1:]
	}
	return path
}

// ---- tree loading -------------------------------------------------------

// skipDir mirrors the default exclude spirit (KTD11): vendored trees,
// testdata, hidden dirs, and Go's own underscore convention.
func skipDir(name string) bool {
	if name == "vendor" || name == "testdata" || name == "node_modules" {
		return true
	}
	return strings.HasPrefix(name, ".") || strings.HasPrefix(name, "_")
}

// skipFile drops generated and test sources before any pass runs. The
// orchestrator also filters on path, but skipping here keeps the symbol
// table and call edges free of generated noise too.
func skipFile(path string, f *ast.File) bool {
	base := filepath.Base(path)
	if strings.HasSuffix(base, "_test.go") ||
		strings.HasSuffix(base, ".pb.go") ||
		strings.HasSuffix(base, ".connect.go") {
		return true
	}
	if f != nil {
		for _, cg := range f.Comments {
			for _, c := range cg.List {
				if generatedComment(c.Text) {
					return true
				}
			}
		}
	}
	return false
}

func generatedComment(text string) bool {
	t := strings.TrimSpace(text)
	return strings.HasPrefix(t, "// Code generated ") &&
		strings.Contains(t, "DO NOT EDIT")
}

// findModuleRoots returns dirs holding a go.mod, shallowest first.
func findModuleRoots(root string) []string {
	var mods []string
	_ = filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.IsDir() {
			if skipDir(d.Name()) && path != root {
				return filepath.SkipDir
			}
			return nil
		}
		if d.Name() == "go.mod" {
			mods = append(mods, filepath.Dir(path))
		}
		return nil
	})
	sort.Strings(mods)
	return mods
}

// underModule reports whether dir sits inside any module root.
func underModule(dir string, mods []string) bool {
	for _, m := range mods {
		if dir == m || strings.HasPrefix(dir, m+string(filepath.Separator)) {
			return true
		}
	}
	return false
}

// loadSyntaxMode needs no module cache and no type information: names,
// file lists, syntax trees, and the import spec graph only.
const loadSyntaxMode = packages.NeedName | packages.NeedFiles |
	packages.NeedSyntax | packages.NeedImports

// loadModule loads every package under one module root. Per-package errors
// (unresolved imports, one bad file) are reported and tolerated; only a
// wholesale failure to list any package returns empty.
func loadModule(root, modRoot string) []*pkgCtx {
	cfg := &packages.Config{Mode: loadSyntaxMode, Dir: modRoot, Fset: token.NewFileSet()}
	pkgs, err := packages.Load(cfg, "./...")
	if err != nil {
		warn("%s: package listing failed: %v", relDirName(root, modRoot), err)
		return nil
	}
	var out []*pkgCtx
	for _, p := range pkgs {
		if len(p.Syntax) == 0 {
			for _, e := range p.Errors {
				warn("%s: %v", p.PkgPath, e)
			}
			continue
		}
		bad := map[string]bool{}
		for _, e := range p.Errors {
			warn("%s: %v", p.PkgPath, e)
			if e.Pos != "" {
				// Pos is "file:line:col"; mark the file damaged so its
				// partial AST contributes nothing.
				bad[strings.SplitN(e.Pos, ":", 2)[0]] = true
			}
		}
		ctx := newPkgCtx(cfg.Fset, root, p.Name, p.PkgPath)
		for _, f := range p.Syntax {
			fname := cfg.Fset.PositionFor(f.Package, false).Filename
			if bad[fname] || !insideRoot(root, fname) || skipFile(fname, f) {
				continue
			}
			ctx.addFile(f)
		}
		if len(ctx.files) > 0 {
			out = append(out, ctx)
		}
	}
	return out
}

func insideRoot(root, path string) bool {
	abs, err := filepath.Abs(path)
	if err != nil {
		return false
	}
	return abs == root || strings.HasPrefix(abs, root+string(filepath.Separator))
}

func relDirName(root, dir string) string {
	rel, err := filepath.Rel(root, dir)
	if err != nil || rel == "." {
		return "."
	}
	return rel
}

// loadDirFallback parses every .go file in a directory without a module
// context. One malformed file warns and is skipped; siblings still extract.
func loadDirFallback(root, dir string) *pkgCtx {
	fset := token.NewFileSet()
	ctx := newPkgCtx(fset, root, "", "")
	entries, err := os.ReadDir(dir)
	if err != nil {
		warn("%s: %v", relDirName(root, dir), err)
		return nil
	}
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".go") {
			continue
		}
		path := filepath.Join(dir, e.Name())
		if skipFile(path, nil) {
			continue
		}
		f, err := parser.ParseFile(fset, path, nil, parser.ParseComments)
		if err != nil {
			warn("%s: parse failed: %v", relDirName(root, dir), err)
			continue
		}
		if skipFile(path, f) {
			continue
		}
		ctx.addFile(f)
	}
	if len(ctx.files) == 0 {
		return nil
	}
	return ctx
}

// fallbackDirs lists directories with .go files. With covered == "" it
// returns dirs outside every module root; otherwise it returns dirs
// inside that one module root but outside any other module root (a
// nested module still belongs to its own go list).
func fallbackDirs(root string, mods []string, covered string) []string {
	var dirs []string
	_ = filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil || !d.IsDir() {
			return nil
		}
		if skipDir(d.Name()) && path != root {
			return filepath.SkipDir
		}
		if covered != "" {
			if !underModule(path, []string{covered}) {
				return filepath.SkipDir
			}
			for _, m := range mods {
				if m != covered && underModule(path, []string{m}) {
					return filepath.SkipDir
				}
			}
		} else if underModule(path, mods) {
			return nil
		}
		for _, e := range mustReadDir(path) {
			if !e.IsDir() && strings.HasSuffix(e.Name(), ".go") {
				dirs = append(dirs, path)
				break
			}
		}
		return nil
	})
	sort.Strings(dirs)
	return dirs
}

func mustReadDir(dir string) []os.DirEntry {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	return entries
}

func loadTree(root string) []*pkgCtx {
	mods := findModuleRoots(root)
	var pkgs []*pkgCtx
	var dirs []string
	for _, m := range mods {
		got := loadModule(root, m)
		if len(got) == 0 {
			// go list failed wholesale for this module (corrupt go.mod,
			// toolchain mismatch): still extract per directory.
			warn("%s: falling back to per-directory parsing", relDirName(root, m))
			dirs = append(dirs, fallbackDirs(root, mods, m)...)
			continue
		}
		pkgs = append(pkgs, got...)
	}
	dirs = append(dirs, fallbackDirs(root, mods, "")...)
	for _, dir := range dirs {
		if ctx := loadDirFallback(root, dir); ctx != nil {
			pkgs = append(pkgs, ctx)
		}
	}
	return pkgs
}

// ---- extraction ---------------------------------------------------------

// extract runs every pass over every package and merges the results.
func extract(pkgs []*pkgCtx) (records []record, spans []symbolSpan, edges []callEdge) {
	seen := map[string]bool{}
	for _, p := range pkgs {
		p.index()
		for _, r := range extractRoutes(p) {
			appendUnique(&records, seen, r)
		}
		for _, r := range extractRPC(p) {
			appendUnique(&records, seen, r)
		}
		for _, r := range extractCLIFlags(p) {
			appendUnique(&records, seen, r)
		}
		for _, r := range extractConfigKeys(p) {
			appendUnique(&records, seen, r)
		}
		for _, r := range extractExports(p) {
			appendUnique(&records, seen, r)
		}
		spans = append(spans, p.symbolSpans()...)
		edges = append(edges, p.callEdges()...)
	}
	return records, spans, edges
}

func appendUnique(recs *[]record, seen map[string]bool, r record) {
	key := r.Kind + "|" + r.ID + "|" + r.File + "|" +
		fmt.Sprint(r.Line) + "|" + r.Symbol
	if seen[key] {
		return
	}
	seen[key] = true
	*recs = append(*recs, r)
}

func sortRecords(recs []record) {
	sort.SliceStable(recs, func(i, j int) bool {
		a, b := recs[i], recs[j]
		if a.Kind != b.Kind {
			return a.Kind < b.Kind
		}
		if a.ID != b.ID {
			return a.ID < b.ID
		}
		if a.File != b.File {
			return a.File < b.File
		}
		if a.Line != b.Line {
			return a.Line < b.Line
		}
		return a.Symbol < b.Symbol
	})
}

func sortSpans(spans []symbolSpan) {
	sort.SliceStable(spans, func(i, j int) bool {
		a, b := spans[i], spans[j]
		if a.File != b.File {
			return a.File < b.File
		}
		if a.Symbol != b.Symbol {
			return a.Symbol < b.Symbol
		}
		return a.StartLine < b.StartLine
	})
}

// dedupeSpans drops exact-duplicate spans so a package covered twice
// (a failed module plus its own nested module) cannot emit repeats.
func dedupeSpans(spans []symbolSpan) []symbolSpan {
	seen := map[symbolSpan]bool{}
	out := spans[:0]
	for _, s := range spans {
		if seen[s] {
			continue
		}
		seen[s] = true
		out = append(out, s)
	}
	return out
}

func sortEdges(edges []callEdge) []callEdge {
	seen := map[callEdge]bool{}
	var out []callEdge
	for _, e := range edges {
		if seen[e] {
			continue
		}
		seen[e] = true
		out = append(out, e)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Caller != out[j].Caller {
			return out[i].Caller < out[j].Caller
		}
		return out[i].Callee < out[j].Callee
	})
	return out
}

func writeJSONL(path string, marshal func(func(v any))) error {
	var lines []byte
	marshal(func(v any) {
		data, err := json.Marshal(v)
		if err == nil {
			lines = append(lines, data...)
			lines = append(lines, '\n')
		}
	})
	return os.WriteFile(path, lines, 0o644)
}

// ---- CLI ----------------------------------------------------------------

func usage() {
	fmt.Fprintln(os.Stderr,
		"usage: go-extractor <root> --symbols <path> --edges <path>")
}

func main() {
	os.Exit(run(os.Args[1:]))
}

func run(args []string) int {
	var root, symbolsPath, edgesPath string
	for i := 0; i < len(args); i++ {
		switch {
		case args[i] == "--symbols" && i+1 < len(args):
			symbolsPath = args[i+1]
			i++
		case args[i] == "--edges" && i+1 < len(args):
			edgesPath = args[i+1]
			i++
		case strings.HasPrefix(args[i], "--symbols="):
			symbolsPath = strings.TrimPrefix(args[i], "--symbols=")
		case strings.HasPrefix(args[i], "--edges="):
			edgesPath = strings.TrimPrefix(args[i], "--edges=")
		case strings.HasPrefix(args[i], "-"):
			warn("unknown flag %q", args[i])
			usage()
			return 2
		case root == "":
			root = args[i]
		default:
			warn("unexpected argument %q", args[i])
			usage()
			return 2
		}
	}
	if root == "" {
		warn("missing root path")
		usage()
		return 2
	}
	abs, err := filepath.Abs(root)
	if err != nil {
		warn("bad root %q: %v", root, err)
		return 2
	}
	info, err := os.Stat(abs)
	if err != nil || !info.IsDir() {
		warn("root %q is not a directory", root)
		return 2
	}

	records, spans, edges := extract(loadTree(abs))
	sortRecords(records)
	spans = dedupeSpans(spans)
	sortSpans(spans)
	edges = sortEdges(edges)

	if symbolsPath != "" {
		if err := writeJSONL(symbolsPath, func(emit func(v any)) {
			for _, s := range spans {
				emit(s)
			}
		}); err != nil {
			warn("cannot write symbols sidecar: %v", err)
			return 2
		}
	}
	if edgesPath != "" {
		if err := writeJSONL(edgesPath, func(emit func(v any)) {
			for _, e := range edges {
				emit(e)
			}
		}); err != nil {
			warn("cannot write edges sidecar: %v", err)
			return 2
		}
	}

	enc := json.NewEncoder(os.Stdout)
	for _, r := range records {
		if err := enc.Encode(r); err != nil {
			warn("stdout encode failed: %v", err)
			return 2
		}
	}
	return 0
}
