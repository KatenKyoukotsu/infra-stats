package scheduler

import (
	"log/slog"

	"github.com/robfig/cron/v3"

	"ssh-checker/internal/checker"
	"ssh-checker/internal/config"
	"ssh-checker/internal/notifier"
	"ssh-checker/internal/storage"
)

type Scheduler struct {
	cron     *cron.Cron
	cfgMgr   *config.Manager
	engine   *checker.Engine
	store    *storage.Storage
	notifier *notifier.Client
}

func NewScheduler(
	cfgMgr *config.Manager,
	engine *checker.Engine,
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

func (s *Scheduler) Start() error {
	cfg := s.cfgMgr.Get()

	// 1. Cron запуск проверки
	_, err := s.cron.AddFunc(cfg.Scheduler.CheckCron, func() {
		slog.Info("[CRON] Scheduled infrastructure check started")
		currentCfg := s.cfgMgr.Get()
		report := s.engine.RunCheck(currentCfg.Targets)
		s.store.AddReport(report)
		slog.Info("[CRON] Scheduled check finished", slog.Int("healthy", report.HealthyVMs), slog.Int("unhealthy", report.UnhealthyVMs))
	})
	if err != nil {
		slog.Error("Failed to add check_cron job", slog.String("cron_spec", cfg.Scheduler.CheckCron), slog.String("error", err.Error()))
		return err
	}

	// 2. Cron отправка отчета
	_, err = s.cron.AddFunc(cfg.Scheduler.SendCron, func() {
		slog.Info("[CRON] Scheduled BotX report trigger executed")
		lastReport, ok := s.store.GetLastReport()
		if !ok {
			slog.Warn("[CRON] Skipping notification: no reports available in storage")
			return
		}

		currentCfg := s.cfgMgr.Get()
		if err := s.notifier.SendReport(currentCfg.Messenger, lastReport); err != nil {
			slog.Error("[CRON] Failed to send scheduled report to BotX", slog.String("error", err.Error()))
			return
		}

		slog.Info("[CRON] Scheduled report successfully delivered to BotX")
	})
	if err != nil {
		slog.Error("Failed to add send_cron job", slog.String("cron_spec", cfg.Scheduler.SendCron), slog.String("error", err.Error()))
		return err
	}

	s.cron.Start()
	slog.Info("Scheduler successfully started", slog.String("check_cron", cfg.Scheduler.CheckCron), slog.String("send_cron", cfg.Scheduler.SendCron))
	return nil
}

func (s *Scheduler) Stop() {
	slog.Info("Stopping scheduler...")
	s.cron.Stop()
}