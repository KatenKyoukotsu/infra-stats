package vmclient

import (
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"time"
)

type Client struct {
	baseURL    string
	httpClient *http.Client
}

func NewClient(baseURL string, timeout time.Duration) *Client {
	slog.Debug("Creating VictoriaMetrics client",
		slog.String("url", baseURL),
		slog.Duration("timeout", timeout),
	)
	return &Client{
		baseURL: baseURL,
		httpClient: &http.Client{
			Timeout: timeout,
		},
	}
}

type QueryResult struct {
	Status string `json:"status"`
	Data   Data   `json:"data"`
}

type Data struct {
	ResultType string   `json:"resultType"`
	Result     []Result `json:"result"`
}

type Result struct {
	Metric map[string]string `json:"metric"`
	Value  []interface{}     `json:"value"`
	Values []interface{}     `json:"values"`
}

func (c *Client) Ping() error {
	u := fmt.Sprintf("%s/api/v1/query?query=%s", c.baseURL, url.QueryEscape("up{}"))
	start := time.Now()
	resp, err := c.httpClient.Get(u)
	duration := time.Since(start)
	if err != nil {
		return fmt.Errorf("vm ping failed: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("vm ping returned status %d", resp.StatusCode)
	}
	slog.Debug("VM ping ok", slog.Duration("duration", duration))
	return nil
}

func (c *Client) QueryInstant(query string) (float64, error) {
	u := fmt.Sprintf("%s/api/v1/query?query=%s", c.baseURL, url.QueryEscape(query))

	slog.Debug("VM query", slog.String("url", u))

	start := time.Now()
	resp, err := c.httpClient.Get(u)
	duration := time.Since(start)

	if err != nil {
		slog.Error("VM HTTP request failed",
			slog.String("error", err.Error()),
			slog.Duration("duration", duration),
		)
		return 0, fmt.Errorf("vm query failed: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return 0, fmt.Errorf("read response failed: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		slog.Error("VM returned non-200",
			slog.Int("status", resp.StatusCode),
			slog.String("body", string(body)),
		)
		return 0, fmt.Errorf("vm returned status %d: %s", resp.StatusCode, string(body))
	}

	var result QueryResult
	if err := json.Unmarshal(body, &result); err != nil {
		return 0, fmt.Errorf("vm parse failed: %w", err)
	}

	if result.Status != "success" {
		return 0, fmt.Errorf("vm query not successful: %s", result.Status)
	}

	if len(result.Data.Result) == 0 {
		slog.Debug("VM query returned no data", slog.String("query", query))
		return 0, fmt.Errorf("no data returned for query")
	}

	valStr, ok := result.Data.Result[0].Value[1].(string)
	if !ok {
		return 0, fmt.Errorf("unexpected value format")
	}

	var val float64
	if _, err := fmt.Sscanf(valStr, "%f", &val); err != nil {
		return 0, fmt.Errorf("parse value %q failed: %w", valStr, err)
	}

	slog.Debug("VM query result",
		slog.String("query", query),
		slog.Float64("value", val),
		slog.Duration("duration", duration),
	)

	return val, nil
}
