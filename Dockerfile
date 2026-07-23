# -----------------------------------------------------------------------------
# Stage 1: Build
# -----------------------------------------------------------------------------
FROM golang:alpine AS builder

WORKDIR /app

# Устанавливаем необходимые системные зависимости для сборки
RUN apk add --no-cache git ca-certificates

# Кэшируем зависимости Go
COPY go.mod go.sum ./
RUN go mod download

# Копируем исходный код
COPY . .

# Собираем бинарник с отключением CGO для максимальной переносимости
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-w -s" -o ssh-checker ./cmd/main.go

# -----------------------------------------------------------------------------
# Stage 2: Final minimal image
# -----------------------------------------------------------------------------
FROM alpine:3.19

WORKDIR /app

# Устанавливаем curl (для healthcheck) и сертификаты (для HTTPS запросов к BotX)
RUN apk add --no-cache ca-certificates curl tzdata

# Создаем системную группу и непривилегированного пользователя appuser
RUN addgroup -S appgroup && adduser -S appuser -G appgroup

# Копируем скомпилированный бинарник
COPY --from=builder /app/ssh-checker /app/ssh-checker

# Создаем директорию для конфигов и SSH-ключей и отдаем права пользователю appuser
RUN mkdir -p /app/configs/keys && chown -R appuser:appgroup /app

# Переключаемся на безопасного пользователя
USER appuser

# Открываем порт веб-интерфейса
EXPOSE 8080

# Настраиваем Healthcheck прямо в Dockerfile
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:8080/healthcheck || exit 1

# Точка входа
ENTRYPOINT ["/app/ssh-checker"]