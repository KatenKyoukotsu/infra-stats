package checker

import (
	"fmt"
	"strings"
	"sync"
	"time"

	"ssh-checker/internal/config"
	"ssh-checker/internal/sshclient"
)

// ItemStatus хранит результат проверки конкретного сервиса, контейнера или URL
type ItemStatus struct {
	Name    string `json:"name"`
	Type    string `json:"type"` // "systemd", "container", "http"
	IsAlive bool   `json:"is_alive"`
	Details string `json:"details,omitempty"`
}

// VMResult хранит полный статус проверки одной ВМ
type VMResult struct {
	VMID      string       `json:"vm_id"`
	VMName    string       `json:"vm_name"`
	Host      string       `json:"host"`
	IsOnline  bool         `json:"is_online"`
	Status    string       `json:"status"` // "healthy", "unhealthy", "unreachable"
	Error     string       `json:"error,omitempty"`
	Items     []ItemStatus `json:"items"`
	CheckedAt time.Time    `json:"checked_at"`
}

// CheckReport — итоговый отчет обо всех ВМ
type CheckReport struct {
	TotalVMs     int        `json:"total_vms"`
	HealthyVMs   int        `json:"healthy_vms"`
	UnhealthyVMs int        `json:"unhealthy_vms"`
	Timestamp    time.Time  `json:"timestamp"`
	Results      []VMResult `json:"results"`
}

// Engine выполняет проверки ВМ с помощью Worker Pool
type Engine struct {
	workers int
}

// NewEngine создает экземпляр движка проверок
func NewEngine(workers int) *Engine {
	if workers <= 0 {
		workers = 50
	}
	return &Engine{workers: workers}
}

// RunCheck параллельно запрашивает статус всех указанных ВМ
func (e *Engine) RunCheck(targets []config.TargetVM) CheckReport {
	jobs := make(chan config.TargetVM, len(targets))
	results := make(chan VMResult, len(targets))

	var wg sync.WaitGroup

	// Запуск Worker Pool
	for i := 0; i < e.workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for target := range jobs {
				results <- e.checkVM(target)
			}
		}()
	}

	// Отправляем задачи воркерам
	for _, target := range targets {
		jobs <- target
	}
	close(jobs)

	// Ожидаем завершения всех воркеров
	go func() {
		wg.Wait()
		close(results)
	}()

	report := CheckReport{
		TotalVMs:  len(targets),
		Timestamp: time.Now(),
		Results:   make([]VMResult, 0, len(targets)),
	}

	for res := range results {
		report.Results = append(report.Results, res)
		if res.Status == "healthy" {
			report.HealthyVMs++
		} else {
			report.UnhealthyVMs++
		}
	}

	return report
}

// checkVM выполняет проверки конкретной ВМ по SSH
func (e *Engine) checkVM(target config.TargetVM) VMResult {
	res := VMResult{
		VMID:      target.ID,
		VMName:    target.Name,
		Host:      target.Host,
		Status:    "healthy",
		CheckedAt: time.Now(),
		Items:     make([]ItemStatus, 0),
	}

	client := sshclient.NewClient(target.User, target.Host, target.Port, target.SSHKeyPath, 7*time.Second)

	// 1. Проверяем Systemd сервисы
	for _, svc := range target.Systemd {
		cmd := fmt.Sprintf("systemctl is-active %s", svc)
		stdout, _, err := client.RunCommand(cmd)

		output := strings.TrimSpace(stdout)
		isAlive := (err == nil && output == "active")

		item := ItemStatus{
			Name:    svc,
			Type:    "systemd",
			IsAlive: isAlive,
			Details: output,
		}

		if err != nil && !isAlive && output == "" {
			item.Details = err.Error()
		}

		if !isAlive {
			res.Status = "unhealthy"
		}

		res.Items = append(res.Items, item)
	}

	// 2. Проверяем Docker контейнеры
	for _, container := range target.Containers {
		cmd := fmt.Sprintf("docker inspect -f '{{.State.Running}}' %s", container)
		stdout, _, err := client.RunCommand(cmd)

		output := strings.TrimSpace(stdout)
		isAlive := (err == nil && output == "true")

		item := ItemStatus{
			Name:    container,
			Type:    "container",
			IsAlive: isAlive,
			Details: fmt.Sprintf("running: %s", output),
		}

		if !isAlive {
			res.Status = "unhealthy"
		}

		res.Items = append(res.Items, item)
	}

	// 3. Проверяем HTTP эндпоинты через curl
	for _, httpCheck := range target.HTTPChecks {
		cmd := fmt.Sprintf("curl -s -o /dev/null -w '%%{http_code}' --max-time 5 '%s'", httpCheck.URL)
		stdout, stderr, err := client.RunCommand(cmd)

		output := strings.TrimSpace(stdout)
		statusCode := 0
		fmt.Sscanf(output, "%d", &statusCode)

		isAlive := false
		if err == nil && statusCode > 0 {
			if len(httpCheck.ValidStatusCodes) > 0 {
				for _, validCode := range httpCheck.ValidStatusCodes {
					if statusCode == validCode {
						isAlive = true
						break
					}
				}
			} else {
				if statusCode >= 200 && statusCode < 300 {
					isAlive = true
				}
			}
		}

		details := fmt.Sprintf("HTTP %d", statusCode)
		if !isAlive {
			if err != nil {
				details = fmt.Sprintf("curl error: %v", err)
			} else if stderr != "" {
				details = fmt.Sprintf("HTTP %d (%s)", statusCode, strings.TrimSpace(stderr))
			} else {
				details = fmt.Sprintf("Unexpected HTTP status code: %d", statusCode)
			}
			res.Status = "unhealthy"
		}

		item := ItemStatus{
			Name:    httpCheck.URL,
			Type:    "http",
			IsAlive: isAlive,
			Details: details,
		}

		res.Items = append(res.Items, item)
	}

	// 4. Определение unreachable состояния
	failedCount := 0
	for _, item := range res.Items {
		if !item.IsAlive {
			failedCount++
		}
	}

	if len(res.Items) > 0 && failedCount == len(res.Items) {
		_, _, err := client.RunCommand("echo 1")
		if err != nil {
			res.IsOnline = false
			res.Status = "unreachable"
			res.Error = fmt.Sprintf("SSH connection failed: %v", err)
			return res
		}
	}

	res.IsOnline = true
	return res
}