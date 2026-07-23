package scheduler

import (
	"log"

	"github.com/robfig/cron/v3"

	"ssh-checker/internal/checker"
	"ssh-checker/internal/config"
	"ssh-checker/internal/notifier"
	"ssh-checker/internal/storage"
)

// Scheduler управляет фоновым выполнением задач по расписанию
type Scheduler struct {
	cron     *cron.Cron
	cfgMgr   *config.Manager
	engine   *checker.Engine
	store    *storage.Storage
	notifier *notifier.Client
}

// NewScheduler создает экземпляр планировщика
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

// Start запускает фоновые задачи по крону
func (s *Scheduler) Start() error {
	cfg := s.cfgMgr.Get()

	// 1. Задача запуска проверки (по дефолту 00:00 -> "0 0 * * *")
	_, err := s.cron.AddFunc(cfg.Scheduler.CheckCron, func() {
		log.Println("[CRON] Запуск плановой проверки ВМ...")
		currentCfg := s.cfgMgr.Get()
		report := s.engine.RunCheck(currentCfg.Targets)
		s.store.AddReport(report)
		log.Printf("[CRON] Проверка завершена. Успешно: %d, Проблемных: %d\n", report.HealthyVMs, report.UnhealthyVMs)
	})
	if err != nil {
		return err
	}

	// 2. Задача отправки отчета в мессенджер (по дефолту 08:00 -> "0 8 * * *")
	_, err = s.cron.AddFunc(cfg.Scheduler.SendCron, func() {
		log.Println("[CRON] Отправка отчета в BotX...")
		lastReport, ok := s.store.GetLastReport()
		if !ok {
			log.Println("[CRON] Нет доступных отчетов для отправки!")
			return
		}

		currentCfg := s.cfgMgr.Get()
		if err := s.notifier.SendReport(currentCfg.Messenger, lastReport); err != nil {
			log.Printf("[CRON] Ошибка отправки отчета в BotX: %v\n", err)
			return
		}

		log.Println("[CRON] Отчет успешно отправлен в BotX!")
	})
	if err != nil {
		return err
	}

	s.cron.Start()
	log.Println("[SCHEDULER] Планировщик задач успешно запущен.")
	return nil
}

// Stop останавливает планировщик
func (s *Scheduler) Stop() {
	s.cron.Stop()
}