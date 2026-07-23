package storage

import (
	"sync"

	"ssh-checker/internal/checker"
)

// Storage хранит историю отчетов о проверках в памяти
type Storage struct {
	mu         sync.RWMutex
	reports    []checker.CheckReport
	maxReports int
}

// NewStorage создает новое хранилище. maxReports задает лимит хранимых отчетов (например, 100).
func NewStorage(maxReports int) *Storage {
	if maxReports <= 0 {
		maxReports = 50
	}
	return &Storage{
		reports:    make([]checker.CheckReport, 0, maxReports),
		maxReports: maxReports,
	}
}

// AddReport добавляет новый отчет в хранилище
func (s *Storage) AddReport(report checker.CheckReport) {
	s.mu.Lock()
	defer s.mu.Unlock()

	// Если достигли лимита, удаляем самый старый отчет
	if len(s.reports) >= s.maxReports {
		s.reports = s.reports[1:]
	}

	s.reports = append(s.reports, report)
}

// GetLastReport возвращает самый свежий отчет
func (s *Storage) GetLastReport() (checker.CheckReport, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()

	if len(s.reports) == 0 {
		return checker.CheckReport{}, false
	}

	return s.reports[len(s.reports)-1], true
}

// GetAllReports возвращает всю историю отчетов
func (s *Storage) GetAllReports() []checker.CheckReport {
	s.mu.RLock()
	defer s.mu.RUnlock()

	// Возвращаем копию среза для безопасности
	copied := make([]checker.CheckReport, len(s.reports))
	copy(copied, s.reports)
	return copied
}

// Clear полностью очищает историю отчетов
func (s *Storage) Clear() {
	s.mu.Lock()
	defer s.mu.Unlock()

	s.reports = make([]checker.CheckReport, 0, s.maxReports)
}