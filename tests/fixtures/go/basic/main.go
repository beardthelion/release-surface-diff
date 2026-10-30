package main

import "net/http"

func hello(w http.ResponseWriter, r *http.Request) {}

func main() {
	http.HandleFunc("/hello", hello)
	http.ListenAndServe(":8080", nil)
}
