// Package config loads and validates the config file.
//
// The config lives in the repo's top-level config/config.yaml so an operator
// can edit it without touching the code. Host/port may be overridden by
// environment (CH_HOST/CH_PORT), and --config points at an alternate file. No
// credentials live in code or config (the target database uses the default user
// with no password).
package config

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"gopkg.in/yaml.v3"

	"github.com/vishal9359/Analysis-FW/internal/fwerr"
)

// StoreConfig is the database section. kind + options select and configure the
// adapter; everything else is generic.
type StoreConfig struct {
	Kind      string                 `yaml:"kind"`
	Host      string                 `yaml:"host"`
	Port      int                    `yaml:"port"`
	Database  string                 `yaml:"-"` // from producer.database
	BatchSize int                    `yaml:"batch_size"`
	Workers   int                    `yaml:"workers"`
	Options   map[string]interface{} `yaml:"options"`
}

// Config is the whole validated configuration.
type Config struct {
	ProducerName string
	Store        StoreConfig
	LogLevel     string
	LogFormat    string
}

type rawConfig struct {
	Producer struct {
		Name     string `yaml:"name"`
		Database string `yaml:"database"`
	} `yaml:"producer"`
	Store   StoreConfig `yaml:"store"`
	Logging struct {
		Level  string `yaml:"level"`
		Format string `yaml:"format"`
	} `yaml:"logging"`
}

// DefaultPath is config/config.yaml resolved next to the executable's module
// root. Callers normally pass an explicit path or rely on Load's search.
func DefaultPath() string {
	// Prefer a config/ directory beside the working directory (repo root),
	// then beside the executable (installed binary).
	if p := filepath.Join("config", "config.yaml"); fileExists(p) {
		return p
	}
	if exe, err := os.Executable(); err == nil {
		p := filepath.Join(filepath.Dir(exe), "config", "config.yaml")
		if fileExists(p) {
			return p
		}
	}
	return filepath.Join("config", "config.yaml")
}

// Load reads and validates the config. An empty path uses DefaultPath.
func Load(path string) (*Config, error) {
	if path == "" {
		path = DefaultPath()
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fwerr.Config("config: config file not found at %s", path)
	}

	var raw rawConfig
	if err := yaml.Unmarshal(data, &raw); err != nil {
		return nil, fwerr.ConfigWrap(err, "config: cannot parse %s", path)
	}

	if raw.Producer.Name == "" {
		return nil, fwerr.Config("config: missing 'producer.name'")
	}
	if raw.Producer.Database == "" {
		return nil, fwerr.Config("config: missing 'producer.database'")
	}
	if raw.Store.Host == "" {
		return nil, fwerr.Config("config: missing 'store.host'")
	}
	if raw.Store.Port == 0 {
		return nil, fwerr.Config("config: missing 'store.port'")
	}

	st := raw.Store
	st.Database = raw.Producer.Database
	if st.Kind == "" {
		st.Kind = "clickhouse"
	}
	if st.Workers == 0 {
		st.Workers = 4
	}
	if st.BatchSize == 0 {
		st.BatchSize = 100000
	}
	if st.Options == nil {
		st.Options = map[string]interface{}{}
	}

	// env overrides, so one config file works on the dev box and the server
	if v := os.Getenv("CH_HOST"); v != "" {
		st.Host = v
	}
	if v := os.Getenv("CH_PORT"); v != "" {
		p, err := strconv.Atoi(v)
		if err != nil {
			return nil, fwerr.Config("config: CH_PORT is not a number: %q", v)
		}
		st.Port = p
	}

	if st.Workers < 1 {
		return nil, fwerr.Config("config: store.workers must be >= 1")
	}
	if st.BatchSize < 1 {
		return nil, fwerr.Config("config: store.batch_size must be >= 1")
	}

	level := strings.ToUpper(raw.Logging.Level)
	if level == "" {
		level = "INFO"
	}
	format := strings.ToLower(raw.Logging.Format)
	if format == "" {
		format = "json"
	}

	return &Config{
		ProducerName: raw.Producer.Name,
		Store:        st,
		LogLevel:     level,
		LogFormat:    format,
	}, nil
}

func fileExists(p string) bool {
	fi, err := os.Stat(p)
	return err == nil && !fi.IsDir()
}
