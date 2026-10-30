package main

import (
	"flag"
	"net/http"
)

var healthPath = "/healthz"

func hello(w http.ResponseWriter, r *http.Request) {}

func auth(h http.Handler) http.Handler { return h }

func ExportedHelper() {}

func main() {
	flag.Int("timeout", 30, "request timeout")
	mux := http.NewServeMux()
	http.HandleFunc("/hello", hello)
	mux.HandleFunc(healthPath, hello)
	mux.Handle("GET /wrapped", auth(http.HandlerFunc(hello)))
	http.ListenAndServe(":8080", mux)
}
