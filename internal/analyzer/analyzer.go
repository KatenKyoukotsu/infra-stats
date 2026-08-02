package analyzer

import (
	"context"
	"fmt"
	"log/slog"
	"math"
	"time"

	"infra-stats/internal/config"
	"infra-stats/internal/vmclient"
)

type MetricValue struct {
	Period string   `json:"period"`
	Value  float64  `json:"value"`
	Diff   *float64 `json:"diff,omitempty"`
}

type DiskStat struct {
	Mountpoint string        `json:"mountpoint"`
	Metrics    []MetricValue `json:"metrics"`
}

type OOMEvent struct {
	Period string `json:"period"`
	Count  int    `json:"count"`
	Diff   *int   `json:"diff,omitempty"`
}

type TargetStats struct {
	Name   string        `json:"name"`
	CPU    []MetricValue `json:"cpu,omitempty"`
	Memory []MetricValue `json:"memory,omitempty"`
	Disks  []DiskStat    `json:"disks,omitempty"`
	OOM    []OOMEvent    `json:"oom,omitempty"`
}

type AnalysisReport struct {
	Timestamp  time.Time       `json:"timestamp"`
	Targets    []TargetStats   `json:"targets"`
	Containers []ContainerStat `json:"containers,omitempty"`
}

// ContainerStat — аналитика по контейнеру за периоды, зеркалит TargetStats.
type ContainerStat struct {
	Name       string        `json:"name"`
	Instance   string        `json:"instance"`
	Job        string        `json:"job"`
	Envir      string        `json:"envir,omitempty"`
	Department string        `json:"department,omitempty"`
	CPU        []MetricValue `json:"cpu,omitempty"`    // % от лимита контейнера
	Memory     []MetricValue `json:"memory,omitempty"` // % от лимита контейнера
	CPUVM      []MetricValue `json:"cpu_vm,omitempty"` // % от CPU всей ВМ
	MemVM      []MetricValue `json:"mem_vm,omitempty"` // % от памяти всей ВМ
}

type Engine struct {
	client        *vmclient.Client
	cpu           bool
	memory        bool
	disk          bool
	oom           bool
	periods       []string
	containersCfg config.ContainersConfig
	containersOn  bool
}

func NewEngine(client *vmclient.Client, analysis config.AnalysisConfig, containers config.ContainersConfig) *Engine {
	slog.Debug("Creating analyzer engine",
		slog.Bool("cpu", analysis.CPU),
		slog.Bool("memory", analysis.Memory),
		slog.Bool("disk", analysis.Disk),
		slog.Bool("oom", analysis.OOM),
		slog.Any("periods", analysis.Periods),
		slog.Bool("containers", containers.Enabled),
	)
	return &Engine{
		client:        client,
		cpu:           analysis.CPU,
		memory:        analysis.Memory,
		disk:          analysis.Disk,
		oom:           analysis.OOM,
		periods:       analysis.Periods,
		containersCfg: containers,
		containersOn:  containers.Enabled,
	}
}

func (e *Engine) ContainersEnabled() bool { return e.containersOn }

// RunAnalysis выполняет один прогон. Каждый период — один группированный
// запрос по всем инстансам сразу (вместо запроса на инстанс), так что число
// запросов к VM не растёт с числом целей.
func (e *Engine) RunAnalysis(ctx context.Context, targets []TargetInput) AnalysisReport {
	slog.Info("Starting metrics analysis", slog.Int("targets", len(targets)))

	report := AnalysisReport{
		Timestamp: time.Now(),
		Targets:   make([]TargetStats, 0, len(targets)),
	}
	for _, t := range targets {
		report.Targets = append(report.Targets, TargetStats{Name: t.Name})
	}

	idxByInstance := make(map[string]int, len(targets))
	for i, t := range targets {
		idxByInstance[t.Instance] = i
	}

	for _, period := range e.periods {
		if e.cpu {
			e.fillCPU(ctx, report.Targets, idxByInstance, period)
		}
		if e.memory {
			e.fillMemory(ctx, report.Targets, idxByInstance, period)
		}
		if e.disk {
			e.fillDisk(ctx, report.Targets, idxByInstance, targets, period)
		}
		if e.oom {
			e.fillOOM(ctx, report.Targets, idxByInstance, period)
		}
	}

	slog.Info("Analysis complete",
		slog.Int("targets", len(report.Targets)),
		slog.Time("timestamp", report.Timestamp),
	)

	return report
}

