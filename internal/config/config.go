package config

import (
	"fmt"
	"log/slog"
	"os"
	"sync"

	"gopkg.in/yaml.v3"
)

type HTTPCheck struct {
	URL              string `yaml:"url" json:"url"`
	ValidStatusCodes []int  `yaml:"valid_status_codes,omitempty" json:"valid_status_codes,omitempty"`
}

type TargetVM struct {
	ID         string      `yaml:"id" json:"id"`
	Name       string      `yaml:"name" json:"name"`
	Host       string      `yaml:"host" json:"host"`
	Port       int         `yaml:"port" json:"port"`
	User       string      `yaml:"user" json:"user"`
	SSHKeyPath string      `yaml:"ssh_key_path" json:"ssh_key_path"`
	Systemd    []string    `yaml:"systemd,omitempty" json:"systemd,omitempty"`
	Containers []string    `yaml:"containers,omitempty" json:"containers,omitempty"`
	HTTPChecks []HTTPCheck `yaml:"http_checks,omitempty" json:"http_checks,omitempty"`
}

type SchedulerConfig struct {
	CheckCron string `yaml:"check_cron" json:"check_cron"`
	SendCron  string `yaml:"send_cron" json:"send_cron"`
}

type MessengerConfig struct {
	APIURL      string `yaml:"api_url" json:"api_url"`
	BearerToken string `yaml:"bearer_token" json:"bearer_token"`
	ChatID      string `yaml:"chat_id" json:"chat_id"`
}

type Config struct {
	ServerPort string          `yaml:"server_port" json:"server_port"`
	Scheduler  SchedulerConfig `yaml:"scheduler" json:"scheduler"`
	Messenger  MessengerConfig `yaml:"messenger" json:"messenger"`
	Targets    []TargetVM      `yaml:"targets" json:"targets"`
}

type Manager struct {
	mu       sync.RWMutex
	filePath string
	cfg      *Config
}

func NewManager(filePath string) (*Manager, error) {
	m := &Manager{filePath: filePath}
	if err := m.Load(); err != nil {
		return nil, err
	}
	return m, nil
}

func (m *Manager) Load() error {
	m.mu.Lock()
	defer m.mu.Unlock()

	data, err := os.ReadFile(m.filePath)
	if err != nil {
		return fmt.Errorf("failed to read config file: %w", err)
	}

	var cfg Config
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return fmt.Errorf("failed to parse yaml config: %w", err)
	}

	if cfg.ServerPort == "" {
		cfg.ServerPort = "8080"
	}

	m.cfg = &cfg
	slog.Info("Configuration loaded successfully", slog.String("file", m.filePath), slog.Int("targets_count", len(cfg.Targets)))
	return nil
}

func (m *Manager) Get() Config {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return *m.cfg
}

func (m *Manager) Save(newCfg *Config) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	data, err := yaml.Marshal(newCfg)
	if err != nil {
		return fmt.Errorf("failed to marshal config: %w", err)
	}

	if err := os.WriteFile(m.filePath, data, 0644); err != nil {
		return fmt.Errorf("failed to write config file: %w", err)
	}

	m.cfg = newCfg
	slog.Info("Configuration updated and saved to file", slog.String("file", m.filePath))
	return nil
}