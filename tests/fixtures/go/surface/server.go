package main

import (
	"net/http"

	"github.com/go-chi/chi/v5"
)

func getUser(w http.ResponseWriter, r *http.Request)    { _ = loadUser(1) }
func createUser(w http.ResponseWriter, r *http.Request) {}
func deleteUser(w http.ResponseWriter, r *http.Request) {}
func adminStats(w http.ResponseWriter, r *http.Request) {}

func loadUser(id int) int { return id }

func authMw(next http.Handler) http.Handler  { return next }
func adminMw(next http.Handler) http.Handler { return next }

func chiRoutes() {
	r := chi.NewRouter()
	r.Use(authMw)
	r.Get("/users/{id}", getUser)
	r.Post("/users", createUser)
	r.With(adminMw).Delete("/users/{id}", deleteUser)
	r.Route("/admin", func(r chi.Router) {
		r.Use(adminMw)
		r.Get("/stats", adminStats)
	})
}
