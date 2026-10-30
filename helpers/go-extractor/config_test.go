package main

import (
	"testing"
)

func TestConfigKeysNested(t *testing.T) {
	ctx := extractOne(t, map[string]string{
		"a.go": `package demo

type Config struct {
	Inline inlineSect ` + "`toml:\"inline\"`" + `
	Anon   struct {
		Deep int ` + "`toml:\"deep\"`" + `
	} ` + "`toml:\"anon\"`" + `
	SkipMe string ` + "`toml:\"-\"`" + `
}

type inlineSect struct {
	A int ` + "`toml:\"a\"`" + `
}
`,
	})
	ids := recordIDs(extractConfigKeys(ctx))
	for _, want := range []string{
		"inline", "inline.a", "anon", "anon.deep",
	} {
		if !ids["config_key|"+want] {
			t.Errorf("missing config key %s, got %v", want, ids)
		}
	}
	if ids["config_key|SkipMe"] || ids["config_key|skipme"] {
		t.Error("toml:\"-\" field should not emit a key")
	}
}

func TestNonConfigStructIgnored(t *testing.T) {
	ctx := extractOne(t, map[string]string{
		"a.go": `package demo

type Request struct {
	ID string ` + "`json:\"id\"`" + `
}
`,
	})
	if recs := extractConfigKeys(ctx); len(recs) != 0 {
		t.Errorf("non-config struct should not emit keys: %+v", recs)
	}
}

func TestConfigPackageAllStructs(t *testing.T) {
	ctx := extractOne(t, map[string]string{
		"a.go": `package config

type Database struct {
	DSN string ` + "`toml:\"dsn\"`" + `
}
`,
	})
	ids := recordIDs(extractConfigKeys(ctx))
	if !ids["config_key|dsn"] {
		t.Errorf("struct in package config is a root, got %v", ids)
	}
}
