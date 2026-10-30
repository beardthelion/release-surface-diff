package main

// Config is the service configuration root. Fields carry toml tags;
// nested sections produce dotted keys.
type Config struct {
	Server serverSection `toml:"server"`
	Debug  bool          `toml:"debug"`
	Secret string        `toml:"-"`
}

type serverSection struct {
	Host string `toml:"host"`
	Port int    `toml:"port"`
	TLS  tlsSection
}

type tlsSection struct {
	Enabled bool `toml:"enabled"`
}
