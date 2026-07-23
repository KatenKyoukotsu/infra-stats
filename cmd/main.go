package main

import (
	"context"
	"embed"
	"io/fs"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"ssh-checker/internal/checker"
	"ssh-checker/internal/config"
	"ssh-checker/internal/health"
	"ssh-checker/internal/notifier"
	"ssh-checker/internal/scheduler"
	"ssh-checker/internal/storage"
	"ssh-checker/internal/web"
)

// Вшиваем web/index.html, который лежит рядом с main.go в папке cmd/web/
//go:embed web/*
var webFS embed.FS

func main() {
	log.Println("[INIT] Запуск SSH Service Checker...")

	// 1. Инициализация менеджера конфигурации
	configPath := "configs/config.yaml"
	if envPath := os.Getenv("CONFIG_PATH"); envPath != "" {
		configPath = envPath
	}

	cfgMgr, err := config.NewManager(configPath)
	if err != nil {
		log.Fatalf("[ERROR] Ошибка загрузки конфигурации: %v", err)
	}
	cfg := cfgMgr.Get()

	// 2. Инициализация ключевых компонентов
	store := storage.NewStorage(100)                      // Храним последние 100 отчетов
	engine := checker.NewEngine(50)                       // Worker pool из 50 параллельных воркеров
	botxClient := notifier.NewClient()                    // Клиент отправки сообщений
	sched := scheduler.NewScheduler(cfgMgr, engine, store, botxClient) // Планировщик

	// 3. Запуск планировщика задач
	if err := sched.Start(); err != nil {
		log.Fatalf("[ERROR] Ошибка запуска планировщика: %v", err)
	}

	// 4. Настройка веб-сервера и REST API
	api := web.NewAPI(cfgMgr, engine, store, botxClient)
	mux := http.NewServeMux()

	// REST API Маршруты
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

	// Статический веб-интерфейс из embedded FS
	subFS, err := fs.Sub(webFS, "web")
	if err != nil {
		log.Fatalf("[ERROR] Ошибка загрузки embedded UI: %v", err)
	}
	mux.Handle("/", http.FileServer(http.FS(subFS)))

	// 5. Запуск HTTP Сервера
	server := &http.Server{
		Addr:         ":" + cfg.ServerPort,
		Handler:      mux,
		ReadTimeout:  15 * time.Second,
		WriteTimeout: 30 * time.Second,
		IdleTimeout:  60 * time.Second,
	}

	go func() {
		log.Printf("[SERVER] Веб-интерфейс доступен на http://localhost:%s\n", cfg.ServerPort)
		if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("[ERROR] Ошибка работы HTTP-сервера: %v", err)
		}
	}()

	// 6. Graceful Shutdown
	quit := make(chan os.Signal, 1)
	signal.Notify(quit, os.Interrupt, syscall.SIGTERM)
	<-quit

	log.Println("[SHUTDOWN] Остановка сервиса...")

	sched.Stop()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	if err := server.Shutdown(ctx); err != nil {
		log.Printf("[ERROR] Принудительная остановка сервера: %v", err)
	}

	log.Println("[SHUTDOWN] Сервис успешно остановлен.")
}