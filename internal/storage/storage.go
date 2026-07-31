package storage

import (
	"sync"

	"infra-stats/internal/analyzer"
)

type Storage struct {
	mu         sync.RWMutex
	reports    []analyzer.AnalysisReport
	maxReports int
}

func NewStorage(maxReports int) *Storage {
	if maxReports <= 0 {
		maxReports = 50
	}
	return &Storage{
		reports:    make([]analyzer.AnalysisReport, 0, maxReports),
		maxReports: maxReports,
	}
}

func (s *Storage) AddReport(report analyzer.AnalysisReport) {
	s.mu.Lock()
	defer s.mu.Unlock()

	if len(s.reports) >= s.maxReports {
		s.reports = s.reports[1:]
	}

	s.reports = append(s.reports, report)
}

func (s *Storage) GetLastReport() (analyzer.AnalysisReport, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()

	if len(s.reports) == 0 {
		return analyzer.AnalysisReport{}, false
	}

	return s.reports[len(s.reports)-1], true
}

func (s *Storage) GetAllReports() []analyzer.AnalysisReport {
	s.mu.RLock()
	defer s.mu.RUnlock()

	copied := make([]analyzer.AnalysisReport, len(s.reports))
	copy(copied, s.reports)
	return copied
}

func (s *Storage) Clear() {
	s.mu.Lock()
	defer s.mu.Unlock()

	s.reports = make([]analyzer.AnalysisReport, 0, s.maxReports)
}
