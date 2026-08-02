package analyzer

import (
	"context"
	"fmt"
	"log/slog"
	"sort"
	"strings"

	"infra-stats/internal/vmclient"
)

const containerGroupLabels = "envir, job, instance, name, department"

// RunContainers собирает аналитику по контейнерам (cadvisor) за все периоды.
// Возвращает все контейнеры с данными; решение «что показывать» — в notifier.
func (e *Engine) RunContainers(ctx context.Context) []ContainerStat {
	slog.Info("Starting container analysis",
		slog.Int("periods", len(e.periods)),
		slog.Any("periods", e.periods),
	)

	sel := containerSelector(e.containersCfg.Filters)

	limitCores, err := e.client.Query(ctx, fmt.Sprintf(
		`sum by (%s) ((container_spec_cpu_quota%s > 0) / (container_spec_cpu_period%s > 0))`,
		containerGroupLabels, sel, sel,
	))
	if err != nil {
		slog.Warn("Container CPU limit query failed", slog.String("error", err.Error()))
		limitCores = nil
	}
	limitByKey := indexContainerByKey(limitCores)

	nodeCores, err := e.client.Query(ctx, `count by (instance) (node_cpu_seconds_total{instance!=""})`)
	if err != nil {
		slog.Warn("Node CPU cores query failed", slog.String("error", err.Error()))
		nodeCores = nil
	}
	coresByInstance := indexByLabel(nodeCores, "instance")

	nodeMem, err := e.client.Query(ctx, `node_memory_MemTotal_bytes{instance!=""}`)
	if err != nil {
		slog.Warn("Node memory total query failed", slog.String("error", err.Error()))
		nodeMem = nil
	}
	memTotalByInstance := indexByLabel(nodeMem, "instance")

	stats := make(map[string]*ContainerStat)

	for _, period := range e.periods {
		usage, err := e.client.Query(ctx, fmt.Sprintf(
			`sum by (%s) (rate(container_cpu_usage_seconds_total%s[%s]))`,
			containerGroupLabels, sel, period,
		))
		if err != nil {
			slog.Warn("Container CPU usage query failed", slog.String("period", period), slog.String("error", err.Error()))
			continue
		}

		memRatio, err := e.client.Query(ctx, fmt.Sprintf(
			`avg_over_time((container_memory_working_set_bytes%s / container_spec_memory_limit_bytes%s)[%s:2m]) * 100`,
			sel, sel, period,
		))
		if err != nil {
			slog.Warn("Container memory ratio query failed", slog.String("period", period), slog.String("error", err.Error()))
			continue
		}

		wsAvg, err := e.client.Query(ctx, fmt.Sprintf(
			`avg_over_time(container_memory_working_set_bytes%s[%s:2m])`,
			sel, period,
		))
		if err != nil {
			slog.Warn("Container working set query failed", slog.String("period", period), slog.String("error", err.Error()))
			continue
		}

		for _, s := range usage {
			key := containerKeyOf(s.Metric)
			if !finite(s.Value) {
				continue
			}
			st := getOrCreateContainer(stats, s.Metric)

			if limitCores, ok := limitByKey[key]; ok && finite(limitCores) && limitCores > 0 {
				st.CPU = append(st.CPU, MetricValue{Period: period, Value: round(s.Value/limitCores*100, 1)})
			}
			if cores, ok := coresByInstance[s.Metric["instance"]]; ok && finite(cores) && cores > 0 {
				st.CPUVM = append(st.CPUVM, MetricValue{Period: period, Value: round(s.Value/cores*100, 1)})
			}
		}

		for _, s := range memRatio {
			if !finite(s.Value) {
				continue
			}
			st := getOrCreateContainer(stats, s.Metric)
			st.Memory = append(st.Memory, MetricValue{Period: period, Value: round(s.Value, 1)})
		}

		for _, s := range wsAvg {
			if !finite(s.Value) {
				continue
			}
			nodeTotal, ok := memTotalByInstance[s.Metric["instance"]]
			if !ok || !finite(nodeTotal) || nodeTotal <= 0 {
				continue
			}
			st := getOrCreateContainer(stats, s.Metric)
			st.MemVM = append(st.MemVM, MetricValue{Period: period, Value: round(s.Value/nodeTotal*100, 1)})
		}
	}

	containers := make([]ContainerStat, 0, len(stats))
	for _, st := range stats {
		containers = append(containers, *st)
	}
	sort.Slice(containers, func(i, j int) bool {
		if containers[i].Instance != containers[j].Instance {
			return containers[i].Instance < containers[j].Instance
		}
		return containers[i].Name < containers[j].Name
	})

	slog.Info("Container analysis complete", slog.Int("containers", len(containers)))
	return containers
}

func containerSelector(filters map[string]string) string {
	parts := []string{`name!=""`}
	keys := make([]string, 0, len(filters))
	for k := range filters {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		parts = append(parts, fmt.Sprintf(`%s=%q`, k, filters[k]))
	}
	return "{" + strings.Join(parts, ",") + "}"
}

func containerKeyOf(m map[string]string) string {
	return strings.Join([]string{
		m["envir"], m["job"], m["instance"], m["name"], m["department"],
	}, "\x00")
}

func indexContainerByKey(series []vmclient.Series) map[string]float64 {
	out := make(map[string]float64, len(series))
	for _, s := range series {
		out[containerKeyOf(s.Metric)] = s.Value
	}
	return out
}

func indexByLabel(series []vmclient.Series, label string) map[string]float64 {
	out := make(map[string]float64, len(series))
	for _, s := range series {
		out[s.Metric[label]] = s.Value
	}
	return out
}

func getOrCreateContainer(stats map[string]*ContainerStat, m map[string]string) *ContainerStat {
	key := containerKeyOf(m)
	st, ok := stats[key]
	if !ok {
		st = &ContainerStat{
			Name:       strings.TrimPrefix(m["name"], "/"),
			Instance:   m["instance"],
			Job:        m["job"],
			Envir:      m["envir"],
			Department: m["department"],
		}
		stats[key] = st
	}
	return st
}
