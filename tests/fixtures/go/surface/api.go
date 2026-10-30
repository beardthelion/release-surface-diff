package main

import (
	"github.com/gin-gonic/gin"
)

func login(c *gin.Context)   {}
func ping(c *gin.Context)    {}
func jwtMw(c *gin.Context)   {}
func auditMw(c *gin.Context) {}

func ginRoutes() {
	e := gin.Default()
	api := e.Group("/api", jwtMw)
	api.POST("/login", login)
	api.GET("/ping", auditMw, ping)
	e.GET("/healthz", ping)
}