func (e *Engine) fillCPU(ctx context.Context, targets []TargetStats, idxByInstance map[string]int, period string) {
	query := fmt.Sprintf(
		`100 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[%s])) * 100`,
		period,
	)

	series, err := e.client.Query(ctx, query)
	if err != nil {
		slog.Warn("CPU query failed", slog.String("period", period), slog.String("error", err.Error()))
		return
	}

	for _, s := range series {
		idx, ok := idxByInstance[s.Metric["instance"]]
		if !ok || !finite(s.Value) {
			continue
		}
		targets[idx].CPU = append(targets[idx].CPU, MetricValue{Period: period, Value: round(s.Value, 1)})
	}
}

func (e *Engine) fillMemory(ctx context.Context, targets []TargetStats, idxByInstance map[string]int, period string) {
	query := fmt.Sprintf(
		`avg_over_time((1 - node_memory_MemAvailable_bytes{instance!=""} / node_memory_MemTotal_bytes{instance!=""})[%s:2m]) * 100`,
		period,
	)

	series, err := e.client.Query(ctx, query)
	if err != nil {
		slog.Warn("Memory query failed", slog.String("period", period), slog.String("error", err.Error()))
		return
	}

	for _, s := range series {
		idx, ok := idxByInstance[s.Metric["instance"]]
		if !ok || !finite(s.Value) {
			continue
		}
		targets[idx].Memory = append(targets[idx].Memory, MetricValue{Period: period, Value: round(s.Value, 1)})
	}
}

func (e *Engine) fillDisk(ctx context.Context, targets []TargetStats, idxByInstance map[string]int, targetsCfg []TargetInput, period string) {
	query := fmt.Sprintf(
		`avg by (instance, mountpoint) (avg_over_time((1 - node_filesystem_avail_bytes{instance!=""} / node_filesystem_size_bytes{instance!=""})[%s:2m])) * 100`,
		period,
	)

	series, err := e.client.Query(ctx, query)
	if err != nil {
		slog.Warn("Disk query failed", slog.String("period", period), slog.String("error", err.Error()))
		return
	}

	allowed := make(map[string]map[string]bool, len(targetsCfg))
	for _, t := range targetsCfg {
		allowed[t.Instance] = make(map[string]bool, len(t.Mountpoints))
		for _, mp := range t.Mountpoints {
			allowed[t.Instance][mp] = true
		}
	}

	for _, s := range series {
		idx, ok := idxByInstance[s.Metric["instance"]]
		if !ok || !finite(s.Value) {
			continue
		}
		mp := s.Metric["mountpoint"]
		if !allowed[s.Metric["instance"]][mp] {
			continue
		}
		targets[idx].addDisk(mp, MetricValue{Period: period, Value: round(s.Value, 1)})
	}
}

func (e *Engine) fillOOM(ctx context.Context, targets []TargetStats, idxByInstance map[string]int, period string) {
	query := fmt.Sprintf(
		`sum by (instance) (increase(node_vmstat_oom_kill[%s]))`,
		period,
	)

	series, err := e.client.Query(ctx, query)
	if err != nil {
		slog.Debug("OOM query returned no data", slog.String("period", period), slog.String("error", err.Error()))
		return
	}

	for _, s := range series {
		idx, ok := idxByInstance[s.Metric["instance"]]
		if !ok || !finite(s.Value) {
			continue
		}
		count := int(s.Value)
		if count <= 0 {
			continue
		}
		targets[idx].OOM = append(targets[idx].OOM, OOMEvent{Period: period, Count: count})
	}
}

