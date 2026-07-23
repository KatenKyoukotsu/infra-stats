package notifier

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"ssh-checker/internal/checker"
	"ssh-checker/internal/config"
)

type Client struct {
	httpClient *http.Client
}

func NewClient() *Client {
	return &Client{
		httpClient: &http.Client{
			Timeout: 10 * time.Second,
		},
	}
}

type BotXPayload struct {
	ChatID string `json:"chat_id"`
	Text   string `json:"text"`
}

func (c *Client) SendReport(cfg config.MessengerConfig, report checker.CheckReport) error {
	if cfg.APIURL == "" || cfg.BearerToken == "" || cfg.ChatID == "" {
		slog.Error("BotX configuration is incomplete",
			slog.Bool("has_url", cfg.APIURL != ""),
			slog.Bool("has_token", cfg.BearerToken != ""),
			slog.Bool("has_chat_id", cfg.ChatID != ""),
		)
		return fmt.Errorf("botx config is incomplete")
	}

	text := c.formatReport(report)

	payload := BotXPayload{
		ChatID: cfg.ChatID,
		Text:   text,
	}

	body, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("failed to marshal botx payload: %w", err)
	}

	req, err := http.NewRequest(http.MethodPost, cfg.APIURL, bytes.NewBuffer(body))
	if err != nil {
		return fmt.Errorf("failed to create http request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+cfg.BearerToken)

	start := time.Now()
	resp, err := c.httpClient.Do(req)
	duration := time.Since(start)

	if err != nil {
		slog.Error("HTTP request to BotX failed", slog.String("url", cfg.APIURL), slog.String("error", err.Error()), slog.Duration("duration", duration))
		return fmt.Errorf("failed to send request to botx: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		slog.Error("BotX API returned error status", slog.Int("status_code", resp.StatusCode), slog.String("chat_id", cfg.ChatID))
		return fmt.Errorf("botx API returned non-2xx status code: %d", resp.StatusCode)
	}

	slog.Info("Report successfully sent to BotX", slog.String("chat_id", cfg.ChatID), slog.Duration("duration", duration))
	return nil
}

func (c *Client) formatReport(report checker.CheckReport) string {
	var sb strings.Builder

	sb.WriteString("📊 *SSH Services Health Report*\n")
	sb.WriteString(fmt.Sprintf("🕒 *Time:* %s\n", report.Timestamp.Format("2006-01-02 15:04:05")))
	sb.WriteString(fmt.Sprintf("🖥 *Total VMs:* %d | ✅ *Healthy:* %d | ❌ *Unhealthy:* %d\n\n",
		report.TotalVMs, report.HealthyVMs, report.UnhealthyVMs))

	for _, vm := range report.Results {
		icon := "✅"
		if vm.Status == "unhealthy" {
			icon = "⚠️"
		} else if vm.Status == "unreachable" {
			icon = "🚨"
		}

		sb.WriteString(fmt.Sprintf("%s *%s* (`%s`)\n", icon, vm.VMName, vm.Host))
		if vm.Uptime != "" {
			sb.WriteString(fmt.Sprintf("   ⏱ Uptime: %s\n", vm.Uptime))
		}
		if vm.HasOOM {
			sb.WriteString(fmt.Sprintf("   ⚠️ *OOM Event:* `%s`\n", vm.OOMDetail))
		}

		if !vm.IsOnline {
			sb.WriteString(fmt.Sprintf("   ❌ Error: %s\n", vm.Error))
		} else {
			for _, item := range vm.Items {
				statusIcon := "🟢"
				if !item.IsAlive {
					statusIcon = "🔴"
				}
				sb.WriteString(fmt.Sprintf("   • [%s] %s: %s %s\n",
					item.Type, item.Name, statusIcon, item.Details))
			}
		}
		sb.WriteString("\n")
	}

	return sb.String()
}