package main

import (
	"github.com/spf13/cobra"
	cli "github.com/urfave/cli/v2"
)

func buildCmd() {
	cmd := &cobra.Command{Use: "serve"}
	cmd.Flags().String("config", "", "config file")
	cmd.PersistentFlags().IntP("port", "p", 8080, "port")
	app := &cli.App{
		Flags: []cli.Flag{
			&cli.StringFlag{Name: "verbose, v"},
		},
	}
	_ = app
}