func (ts *TargetStats) addDisk(mountpoint string, m MetricValue) {
	for i := range ts.Disks {
		if ts.Disks[i].Mountpoint == mountpoint {
			ts.Disks[i].Metrics = append(ts.Disks[i].Metrics, m)
			return
		}
	}
	ts.Disks = append(ts.Disks, DiskStat{Mountpoint: mountpoint, Metrics: []MetricValue{m}})
}

func finite(v float64) bool {
	return !math.IsNaN(v) && !math.IsInf(v, 0)
}

func round(v float64, decimals int) float64 {
	pow := 1.0
	for i := 0; i < decimals; i++ {
		pow *= 10
	}
	return float64(int(v*pow+0.5)) / pow
}

func ComputeDiffs(current, previous AnalysisReport) AnalysisReport {
	if len(previous.Targets) == 0 {
		return current
	}

	prevByTarget := make(map[string]TargetStats, len(previous.Targets))
	for _, t := range previous.Targets {
		prevByTarget[t.Name] = t
	}

	for ti, t := range current.Targets {
		prev, ok := prevByTarget[t.Name]
		if !ok {
			continue
		}

		for mi := range t.CPU {
			current.Targets[ti].CPU[mi].Diff = metricDiff(t.CPU[mi], prev.CPU)
		}
		for mi := range t.Memory {
			current.Targets[ti].Memory[mi].Diff = metricDiff(t.Memory[mi], prev.Memory)
		}
		for di := range t.Disks {
			for mi := range t.Disks[di].Metrics {
				current.Targets[ti].Disks[di].Metrics[mi].Diff = diskMetricDiff(t.Disks[di].Metrics[mi], prev.Disks, t.Disks[di].Mountpoint)
			}
		}
		for oi := range t.OOM {
			current.Targets[ti].OOM[oi].Diff = oomDiff(t.OOM[oi], prev.OOM)
		}
	}

	prevByContainer := make(map[string]ContainerStat, len(previous.Containers))
	for _, c := range previous.Containers {
		prevByContainer[containerKey(c)] = c
	}
	for ci := range current.Containers {
		prev, ok := prevByContainer[containerKey(current.Containers[ci])]
		if !ok {
			continue
		}
		cur := &current.Containers[ci]
		for mi := range cur.CPU {
			cur.CPU[mi].Diff = metricDiff(cur.CPU[mi], prev.CPU)
		}
		for mi := range cur.Memory {
			cur.Memory[mi].Diff = metricDiff(cur.Memory[mi], prev.Memory)
		}
		for mi := range cur.CPUVM {
			cur.CPUVM[mi].Diff = metricDiff(cur.CPUVM[mi], prev.CPUVM)
		}
		for mi := range cur.MemVM {
			cur.MemVM[mi].Diff = metricDiff(cur.MemVM[mi], prev.MemVM)
		}
	}

	return current
}

func containerKey(c ContainerStat) string {
	return c.Instance + "\x00" + c.Name
}

func metricDiff(curr MetricValue, prevMetrics []MetricValue) *float64 {
	for _, p := range prevMetrics {
		if p.Period == curr.Period {
			d := round(curr.Value-p.Value, 1)
			return &d
		}
	}
	return nil
}

func diskMetricDiff(curr MetricValue, prevDisks []DiskStat, mountpoint string) *float64 {
	for _, d := range prevDisks {
		if d.Mountpoint == mountpoint {
			return metricDiff(curr, d.Metrics)
		}
	}
	return nil
}

func oomDiff(curr OOMEvent, prevEvents []OOMEvent) *int {
	for _, p := range prevEvents {
		if p.Period == curr.Period {
			d := curr.Count - p.Count
			return &d
		}
	}
	return nil
}

type TargetInput struct {
	Name        string
	Instance    string
	Mountpoints []string
}
