package config

import (
	"fmt"
	"os"
	"sync"

	"gopkg.in/yaml.v3"
)

type Manager struct {
	mu   sync.RWMutex
	path string
	cfg  *Config
}

func NewManager(path string) (*Manager, error) {
	cfg, err := LoadConfig(path)
	if err != nil {
		return nil, fmt.Errorf("failed to load initial config: %w", err)
	}

	return &Manager{
		path: path,
		cfg:  cfg,
	}, nil
}

func (m *Manager) Get() *Config {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.cfg
}

func (m *Manager) Save(newCfg *Config) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	data, err := yaml.Marshal(newCfg)
	if err != nil {
		return fmt.Errorf("failed to marshal config: %w", err)
	}

	if err := os.WriteFile(m.path, data, 0644); err != nil {
		return fmt.Errorf("failed to write config file: %w", err)
	}

	m.cfg = newCfg
	return nil
}