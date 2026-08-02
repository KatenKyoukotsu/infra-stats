package vmclient

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

func TestRetryOn429(t *testing.T) {
	var calls int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if atomic.AddInt32(&calls, 1) <= 2 {
			w.WriteHeader(http.StatusTooManyRequests)
			return
		}
		w.Write([]byte(`{"status":"success","data":{"resultType":"vector","result":[{"metric":{"instance":"a"},"value":[123,"42"]}]}}`))
	}))
	defer srv.Close()

	c := NewClient(srv.URL, 2*time.Second, Options{RPS: 1000, Retries: 3})
	series, err := c.Query(context.Background(), "up{}")
	if err != nil {
		t.Fatalf("Query failed after retries: %v", err)
	}
	if len(series) != 1 || series[0].Value != 42 {
		t.Fatalf("unexpected series: %+v", series)
	}
	if got := atomic.LoadInt32(&calls); got != 3 {
		t.Fatalf("expected 3 HTTP calls (2 retries), got %d", got)
	}
}

func TestRetryExhausted(t *testing.T) {
	var calls int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&calls, 1)
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer srv.Close()

	c := NewClient(srv.URL, 2*time.Second, Options{RPS: 1000, Retries: 2})
	_, err := c.Query(context.Background(), "up{}")
	if err == nil {
		t.Fatal("expected error after exhausting retries")
	}
	if got := atomic.LoadInt32(&calls); got != 3 {
		t.Fatalf("expected 3 HTTP calls (initial + 2 retries), got %d", got)
	}
}

func TestRateLimiter(t *testing.T) {
	start := time.Now()
	rl := newRateLimiter(20, 1) // 20 tokens/sec, burst 1
	for i := 0; i < 10; i++ {
		if err := rl.Wait(context.Background()); err != nil {
			t.Fatal(err)
		}
	}
	// 10 tokens at 20/s => 9 gaps * 50ms >= 400ms
	if elapsed := time.Since(start); elapsed < 400*time.Millisecond {
		t.Fatalf("rate limiter allowed too many requests: %v", elapsed)
	}
}

func TestQueryEmptyResult(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{"status":"success","data":{"resultType":"vector","result":[]}}`))
	}))
	defer srv.Close()

	c := NewClient(srv.URL, 2*time.Second, Options{})
	series, err := c.Query(context.Background(), "up{}")
	if err != nil {
		t.Fatalf("expected nil error on empty result, got %v", err)
	}
	if len(series) != 0 {
		t.Fatalf("expected 0 series, got %d", len(series))
	}
}
