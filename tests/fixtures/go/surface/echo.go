package main

import "github.com/labstack/echo/v4"

func echoHandler(c echo.Context) error { return nil }

func echoRoutes() {
	e := echo.New()
	e.GET("/status", echoHandler)
	grp := e.Group("/v2")
	grp.POST("/items", echoHandler)
}
