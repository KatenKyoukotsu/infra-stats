package config

import (
	"fmt"
	"os"
	"sync"

	"gopkg.in/yaml.v3"
)

// TargetVM описывает параметры целевой виртуальной машины и проверяемые элементы
type TargetVM struct {
	ID         string   `yaml:"id" json:"id"`
	Name       string   `yaml:"name" json:"name"`
	Host       string   `yaml:"host" json:"host"` // IP или FQDN
	Port       int      `yaml:"port" json:"port"` // Обычно 22
	User       string   `yaml:"user" json:"user"`
	SSHKeyPath string   `yaml:"ssh_key_path" json:"ssh_key_path"` // Путь к приватного ключу в репо
	Systemd    []string `yaml:"systemd" json:"systemd"`           // Список сервисов для проверки
	Containers []string `yaml:"containers" json:"containers"`     // Список Docker-контейнеров
}

// MessengerConfig описывает параметры подключения к BotX
type MessengerConfig struct {
	APIURL      string `yaml:"api_url" json:"api_url"`
	BearerToken string `yaml:"bearer_token" json:"bearer_token"`
	ChatID      string `yaml:"chat_id" json:"chat_id"`
}

// SchedulerConfig задает время работы по крону
type SchedulerConfig struct {
	CheckCron string `yaml:"check_cron" json:"check_cron"` // Например: "0 0 * * *" (в 00:00)
	SendCron  string `yaml:"send_cron" json:"send_cron"`   // Например: "0 8 * * *" (в 08:00)
}

// Config — общая структура конфигурации приложения
type Config struct {
	ServerPort string          `yaml:"server_port" json:"server_port"`
	Messenger  MessengerConfig `yaml:"messenger" json:"messenger"`
	Scheduler  SchedulerConfig `yaml:"scheduler" json:"scheduler"`
	Targets    []TargetVM      `yaml:"targets" json:"targets"`
}

// Manager обеспечивает безопасную работу с конфигом в конкурентной среде
type Manager struct {
	mu       sync.RWMutex
	filePath string
	cfg      *Config
}

// NewManager создает новый менеджер конфигурации и загружает файл
func NewManager(filePath string) (*Manager, error) {
	m := &Manager{
		filePath: filePath,
	}
	if err := m.Load(); err != nil {
		return nil, err
	}
	return m, nil
}

// Load считывает конфигурацию из файла YAML
func (m *Manager) Load() error {
	m.mu.Lock()
	defer m.mu.Unlock()

	data, err := os.ReadFile(m.filePath)
	if err != nil {
		return fmt.Errorf("failed to read config file: %w", err)
	}

	var cfg Config
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return fmt.Errorf("failed to unmarshal yaml config: %w", err)
	}

	// Дефолтные значения, если не заданы
	if cfg.ServerPort == "" {
		cfg.ServerPort = "8080"
	}

	m.cfg = &cfg
	return nil
}

// Save сохраняет текущую конфигурацию обратно в YAML-файл
func (m *Manager) Save(newCfg *Config) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	data, err := yaml.Marshal(newCfg)
	if err != nil {
		return fmt.Errorf("failed to marshal config to yaml: %w", err)
	}

	// Записываем файл атомарно с правами 0644
	if err := os.WriteFile(m.filePath, data, 0644); err != nil {
		return fmt.Errorf("failed to write config file: %w", err)
	}

	m.cfg = newCfg
	return nil
}

// Get возвращает копию текущей конфигурации для безопасного чтения
func (m *Manager) Get() Config {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return *m.cfg
}