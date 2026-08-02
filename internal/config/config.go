package config

import (
	"os"
	"time"

	"gopkg.in/yaml.v3"
)

type SchedulerConfig struct {
	AnalyzeCron string        `yaml:"analyze_cron"`
	SendCron    string        `yaml:"send_cron"`
	Jitter      time.Duration `yaml:"jitter"`
}

type Config struct {
	VictoriaMetrics VMConfig         `yaml:"victoria_metrics"`
	Targets         []TargetConfig   `yaml:"targets"`
	Analysis        AnalysisConfig   `yaml:"analysis"`
	Containers      ContainersConfig `yaml:"containers"`
	Scheduler       SchedulerConfig  `yaml:"scheduler"`
	Notifier        NotifierConfig   `yaml:"notifier"`
}

type VMConfig struct {
	URL           string        `yaml:"url"`
	Timeout       time.Duration `yaml:"timeout"`
	MaxConcurrent int           `yaml:"max_concurrent"`
	RPS           float64       `yaml:"rps"`
	Retries       int           `yaml:"retries"`
}

type TargetConfig struct {
	Name        string   `yaml:"name"`
	Instance    string   `yaml:"instance"`
	Mountpoints []string `yaml:"mountpoints,omitempty"`
	Description string   `yaml:"description,omitempty"`
}

type AnalysisConfig struct {
	CPU     bool     `yaml:"cpu"`
	Memory  bool     `yaml:"memory"`
	Disk    bool     `yaml:"disk"`
	OOM     bool     `yaml:"oom"`
	Periods []string `yaml:"periods"`
}

type ContainersConfig struct {
	Enabled         bool              `yaml:"enabled"`
	ChangeThreshold float64           `yaml:"change_threshold"`
	HighThreshold   float64           `yaml:"high_threshold"`
	CPUThreshold    float64           `yaml:"cpu_threshold"`
	MemThreshold    float64           `yaml:"mem_threshold"`
	Filters         map[string]string `yaml:"filters,omitempty"`
}

type MessengerConfig struct {
	Enabled     bool   `yaml:"enabled"`
	APIURL      string `yaml:"api_url"`
	ChatID      string `yaml:"chat_id"`
	BearerToken string `yaml:"bearer_token"`
}

type NotifierConfig struct {
	BotX MessengerConfig `yaml:"botx"`
}

func LoadConfig(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}

	var cfg Config
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, err
	}

	if cfg.VictoriaMetrics.Timeout == 0 {
		cfg.VictoriaMetrics.Timeout = 30 * time.Second
	}
	if cfg.VictoriaMetrics.MaxConcurrent == 0 {
		cfg.VictoriaMetrics.MaxConcurrent = 8
	}
	if cfg.VictoriaMetrics.RPS == 0 {
		cfg.VictoriaMetrics.RPS = 20
	}
	if cfg.VictoriaMetrics.Retries == 0 {
		cfg.VictoriaMetrics.Retries = 3
	}

	if cfg.Scheduler.Jitter == 0 {
		cfg.Scheduler.Jitter = 30 * time.Second
	}

	if !cfg.Analysis.CPU && !cfg.Analysis.Memory && !cfg.Analysis.Disk && !cfg.Analysis.OOM {
		cfg.Analysis.CPU = true
		cfg.Analysis.Memory = true
		cfg.Analysis.Disk = true
		cfg.Analysis.OOM = true
	}

	if len(cfg.Analysis.Periods) == 0 {
		cfg.Analysis.Periods = []string{"1d", "7d", "14d"}
	}

	if cfg.Containers.ChangeThreshold == 0 {
		cfg.Containers.ChangeThreshold = 5
	}
	if cfg.Containers.HighThreshold == 0 {
		cfg.Containers.HighThreshold = 70
	}
	if cfg.Containers.CPUThreshold == 0 {
		cfg.Containers.CPUThreshold = 80
	}
	if cfg.Containers.MemThreshold == 0 {
		cfg.Containers.MemThreshold = 95
	}

	if envToken := os.Getenv("BOTX_BEARER_TOKEN"); envToken != "" {
		cfg.Notifier.BotX.BearerToken = envToken
	}
	if envChatID := os.Getenv("BOTX_CHAT_ID"); envChatID != "" {
		cfg.Notifier.BotX.ChatID = envChatID
	}

	return &cfg, nil
}
