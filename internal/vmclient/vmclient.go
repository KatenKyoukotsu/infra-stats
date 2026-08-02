package vmclient

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"math"
	"math/rand"
	"net/http"
	"net/url"
	"strconv"
	"sync"
	"time"
)

const (
	defaultMaxConcurrent = 8
	defaultRPS           = 20
	baseBackoff          = 300 * time.Millisecond
	maxBackoff           = 5 * time.Second
)

// Options настраивают «вежливость» клиента к VictoriaMetrics.
type Options struct {
	// MaxConcurrent — максимум одновременных запросов в полёте.
	MaxConcurrent int
	// RPS — целевая скорость запросов (tokens/sec). 0 — без ограничения.
	RPS float64
	// Retries — число повторов для 429/503/5xx и сетевых ошибок.
	Retries int
}

type Client struct {
	baseURL    string
	httpClient *http.Client
	sem        chan struct{}
	limiter    *rateLimiter
	retries    int
}

func NewClient(baseURL string, timeout time.Duration, opts Options) *Client {
	if opts.MaxConcurrent <= 0 {
		opts.MaxConcurrent = defaultMaxConcurrent
	}
	if opts.RPS <= 0 {
		opts.RPS = defaultRPS
	}
	if opts.Retries < 0 {
		opts.Retries = 0
	}

	slog.Debug("Creating VictoriaMetrics client",
		slog.String("url", baseURL),
		slog.Duration("timeout", timeout),
		slog.Int("max_concurrent", opts.MaxConcurrent),
		slog.Float64("rps", opts.RPS),
		slog.Int("retries", opts.Retries),
	)

	return &Client{
		baseURL: baseURL,
		httpClient: &http.Client{
			Timeout: timeout,
		},
		sem:     make(chan struct{}, opts.MaxConcurrent),
		limiter: newRateLimiter(opts.RPS, float64(opts.MaxConcurrent)),
		retries: opts.Retries,
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

// Series — один временной ряд с разобранным значением.
type Series struct {
	Metric map[string]string
	Value  float64
}

func (c *Client) acquire(ctx context.Context) error {
	select {
	case c.sem <- struct{}{}:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (c *Client) release() {
	<-c.sem
}

// Ping проверяет доступность VictoriaMetrics.
func (c *Client) Ping(ctx context.Context) error {
	u := fmt.Sprintf("%s/api/v1/query?query=%s", c.baseURL, url.QueryEscape("up{}"))
	start := time.Now()

	if err := c.acquire(ctx); err != nil {
		return fmt.Errorf("vm ping failed: %w", err)
	}
	defer c.release()
	if err := c.limiter.Wait(ctx); err != nil {
		return fmt.Errorf("vm ping failed: %w", err)
	}

	var result QueryResult
	if err := c.get(ctx, u, &result); err != nil {
		return fmt.Errorf("vm ping failed: %w", err)
	}

	slog.Debug("VM ping ok", slog.Duration("duration", time.Since(start)))
	return nil
}

// Query выполняет instant-запрос и возвращает все временные ряды.
func (c *Client) Query(ctx context.Context, query string) ([]Series, error) {
	u := fmt.Sprintf("%s/api/v1/query?query=%s", c.baseURL, url.QueryEscape(query))
	start := time.Now()

	if err := c.acquire(ctx); err != nil {
		return nil, err
	}
	defer c.release()
	if err := c.limiter.Wait(ctx); err != nil {
		return nil, err
	}

	var result QueryResult
	if err := c.get(ctx, u, &result); err != nil {
		return nil, err
	}

	if result.Status != "success" {
		return nil, fmt.Errorf("vm query not successful: %s", result.Status)
	}

	series := make([]Series, 0, len(result.Data.Result))
	for _, r := range result.Data.Result {
		val, err := parseValue(r)
		if err != nil {
			slog.Debug("VM series value parse failed",
				slog.String("error", err.Error()),
				slog.String("query", query),
			)
			continue
		}
		series = append(series, Series{Metric: r.Metric, Value: val})
	}

	slog.Debug("VM query ok",
		slog.String("query", query),
		slog.Int("series", len(series)),
		slog.Duration("duration", time.Since(start)),
	)

	return series, nil
}

// QueryInstant — совместимая обёртка, возвращает первое значение.
func (c *Client) QueryInstant(ctx context.Context, query string) (float64, error) {
	series, err := c.Query(ctx, query)
	if err != nil {
		return 0, err
	}
	if len(series) == 0 {
		return 0, fmt.Errorf("no data returned for query")
	}
	return series[0].Value, nil
}

func parseValue(r Result) (float64, error) {
	if len(r.Value) < 2 {
		return 0, fmt.Errorf("empty value")
	}
	valStr, ok := r.Value[1].(string)
	if !ok {
		return 0, fmt.Errorf("unexpected value type %T", r.Value[1])
	}
	val, err := strconv.ParseFloat(valStr, 64)
	if err != nil {
		return 0, fmt.Errorf("parse value %q failed: %w", valStr, err)
	}
	return val, nil
}

// get выполняет GET с ретраями на retryable-статусы и сетевые ошибки.
func (c *Client) get(ctx context.Context, u string, out interface{}) error {
	backoff := baseBackoff
	var lastErr error

	for attempt := 0; attempt <= c.retries; attempt++ {
		if attempt > 0 {
			if err := sleepCtx(ctx, jittered(backoff)); err != nil {
				return err
			}
			backoff *= 2
			if backoff > maxBackoff {
				backoff = maxBackoff
			}
		}

		req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
		if err != nil {
			return fmt.Errorf("failed to create http request: %w", err)
		}

		start := time.Now()
		resp, err := c.httpClient.Do(req)
		duration := time.Since(start)

		if err != nil {
			lastErr = fmt.Errorf("vm request failed: %w", err)
			slog.Warn("VM request failed",
				slog.Int("attempt", attempt+1),
				slog.String("error", err.Error()),
				slog.Duration("duration", duration),
			)
			continue
		}

		if isRetryable(resp.StatusCode) {
			body, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			lastErr = fmt.Errorf("vm returned retryable status %d: %s", resp.StatusCode, string(body))
			slog.Warn("VM returned retryable status",
				slog.Int("attempt", attempt+1),
				slog.Int("status", resp.StatusCode),
			)
			continue
		}

		if resp.StatusCode != http.StatusOK {
			body, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			return fmt.Errorf("vm returned status %d: %s", resp.StatusCode, string(body))
		}

		if err := json.NewDecoder(resp.Body).Decode(out); err != nil {
			resp.Body.Close()
			lastErr = fmt.Errorf("vm parse failed: %w", err)
			continue
		}
		resp.Body.Close()
		return nil
	}

	return lastErr
}

func isRetryable(status int) bool {
	switch status {
	case http.StatusTooManyRequests, http.StatusInternalServerError, http.StatusBadGateway, http.StatusServiceUnavailable, http.StatusGatewayTimeout:
		return true
	default:
		return false
	}
}

func jittered(d time.Duration) time.Duration {
	f := 0.5 + rand.Float64()
	return time.Duration(float64(d) * f)
}

func sleepCtx(ctx context.Context, d time.Duration) error {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

// rateLimiter — простой token bucket с burst-ёмкостью.
type rateLimiter struct {
	mu     sync.Mutex
	rate   float64
	burst  float64
	tokens float64
	last   time.Time
}

func newRateLimiter(rate, burst float64) *rateLimiter {
	now := time.Now()
	return &rateLimiter{rate: rate, burst: burst, tokens: burst, last: now}
}

func (l *rateLimiter) Wait(ctx context.Context) error {
	for {
		l.mu.Lock()
		now := time.Now()
		l.tokens += now.Sub(l.last).Seconds() * l.rate
		if l.tokens > l.burst {
			l.tokens = l.burst
		}
		l.last = now
		if l.tokens >= 1 {
			l.tokens--
			l.mu.Unlock()
			return nil
		}
		need := time.Duration((1 - l.tokens) / l.rate * float64(time.Second))
		l.mu.Unlock()

		t := time.NewTimer(need)
		select {
		case <-ctx.Done():
			t.Stop()
			return ctx.Err()
		case <-t.C:
		}
	}
}

func finite(v float64) bool {
	return !math.IsNaN(v) && !math.IsInf(v, 0)
}
