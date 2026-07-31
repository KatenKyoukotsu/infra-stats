package notifier

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"time"

	"infra-stats/internal/analyzer"
	"infra-stats/internal/config"
)

type NotificationRecord struct {
	Timestamp time.Time `json:"timestamp"`
	Success   bool      `json:"success"`
	Error     string    `json:"error,omitempty"`
	ChatID    string    `json:"chat_id"`
}

type Client struct {
	mu           sync.RWMutex
	httpClient   *http.Client
	history      []NotificationRecord
	maxHistory   int
}

func NewClient() *Client {
	slog.Debug("Creating BotX notifier client")
	return &Client{
		httpClient: &http.Client{
			Timeout: 10 * time.Second,
		},
		history:    make([]NotificationRecord, 0, 50),
		maxHistory: 50,
	}
}

func (c *Client) Notifications() []NotificationRecord {
	c.mu.RLock()
	defer c.mu.RUnlock()
	out := make([]NotificationRecord, len(c.history))
	copy(out, c.history)
	return out
}

func (c *Client) addRecord(r NotificationRecord) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(c.history) >= c.maxHistory {
		c.history = c.history[1:]
	}
	c.history = append(c.history, r)
}

type BotXPayload struct {
	ChatID string `json:"chat_id"`
	Text   string `json:"text"`
}

func (c *Client) SendReport(cfg config.MessengerConfig, report analyzer.AnalysisReport) error {
	rec := NotificationRecord{
		Timestamp: time.Now(),
		ChatID:    cfg.ChatID,
	}

	if !cfg.Enabled {
		slog.Debug("BotX notifier disabled, skipping")
		rec.Success = true
		c.addRecord(rec)
		return nil
	}

	if cfg.APIURL == "" || cfg.BearerToken == "" || cfg.ChatID == "" {
		err := fmt.Errorf("botx config is incomplete")
		slog.Error("BotX configuration is incomplete",
			slog.Bool("has_url", cfg.APIURL != ""),
			slog.Bool("has_token", cfg.BearerToken != ""),
			slog.Bool("has_chat_id", cfg.ChatID != ""),
		)
		rec.Error = err.Error()
		c.addRecord(rec)
		return err
	}

	text := c.FormatReport(report)
	slog.Debug("Formatted BotX report", slog.Int("length", len(text)))

	payload := BotXPayload{
		ChatID: cfg.ChatID,
		Text:   text,
	}

	body, err := json.Marshal(payload)
	if err != nil {
		rec.Error = err.Error()
		c.addRecord(rec)
		return fmt.Errorf("failed to marshal botx payload: %w", err)
	}

	req, err := http.NewRequest(http.MethodPost, cfg.APIURL, bytes.NewBuffer(body))
	if err != nil {
		rec.Error = err.Error()
		c.addRecord(rec)
		return fmt.Errorf("failed to create http request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+cfg.BearerToken)

	start := time.Now()
	resp, err := c.httpClient.Do(req)
	duration := time.Since(start)

	if err != nil {
		rec.Error = err.Error()
		c.addRecord(rec)
		slog.Error("HTTP request to BotX failed",
			slog.String("url", cfg.APIURL),
			slog.String("error", err.Error()),
			slog.Duration("duration", duration),
		)
		return fmt.Errorf("failed to send request to botx: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		rec.Error = fmt.Sprintf("HTTP %d", resp.StatusCode)
		c.addRecord(rec)
		slog.Error("BotX API returned error status",
			slog.Int("status_code", resp.StatusCode),
			slog.String("chat_id", cfg.ChatID),
		)
		return fmt.Errorf("botx API returned non-2xx status code: %d", resp.StatusCode)
	}

	rec.Success = true
	c.addRecord(rec)
	slog.Info("Report sent to BotX",
		slog.String("chat_id", cfg.ChatID),
		slog.Duration("duration", duration),
	)
	return nil
}

func (c *Client) Validate(cfg config.MessengerConfig) error {
	if !cfg.Enabled {
		return fmt.Errorf("botx notifier is disabled")
	}
	if cfg.APIURL == "" {
		return fmt.Errorf("botx api_url is empty")
	}
	if strings.Contains(cfg.APIURL, "${") {
		return fmt.Errorf("botx api_url contains unresolved placeholder %q", cfg.APIURL)
	}
	if cfg.BearerToken == "" {
		return fmt.Errorf("botx bearer_token is empty")
	}
	if strings.Contains(cfg.BearerToken, "${") {
		return fmt.Errorf("botx bearer_token contains unresolved placeholder %q", cfg.BearerToken)
	}
	if cfg.ChatID == "" {
		return fmt.Errorf("botx chat_id is empty")
	}
	if strings.Contains(cfg.ChatID, "${") {
		return fmt.Errorf("botx chat_id contains unresolved placeholder %q", cfg.ChatID)
	}
	return nil
}

func (c *Client) FormatReport(report analyzer.AnalysisReport) string {
	var sb strings.Builder

	sb.WriteString("📊 *Infra Stats Report*\n")
	sb.WriteString(fmt.Sprintf("🕒 *Time:* %s\n\n", report.Timestamp.Format("2006-01-02 15:04:05")))

	for _, t := range report.Targets {
		sb.WriteString(fmt.Sprintf("🖥 *%s*\n", t.Name))

		if len(t.CPU) > 0 {
			parts := make([]string, 0, len(t.CPU))
			for _, m := range t.CPU {
				parts = append(parts, formatMetricWithDiff(m.Period, m.Value, m.Diff)+"%")
			}
			sb.WriteString(fmt.Sprintf("   📈 CPU: %s\n", strings.Join(parts, " | ")))
		}

		if len(t.Memory) > 0 {
			parts := make([]string, 0, len(t.Memory))
			for _, m := range t.Memory {
				parts = append(parts, formatMetricWithDiff(m.Period, m.Value, m.Diff)+"%")
			}
			sb.WriteString(fmt.Sprintf("   📈 Mem: %s\n", strings.Join(parts, " | ")))
		}

		for _, d := range t.Disks {
			parts := make([]string, 0, len(d.Metrics))
			for _, m := range d.Metrics {
				parts = append(parts, formatMetricWithDiff(m.Period, m.Value, m.Diff)+"%")
			}
			mp := d.Mountpoint
			if mp == "/" {
				mp = "root"
			}
			sb.WriteString(fmt.Sprintf("   💾 %s: %s\n", mp, strings.Join(parts, " | ")))
		}

		for _, o := range t.OOM {
			diffStr := ""
			if o.Diff != nil {
				prefix := "+"
				if *o.Diff < 0 {
					prefix = ""
				}
				diffStr = fmt.Sprintf(" (%s%d)", prefix, *o.Diff)
			}
			sb.WriteString(fmt.Sprintf("   💀 OOM (%s): %d kill(s)%s\n", o.Period, o.Count, diffStr))
		}

		sb.WriteString("\n")
	}

	return sb.String()
}

func formatMetricWithDiff(period string, value float64, diff *float64) string {
	if diff == nil {
		return fmt.Sprintf("%s: %.1f", period, value)
	}
	prefix := "+"
	if *diff < 0 {
		prefix = ""
	}
	return fmt.Sprintf("%s: %.1f (%s%.1f)", period, value, prefix, *diff)
}
