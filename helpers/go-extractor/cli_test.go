package main

import (
	"testing"
)

func TestFlagSetVarFlags(t *testing.T) {
	ctx := extractOne(t, map[string]string{
		"a.go": `package demo

import "flag"

func f() {
	fs := flag.NewFlagSet("sub", flag.ContinueOnError)
	fs.Bool("verbose", false, "chatty")
	fs.StringVar(&dst, "out", "", "output")
	flag.CommandLine.Int("top", 0, "top level")
}

var dst string
`,
	})
	ids := recordIDs(extractCLIFlags(ctx))
	for _, want := range []string{"--verbose", "--out", "--top"} {
		if !ids["cli_flag|"+want] {
			t.Errorf("missing flag %s, got %v", want, ids)
		}
	}
}

func TestNonFlagSelectorNotAFlag(t *testing.T) {
	ctx := extractOne(t, map[string]string{
		"a.go": `package demo

func f(s Stringer) {
	s.String("name", 0, "x")
}

type Stringer interface {
	String(string, int, string) *string
}
`,
	})
	if recs := extractCLIFlags(ctx); len(recs) != 0 {
		t.Errorf("non-flag selector should not emit: %+v", recs)
	}
}
