package notifier

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"ssh-checker/internal/checker"
	"ssh-checker/internal/config"
)

// BotXPayload описывает тело запроса к апи BotX
type BotXPayload struct {
	GroupChatID  string       `json:"group_chat_id"`
	Notification Notification `json:"notification"`
}

type Notification struct {
	Body string `json:"body"`
}

// Client отвечает за отправку уведомлений
type Client struct {
	httpClient *http.Client
}

// NewClient создает экземпляр клиента
func NewClient() *Client {
	return &Client{
		httpClient: &http.Client{
			Timeout: 10 * time.Second,
		},
	}
}

// SendReport форматирует и отправляет отчет в BotX
func (c *Client) SendReport(cfg config.MessengerConfig, report checker.CheckReport) error {
	if cfg.APIURL == "" || cfg.BearerToken == "" || cfg.ChatID == "" {
		return fmt.Errorf("messenger configuration is incomplete")
	}

	// Формируем текстовое сообщение из отчета
	msgText := formatReportText(report)

	payload := BotXPayload{
		GroupChatID: cfg.ChatID,
		Notification: Notification{
			Body: msgText,
		},
	}

	jsonData, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("failed to marshal botx payload: %w", err)
	}

	req, err := http.NewRequest(http.MethodPost, cfg.APIURL, bytes.NewBuffer(jsonData))
	if err != nil {
		return fmt.Errorf("failed to create http request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", cfg.BearerToken))

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("failed to send http request to botx: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("botx API returned non-2xx status code: %d", resp.StatusCode)
	}

	return nil
}

// formatReportText генерирует понятное текстовое представление отчета
func formatReportText(report checker.CheckReport) string {
	var buf bytes.Buffer

	buf.WriteString(fmt.Sprintf("📊 **Отчет о состоянии ВМ на %s**\n", report.Timestamp.Format("02.01.2006 15:04")))
	buf.WriteString(fmt.Sprintf("Всего ВМ: %d | 🟢 Healthy: %d | 🔴 Unhealthy/Down: %d\n\n",
		report.TotalVMs, report.HealthyVMs, report.UnhealthyVMs))

	if report.UnhealthyVMs == 0 {
		buf.WriteString("✅ Все виртуальные машины и сервисы работают в штатном режиме!")
		return buf.String()
	}

	buf.WriteString("⚠️ **Проблемные узлы:**\n")

	for _, vm := range report.Results {
		if vm.Status == "healthy" {
			continue
		}

		if !vm.IsOnline {
			buf.WriteString(fmt.Sprintf("\n❌ **%s (%s)** - Недоступен по SSH: %s\n", vm.VMName, vm.Host, vm.Error))
			continue
		}

		buf.WriteString(fmt.Sprintf("\n🔴 **%s (%s)**:\n", vm.VMName, vm.Host))
		for _, item := range vm.Items {
			if !item.IsAlive {
				buf.WriteString(fmt.Sprintf("  • [%s] %s: %s\n", item.Type, item.Name, item.Details))
			}
		}
	}

	return buf.String()
}