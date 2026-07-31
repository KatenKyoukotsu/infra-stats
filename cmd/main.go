package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io/fs"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"infra-stats/internal/analyzer"
	"infra-stats/internal/config"
	"infra-stats/internal/logger"
	"infra-stats/internal/notifier"
	"infra-stats/internal/scheduler"
	"infra-stats/internal/storage"
	"infra-stats/internal/vmclient"
	"infra-stats/internal/web"
)

func main() {
	log := logger.InitLogger()
	slog.SetDefault(log)

	slog.Info("Starting Infra Stats Analyzer...")

	configPath := "configs/config.yaml"
	if envPath := os.Getenv("CONFIG_PATH"); envPath != "" {
		configPath = envPath
	}

	cfgMgr, err := config.NewManager(configPath)
	if err != nil {
		slog.Error("Failed to load configuration", slog.String("path", configPath), slog.String("error", err.Error()))
		os.Exit(1)
	}
	cfg := cfgMgr.Get()

	vmClient := vmclient.NewClient(cfg.VictoriaMetrics.URL, cfg.VictoriaMetrics.Timeout)

	engine := analyzer.NewEngine(vmClient, cfg.Analysis.CPU, cfg.Analysis.Memory, cfg.Analysis.Disk, cfg.Analysis.OOM, cfg.Analysis.Periods)

	store := storage.NewStorage(100)
	botxClient := notifier.NewClient()
	sched := scheduler.NewScheduler(cfgMgr, engine, store, botxClient)

	if err := sched.Start(); err != nil {
		slog.Error("Failed to start scheduler", slog.String("error", err.Error()))
		os.Exit(1)
	}

	writeJSON := func(w http.ResponseWriter, v interface{}) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(v)
	}

	mux := http.NewServeMux()

	mux.HandleFunc("/healthcheck", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write([]byte("ItsOK"))
	})

	mux.HandleFunc("/api/status", func(w http.ResponseWriter, r *http.Request) {
		report, ok := store.GetLastReport()
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			writeJSON(w, map[string]string{"error": "no reports yet"})
			return
		}
		writeJSON(w, report)
	})

	mux.HandleFunc("/api/reports", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, store.GetAllReports())
	})

	mux.HandleFunc("/api/analyze", func(w http.ResponseWriter, r *http.Request) {
		slog.Info("Manual analysis triggered via API")

		currentCfg := cfgMgr.Get()
		targets := buildTargets(currentCfg.Targets)

		report := engine.RunAnalysis(targets)
		if prev, ok := store.GetLastReport(); ok {
			report = analyzer.ComputeDiffs(report, prev)
		}
		store.AddReport(report)
		writeJSON(w, report)
	})

	mux.HandleFunc("/api/clear", func(w http.ResponseWriter, r *http.Request) {
		store.Clear()
		writeJSON(w, map[string]string{"status": "cleared"})
	})

	mux.HandleFunc("/api/config", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, cfgMgr.Get())
	})

	mux.HandleFunc("/api/scheduler", func(w http.ResponseWriter, r *http.Request) {
		cfg := cfgMgr.Get()
		resp := map[string]interface{}{
			"analyze_cron": cfg.Scheduler.AnalyzeCron,
			"send_cron":    cfg.Scheduler.SendCron,
			"status":       sched.Status(),
		}
		writeJSON(w, resp)
	})

	mux.HandleFunc("/api/notifications", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, botxClient.Notifications())
	})

	mux.HandleFunc("/api/test/vm", func(w http.ResponseWriter, r *http.Request) {
		if err := vmClient.Ping(); err != nil {
			slog.Warn("VM connectivity test failed", slog.String("error", err.Error()))
			writeJSON(w, map[string]interface{}{"success": false, "error": err.Error()})
			return
		}
		writeJSON(w, map[string]interface{}{"success": true})
	})

	mux.HandleFunc("/api/test/clouds", func(w http.ResponseWriter, r *http.Request) {
		cfg := cfgMgr.Get()
		if err := botxClient.Validate(cfg.Notifier.BotX); err != nil {
			writeJSON(w, map[string]interface{}{"success": false, "error": err.Error()})
			return
		}
		writeJSON(w, map[string]interface{}{
			"success": true,
			"api_url": cfg.Notifier.BotX.APIURL,
			"chat_id": cfg.Notifier.BotX.ChatID,
		})
	})

	mux.HandleFunc("/api/test/send", func(w http.ResponseWriter, r *http.Request) {
		cfg := cfgMgr.Get()

		if err := botxClient.Validate(cfg.Notifier.BotX); err != nil {
			writeJSON(w, map[string]interface{}{"success": false, "error": err.Error()})
			return
		}

		report, ok := store.GetLastReport()
		if !ok {
			writeJSON(w, map[string]interface{}{"success": false, "error": "no reports available"})
			return
		}

		if err := botxClient.SendReport(cfg.Notifier.BotX, report); err != nil {
			slog.Error("Test send failed", slog.String("error", err.Error()))
			writeJSON(w, map[string]interface{}{"success": false, "error": err.Error()})
			return
		}

		writeJSON(w, map[string]interface{}{"success": true})
	})

	mux.HandleFunc("/api/preview", func(w http.ResponseWriter, r *http.Request) {
		report, ok := store.GetLastReport()
		if !ok {
			writeJSON(w, map[string]interface{}{"error": "no reports available"})
			return
		}
		text := botxClient.FormatReport(report)
		writeJSON(w, map[string]interface{}{
			"text":      text,
			"timestamp": report.Timestamp,
		})
	})

	staticFS, err := fs.Sub(web.Static, "static")
	if err != nil {
		slog.Error("Failed to create static sub filesystem", slog.String("error", err.Error()))
		os.Exit(1)
	}
	mux.Handle("/", http.FileServer(http.FS(staticFS)))

	port := 8080
	server := &http.Server{
		Addr:         fmt.Sprintf(":%d", port),
		Handler:      mux,
		ReadTimeout:  15 * time.Second,
		WriteTimeout: 30 * time.Second,
		IdleTimeout:  60 * time.Second,
	}

	go func() {
		slog.Info("HTTP server started", slog.String("addr", server.Addr))
		if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			slog.Error("HTTP server error", slog.String("error", err.Error()))
			os.Exit(1)
		}
	}()

	quit := make(chan os.Signal, 1)
	signal.Notify(quit, os.Interrupt, syscall.SIGTERM)
	<-quit

	slog.Info("Shutdown signal received, starting graceful termination...")
	sched.Stop()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	if err := server.Shutdown(ctx); err != nil {
		slog.Error("Forced HTTP server shutdown", slog.String("error", err.Error()))
	}

	slog.Info("Infra Stats Analyzer gracefully stopped")
}

func buildTargets(cfgTargets []config.TargetConfig) []analyzer.TargetInput {
	targets := make([]analyzer.TargetInput, 0, len(cfgTargets))
	for _, t := range cfgTargets {
		mps := t.Mountpoints
		if len(mps) == 0 {
			mps = []string{"/"}
		}
		targets = append(targets, analyzer.TargetInput{
			Name:        t.Name,
			Instance:    t.Instance,
			Mountpoints: mps,
		})
	}
	return targets
}
