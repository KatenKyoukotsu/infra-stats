package scheduler

import (
	"log/slog"
	"sync"
	"time"

	"github.com/robfig/cron/v3"

	"infra-stats/internal/analyzer"
	"infra-stats/internal/config"
	"infra-stats/internal/notifier"
	"infra-stats/internal/storage"
)

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

func (s *Scheduler) Start() error {
	cfg := s.cfgMgr.Get()

	_, err := s.cron.AddFunc(cfg.Scheduler.AnalyzeCron, func() {
		slog.Info("[CRON] Scheduled metrics analysis started")

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

		currentCfg := s.cfgMgr.Get()
		targets := buildTargetInputs(currentCfg.Targets)

		report := s.engine.RunAnalysis(targets)
		if prev, ok := s.store.GetLastReport(); ok {
			report = analyzer.ComputeDiffs(report, prev)
		}
		s.store.AddReport(report)
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
		if err := s.notifier.SendReport(currentCfg.Notifier.BotX, lastReport); err != nil {
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
	)
	return nil
}

func (s *Scheduler) Stop() {
	slog.Info("Stopping scheduler...")
	s.cron.Stop()
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
