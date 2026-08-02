package scheduler

import (
	"context"
	"log/slog"
	"math/rand"
	"sync"
	"time"

	"github.com/robfig/cron/v3"

	"infra-stats/internal/analyzer"
	"infra-stats/internal/config"
	"infra-stats/internal/notifier"
	"infra-stats/internal/storage"
)

const analysisTimeout = 5 * time.Minute

type JobStatus struct {
	LastRun     time.Time `json:"last_run"`
	LastSuccess bool      `json:"last_success"`
	LastError   string    `json:"last_error,omitempty"`
}

type Status struct {
	Analyze JobStatus `json:"analyze"`
	Send    JobStatus `json:"send"`
}

type Scheduler struct {
	mu       sync.RWMutex
	cron     *cron.Cron
	cfgMgr   *config.Manager
	engine   *analyzer.Engine
	store    *storage.Storage
	notifier *notifier.Client
	status   Status
}

func NewScheduler(
	cfgMgr *config.Manager,
	engine *analyzer.Engine,
	store *storage.Storage,
	notifierClient *notifier.Client,
) *Scheduler {
	return &Scheduler{
		cron:     cron.New(),
		cfgMgr:   cfgMgr,
		engine:   engine,
		store:    store,
		notifier: notifierClient,
	}
}

func (s *Scheduler) Status() Status {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.status
}

// AnalyzeNow запускает полный цикл анализа (инстансы + контейнеры),
// считает диффы и сохраняет отчёт в историю.
func (s *Scheduler) AnalyzeNow() analyzer.AnalysisReport {
	currentCfg := s.cfgMgr.Get()
	targets := buildTargetInputs(currentCfg.Targets)

	ctx, cancel := context.WithTimeout(context.Background(), analysisTimeout)
	defer cancel()

	report := s.engine.RunAnalysis(ctx, targets)

	if s.engine.ContainersEnabled() {
		report.Containers = s.engine.RunContainers(ctx)
	}

	if prev, ok := s.store.GetLastReport(); ok {
		report = analyzer.ComputeDiffs(report, prev)
	}
	s.store.AddReport(report)
	return report
}

func (s *Scheduler) Start() error {
	cfg := s.cfgMgr.Get()

	_, err := s.cron.AddFunc(cfg.Scheduler.AnalyzeCron, func() {
		slog.Info("[CRON] Scheduled metrics analysis started")

		applyJitter(s.cfgMgr.Get().Scheduler.Jitter)

		start := time.Now()
		var success bool
		var errStr string

		defer func() {
			s.mu.Lock()
			s.status.Analyze = JobStatus{
				LastRun:     start,
				LastSuccess: success,
				LastError:   errStr,
			}
			s.mu.Unlock()
		}()

		report := s.AnalyzeNow()
		success = true
		slog.Info("[CRON] Scheduled analysis finished", slog.Int("targets", len(report.Targets)))
	})
	if err != nil {
		slog.Error("Failed to add analyze_cron job", slog.String("cron_spec", cfg.Scheduler.AnalyzeCron), slog.String("error", err.Error()))
		return err
	}

	_, err = s.cron.AddFunc(cfg.Scheduler.SendCron, func() {
		slog.Info("[CRON] Scheduled BotX report trigger executed")

		start := time.Now()
		var success bool
		var errStr string

		defer func() {
			s.mu.Lock()
			s.status.Send = JobStatus{
				LastRun:     start,
				LastSuccess: success,
				LastError:   errStr,
			}
			s.mu.Unlock()
		}()

		lastReport, ok := s.store.GetLastReport()
		if !ok {
			errStr = "no reports available"
			slog.Warn("[CRON] Skipping notification: no reports available in storage")
			return
		}

		currentCfg := s.cfgMgr.Get()
		if err := s.notifier.SendReport(currentCfg, lastReport); err != nil {
			errStr = err.Error()
			slog.Error("[CRON] Failed to send scheduled report to BotX", slog.String("error", err.Error()))
			return
		}

		success = true
		slog.Info("[CRON] Scheduled report successfully delivered to BotX")
	})
	if err != nil {
		slog.Error("Failed to add send_cron job", slog.String("cron_spec", cfg.Scheduler.SendCron), slog.String("error", err.Error()))
		return err
	}

	s.cron.Start()
	slog.Info("Scheduler started",
		slog.String("analyze_cron", cfg.Scheduler.AnalyzeCron),
		slog.String("send_cron", cfg.Scheduler.SendCron),
		slog.Duration("jitter", cfg.Scheduler.Jitter),
	)
	return nil
}

func (s *Scheduler) Stop() {
	slog.Info("Stopping scheduler...")
	s.cron.Stop()
}

func applyJitter(max time.Duration) {
	if max <= 0 {
		return
	}
	delay := time.Duration(rand.Int63n(int64(max)))
	if delay > 0 {
		slog.Info("Applying scheduler jitter", slog.Duration("delay", delay))
		time.Sleep(delay)
	}
}

func buildTargetInputs(cfgTargets []config.TargetConfig) []analyzer.TargetInput {
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
