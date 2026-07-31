package analyzer

import (
	"fmt"
	"log/slog"
	"time"

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
	Timestamp time.Time     `json:"timestamp"`
	Targets   []TargetStats `json:"targets"`
}

type Engine struct {
	client  *vmclient.Client
	cpu     bool
	memory  bool
	disk    bool
	oom     bool
	periods []string
}

func NewEngine(client *vmclient.Client, cpu, memory, disk, oom bool, periods []string) *Engine {
	slog.Debug("Creating analyzer engine",
		slog.Bool("cpu", cpu),
		slog.Bool("memory", memory),
		slog.Bool("disk", disk),
		slog.Bool("oom", oom),
		slog.Any("periods", periods),
	)
	return &Engine{
		client:  client,
		cpu:     cpu,
		memory:  memory,
		disk:    disk,
		oom:     oom,
		periods: periods,
	}
}

func (e *Engine) RunAnalysis(targets []TargetInput) AnalysisReport {
	slog.Info("Starting metrics analysis", slog.Int("targets", len(targets)))

	report := AnalysisReport{
		Timestamp: time.Now(),
	}

	for _, t := range targets {
		slog.Debug("Analyzing target", slog.String("name", t.Name), slog.String("instance", t.Instance))

		stats := TargetStats{Name: t.Name}

		if e.cpu {
			stats.CPU = e.queryCPU(t.Instance)
		}
		if e.memory {
			stats.Memory = e.queryMemory(t.Instance)
		}
		if e.disk && len(t.Mountpoints) > 0 {
			for _, mp := range t.Mountpoints {
				metrics := e.queryDisk(t.Instance, mp)
				if len(metrics) > 0 {
					stats.Disks = append(stats.Disks, DiskStat{Mountpoint: mp, Metrics: metrics})
				}
			}
		}

		if e.oom {
			stats.OOM = e.queryOOM(t.Instance)
		}

		report.Targets = append(report.Targets, stats)
	}

	slog.Info("Analysis complete",
		slog.Int("targets", len(report.Targets)),
		slog.Time("timestamp", report.Timestamp),
	)

	return report
}

func (e *Engine) queryCPU(instance string) []MetricValue {
	slog.Debug("Querying CPU metrics", slog.String("instance", instance))

	var values []MetricValue

	for _, period := range e.periods {
		query := fmt.Sprintf(
			`100 - avg(rate(node_cpu_seconds_total{mode="idle",instance=%q}[%s])) * 100`,
			instance, period,
		)

		val, err := e.client.QueryInstant(query)
		if err != nil {
			slog.Warn("CPU query failed",
				slog.String("instance", instance),
				slog.String("period", period),
				slog.String("error", err.Error()),
			)
			continue
		}

		slog.Debug("CPU result",
			slog.String("instance", instance),
			slog.String("period", period),
			slog.Float64("value", val),
		)

		values = append(values, MetricValue{Period: period, Value: round(val, 1)})
	}

	return values
}

func (e *Engine) queryMemory(instance string) []MetricValue {
	slog.Debug("Querying memory metrics", slog.String("instance", instance))

	var values []MetricValue

	for _, period := range e.periods {
		query := fmt.Sprintf(
			`avg_over_time((1 - node_memory_MemAvailable_bytes{instance=%q} / node_memory_MemTotal_bytes{instance=%q})[%s:2m]) * 100`,
			instance, instance, period,
		)

		val, err := e.client.QueryInstant(query)
		if err != nil {
			slog.Warn("Memory query failed",
				slog.String("instance", instance),
				slog.String("period", period),
				slog.String("error", err.Error()),
			)
			continue
		}

		slog.Debug("Memory result",
			slog.String("instance", instance),
			slog.String("period", period),
			slog.Float64("value", val),
		)

		values = append(values, MetricValue{Period: period, Value: round(val, 1)})
	}

	return values
}

func (e *Engine) queryDisk(instance, mountpoint string) []MetricValue {
	slog.Debug("Querying disk metrics", slog.String("instance", instance), slog.String("mountpoint", mountpoint))

	var values []MetricValue

	for _, period := range e.periods {
		query := fmt.Sprintf(
			`avg_over_time((1 - node_filesystem_avail_bytes{mountpoint=%q,instance=%q} / node_filesystem_size_bytes{mountpoint=%q,instance=%q})[%s:2m]) * 100`,
			mountpoint, instance, mountpoint, instance, period,
		)

		val, err := e.client.QueryInstant(query)
		if err != nil {
			slog.Warn("Disk query failed",
				slog.String("instance", instance),
				slog.String("mountpoint", mountpoint),
				slog.String("period", period),
				slog.String("error", err.Error()),
			)
			continue
		}

		slog.Debug("Disk result",
			slog.String("instance", instance),
			slog.String("mountpoint", mountpoint),
			slog.String("period", period),
			slog.Float64("value", val),
		)

		values = append(values, MetricValue{Period: period, Value: round(val, 1)})
	}

	return values
}

func (e *Engine) queryOOM(instance string) []OOMEvent {
	slog.Debug("Querying OOM events", slog.String("instance", instance))

	var events []OOMEvent

	for _, period := range e.periods {
		query := fmt.Sprintf(
			`sum(increase(node_vmstat_oom_kill{instance=%q}[%s]))`,
			instance, period,
		)

		val, err := e.client.QueryInstant(query)
		if err != nil {
			slog.Debug("OOM query returned no data",
				slog.String("instance", instance),
				slog.String("period", period),
			)
			continue
		}

		count := int(val)
		slog.Debug("OOM result",
			slog.String("instance", instance),
			slog.String("period", period),
			slog.Int("count", count),
		)

		if count > 0 {
			events = append(events, OOMEvent{Period: period, Count: count})
		}
	}

	return events
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

	return current
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
