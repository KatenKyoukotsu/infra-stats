package checker

import (
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"

	"ssh-checker/internal/config"
	"ssh-checker/internal/sshclient"
)

type ItemStatus struct {
	Name    string `json:"name"`
	Type    string `json:"type"` // "systemd", "container", "http", "system"
	IsAlive bool   `json:"is_alive"`
	Details string `json:"details,omitempty"`
}

type VMResult struct {
	VMID      string       `json:"vm_id"`
	VMName    string       `json:"vm_name"`
	Host      string       `json:"host"`
	IsOnline  bool         `json:"is_online"`
	Uptime    string       `json:"uptime,omitempty"`
	HasOOM    bool         `json:"has_oom"`
	OOMDetail string       `json:"oom_detail,omitempty"`
	Status    string       `json:"status"` // "healthy", "unhealthy", "unreachable"
	Error     string       `json:"error,omitempty"`
	Items     []ItemStatus `json:"items"`
	CheckedAt time.Time    `json:"checked_at"`
}

type CheckReport struct {
	TotalVMs     int        `json:"total_vms"`
	HealthyVMs   int        `json:"healthy_vms"`
	UnhealthyVMs int        `json:"unhealthy_vms"`
	Timestamp    time.Time  `json:"timestamp"`
	Results      []VMResult `json:"results"`
}

type Engine struct {
	workers int
}

func NewEngine(workers int) *Engine {
	if workers <= 0 {
		workers = 50
	}
	return &Engine{workers: workers}
}

func (e *Engine) RunCheck(targets []config.TargetVM) CheckReport {
	startTotal := time.Now()
	slog.Info("Starting infrastructure check", slog.Int("targets_count", len(targets)))

	jobs := make(chan config.TargetVM, len(targets))
	results := make(chan VMResult, len(targets))

	var wg sync.WaitGroup

	for i := 0; i < e.workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for target := range jobs {
				results <- e.checkVM(target)
			}
		}()
	}

	for _, target := range targets {
		jobs <- target
	}
	close(jobs)

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

	slog.Info("Infrastructure check completed",
		slog.Int("total", report.TotalVMs),
		slog.Int("healthy", report.HealthyVMs),
		slog.Int("unhealthy", report.UnhealthyVMs),
		slog.Duration("duration", time.Since(startTotal)),
	)

	return report
}

func (e *Engine) checkVM(target config.TargetVM) VMResult {
	vmStart := time.Now()
	logger := slog.With(
		slog.String("target_id", target.ID),
		slog.String("target_name", target.Name),
		slog.String("host", target.Host),
	)

	logger.Debug("Checking target VM...")

	res := VMResult{
		VMID:      target.ID,
		VMName:    target.Name,
		Host:      target.Host,
		Status:    "healthy",
		CheckedAt: time.Now(),
		Items:     make([]ItemStatus, 0),
	}

	client := sshclient.NewClient(target.User, target.Host, target.Port, target.SSHKeyPath, 7*time.Second)

	// --- 1. Uptime ---
	uptimeOut, _, errUptime := client.RunCommand("uptime -p")
	if errUptime == nil {
		res.Uptime = strings.TrimSpace(uptimeOut)
	} else {
		res.Uptime = "unknown"
		logger.Warn("Failed to fetch uptime via SSH", slog.String("error", errUptime.Error()))
	}

	// --- 2. OOM Killer ---
	oomCmd := "dmesg -T 2>/dev/null | grep -iE 'oom-killer|out of memory' | tail -n 1"
	oomOut, _, errOOM := client.RunCommand(oomCmd)

	cleanOOM := strings.TrimSpace(oomOut)

	if errOOM != nil || cleanOOM == "" {
		journalCmd := "journalctl -k -b -g 'Out of memory|oom-killer' --no-pager -n 1 2>/dev/null"
		journalOut, _, _ := client.RunCommand(journalCmd)
		cleanOOM = strings.TrimSpace(journalOut)
	}

	if strings.Contains(cleanOOM, "-- No entries --") ||
		strings.Contains(cleanOOM, "-- No matches --") ||
		strings.HasPrefix(cleanOOM, "Notice:") {
		cleanOOM = ""
	}

	if cleanOOM != "" {
		res.HasOOM = true
		res.OOMDetail = cleanOOM
		res.Status = "unhealthy"

		logger.Warn("OOM Event detected on target VM", slog.String("detail", cleanOOM))

		res.Items = append(res.Items, ItemStatus{
			Name:    "OOM-Killer Check",
			Type:    "system",
			IsAlive: false,
			Details: fmt.Sprintf("OOM detected: %s", cleanOOM),
		})
	} else {
		res.Items = append(res.Items, ItemStatus{
			Name:    "OOM-Killer Check",
			Type:    "system",
			IsAlive: true,
			Details: "No OOM events found",
		})
	}

	// --- 3. Systemd ---
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
			logger.Warn("Systemd service is down", slog.String("service", svc), slog.String("details", item.Details))
		}

		res.Items = append(res.Items, item)
	}

	// --- 4. Docker Containers ---
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
			logger.Warn("Docker container is not running", slog.String("container", container), slog.String("output", output))
		}

		res.Items = append(res.Items, item)
	}

	// --- 5. HTTP Checks ---
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
			logger.Warn("HTTP check failed", slog.String("url", httpCheck.URL), slog.String("details", details))
		}

		item := ItemStatus{
			Name:    httpCheck.URL,
			Type:    "http",
			IsAlive: isAlive,
			Details: details,
		}

		res.Items = append(res.Items, item)
	}

	// --- 6. Unreachable State ---
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
			logger.Error("Target host unreachable via SSH", slog.String("error", res.Error))
			return res
		}
	}

	res.IsOnline = true
	logger.Debug("Target VM check completed", slog.String("status", res.Status), slog.Duration("duration", time.Since(vmStart)))

	return res
}