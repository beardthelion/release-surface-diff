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

func TestConfigKeysSliceAndMapFields(t *testing.T) {
	ctx := extractOne(t, map[string]string{
		"a.go": `package demo

type Config struct {
	Servers []ServerConfig ` + "`toml:\"servers\"`" + `
	Subs    map[string]Sub ` + "`toml:\"subs\"`" + `
}

type ServerConfig struct {
	Host string ` + "`toml:\"host\"`" + `
}

type Sub struct {
	V int ` + "`toml:\"v\"`" + `
}
`,
	})
	ids := recordIDs(extractConfigKeys(ctx))
	for _, want := range []string{
		"servers", "servers.host", "subs", "subs.v",
	} {
		if !ids["config_key|"+want] {
			t.Errorf("missing config key %s, got %v", want, ids)
		}
	}
}

func TestConfigMutualRoots(t *testing.T) {
	ctx := extractOne(t, map[string]string{
		"a.go": `package config

type Alpha struct {
	B    Beta   ` + "`toml:\"b\"`" + `
	Name string ` + "`toml:\"name\"`" + `
}

type Beta struct {
	A    Alpha ` + "`toml:\"a\"`" + `
	Port int   ` + "`toml:\"port\"`" + `
}
`,
	})
	recs := extractConfigKeys(ctx)
	if len(recs) == 0 {
		t.Fatal("mutually-referencing roots emitted no config keys")
	}
	ids := recordIDs(recs)
	// Alpha sorts first, so Beta's keys appear under Alpha's "b" field
	// and Beta is not re-emitted as a standalone root.
	for _, want := range []string{"name", "b", "b.port"} {
		if !ids["config_key|"+want] {
			t.Errorf("missing config key %s, got %v", want, ids)
		}
	}
	if ids["config_key|port"] {
		t.Error("nested root re-emitted as standalone: got top-level 'port'")
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
