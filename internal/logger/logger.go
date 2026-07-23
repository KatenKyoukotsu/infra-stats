package logger

import (
	"log/slog"
	"os"
	"strings"
)

// InitLogger настраивает глобальный slog.Logger в зависимости от ENV параметров:
// LOG_LEVEL: debug, info, warn, error (default: info)
// LOG_FORMAT: json, text (default: text)
func InitLogger() *slog.Logger {
	levelStr := strings.ToLower(os.Getenv("LOG_LEVEL"))
	formatStr := strings.ToLower(os.Getenv("LOG_FORMAT"))

	var level slog.Level
	switch levelStr {
	case "debug":
		level = slog.LevelDebug
	case "warn", "warning":
		level = slog.LevelWarn
	case "error":
		level = slog.LevelError
	default:
		level = slog.LevelInfo
	}

	opts := &slog.HandlerOptions{
		Level: level,
	}

	var handler slog.Handler
	if formatStr == "json" {
		handler = slog.NewJSONHandler(os.Stdout, opts)
	} else {
		handler = slog.NewTextHandler(os.Stdout, opts)
	}

	logger := slog.New(handler)
	slog.SetDefault(logger)
	return logger
}