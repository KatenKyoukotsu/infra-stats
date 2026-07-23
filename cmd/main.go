package main

import (
	"context"
	"embed"
	"io/fs"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"ssh-checker/internal/checker"
	"ssh-checker/internal/config"
	"ssh-checker/internal/health"
	"ssh-checker/internal/logger"
	"ssh-checker/internal/notifier"
	"ssh-checker/internal/scheduler"
	"ssh-checker/internal/storage"
	"ssh-checker/internal/web"
)

//go:embed web/*
var webFS embed.FS

func main() {
	// 1. Инициализация структурированного логгера (slog)
	log := logger.InitLogger()
	slog.SetDefault(log)

	slog.Info("Starting SSH Service Checker Application...")

	// 2. Инициализация конфигурации
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

	// 3. Инициализация доменных сервисов
	store := storage.NewStorage(100)
	engine := checker.NewEngine(50)
	botxClient := notifier.NewClient()
	sched := scheduler.NewScheduler(cfgMgr, engine, store, botxClient)

	// 4. Запуск Cron Планировщика
	if err := sched.Start(); err != nil {
		slog.Error("Failed to start scheduler", slog.String("error", err.Error()))
		os.Exit(1)
	}

	// 5. Настройка HTTP роутинга
	api := web.NewAPI(cfgMgr, engine, store, botxClient)
	mux := http.NewServeMux()

	mux.HandleFunc("/healthcheck", health.Handler)
	mux.HandleFunc("/api/status", api.GetStatusHandler)
	mux.HandleFunc("/api/check", api.RunCheckHandler)
	mux.HandleFunc("/api/send", api.SendReportHandler)
	mux.HandleFunc("/api/clear", api.ClearStorageHandler)
	mux.HandleFunc("/api/config", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet {
			api.GetConfigHandler(w, r)
		} else if r.Method == http.MethodPost {
			api.UpdateConfigHandler(w, r)
		} else {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		}
	})

	// Статика из Embedded FS
	subFS, err := fs.Sub(webFS, "web")
	if err != nil {
		slog.Error("Failed to load embedded UI", slog.String("error", err.Error()))
		os.Exit(1)
	}
	mux.Handle("/", http.FileServer(http.FS(subFS)))

	// Оборачиваем весь роутер в Logger Middleware
	loggedHandler := web.LoggerMiddleware(mux)

	// 6. Конфигурация HTTP Сервера
	server := &http.Server{
		Addr:         ":" + cfg.ServerPort,
		Handler:      loggedHandler,
		ReadTimeout:  15 * time.Second,
		WriteTimeout: 30 * time.Second,
		IdleTimeout:  60 * time.Second,
	}

	go func() {
		slog.Info("Web UI and API server active", slog.String("port", cfg.ServerPort), slog.String("url", "http://localhost:"+cfg.ServerPort))
		if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			slog.Error("HTTP Server crushed", slog.String("error", err.Error()))
			os.Exit(1)
		}
	}()

	// 7. Graceful Shutdown
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

	slog.Info("SSH Service Checker gracefully stopped")
}