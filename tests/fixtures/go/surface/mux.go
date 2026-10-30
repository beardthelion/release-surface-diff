package main

import (
	"net/http"

	"github.com/gorilla/mux"
)

func getItem(w http.ResponseWriter, r *http.Request) {}
func putItem(w http.ResponseWriter, r *http.Request) {}

func gorillaRoutes() {
	r := mux.NewRouter()
	r.HandleFunc("/items/{id:[0-9]+}", getItem).Methods("GET")
	r.Path("/items/{id}").Methods("PUT").HandlerFunc(putItem)
	r.PathPrefix("/static").Handler(http.StripPrefix("/static", http.FileServer(http.Dir("."))))
}
