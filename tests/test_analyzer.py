import unittest
from datetime import datetime

from app.analyzer.analyzer import (
    AnalysisReport,
    ContainerStat,
    EndpointStatus,
    Engine,
    MetricValue,
    OOMEvent,
    TargetInput,
    TargetStats,
    compute_diffs,
    container_notable,
    container_hot,
    disk_query,
    instance_selector,
    memory_query,
    mountpoint_selector,
    round_value,
    trim_report_containers,
)
from app.config.config import AnalysisConfig, BlackboxConfig, ContainersConfig
from app.vmclient.vmclient import Series


class RoundValueTest(unittest.TestCase):
    def test_round_half_up(self):
        self.assertEqual(round_value(87.54, 1), 87.5)
        self.assertEqual(round_value(87.55, 1), 87.6)
        self.assertEqual(round_value(12.349, 1), 12.3)


class ComputeDiffsTest(unittest.TestCase):
    def _report(self):
        prev = AnalysisReport(
            timestamp=datetime(2026, 8, 1),
            targets=[
                TargetStats(
                    name="srv",
                    cpu=[MetricValue("1d", 20.0), MetricValue("7d", 15.0)],
                    memory=[MetricValue("1d", 50.0)],
                )
            ],
        )
        cur = AnalysisReport(
            timestamp=datetime(2026, 8, 2),
            targets=[
                TargetStats(
                    name="srv",
                    cpu=[MetricValue("1d", 25.0), MetricValue("7d", 15.0)],
                    memory=[MetricValue("1d", 55.0)],
                )
            ],
        )
        return cur, prev

    def test_target_diffs(self):
        cur, prev = self._report()
        compute_diffs(cur, prev)
        self.assertEqual(cur.targets[0].cpu[0].diff, 5.0)
        self.assertEqual(cur.targets[0].cpu[1].diff, 0.0)
        self.assertEqual(cur.targets[0].memory[0].diff, 5.0)

    def test_oom_diff(self):
        prev = AnalysisReport(
            timestamp=datetime(2026, 8, 1),
            targets=[TargetStats(name="srv", oom=[OOMEvent("1d", 3)])],
        )
        cur = AnalysisReport(
            timestamp=datetime(2026, 8, 2),
            targets=[TargetStats(name="srv", oom=[OOMEvent("1d", 5)])],
        )
        compute_diffs(cur, prev)
        self.assertEqual(cur.targets[0].oom[0].diff, 2)

    def test_container_diff(self):
        # В Go ComputeDiffs выходит рано, если в прошлом отчёте нет targets,
        # поэтому даём прошлому отчёту таргет (как в реальном потоке).
        prev = AnalysisReport(
            timestamp=datetime(2026, 8, 1),
            targets=[TargetStats(name="srv")],
            containers=[
                ContainerStat(
                    name="app",
                    instance="vm:8080",
                    job="cadvisor",
                    cpu=[MetricValue("1d", 80.0)],
                    memory=[MetricValue("1d", 90.0)],
                )
            ],
        )
        cur = AnalysisReport(
            timestamp=datetime(2026, 8, 2),
            targets=[TargetStats(name="srv")],
            containers=[
                ContainerStat(
                    name="app",
                    instance="vm:8080",
                    job="cadvisor",
                    cpu=[MetricValue("1d", 87.5)],
                    memory=[MetricValue("1d", 97.0)],
                )
            ],
        )
        compute_diffs(cur, prev)
        self.assertEqual(cur.containers[0].cpu[0].diff, 7.5)
        self.assertEqual(cur.containers[0].memory[0].diff, 7.0)


class ToDictTest(unittest.TestCase):
    def test_report_to_dict_matches_go_shape(self):
        report = AnalysisReport(
            timestamp=datetime(2026, 8, 1, 10, 30, 0),
            targets=[
                TargetStats(
                    name="srv",
                    url="https://ya.ru",
                    cpu=[MetricValue("1d", 23.4, 1.2)],
                    endpoints=[EndpointStatus(period="1d", status="ok", uptime=100.0)],
                )
            ],
            containers=[
                ContainerStat(
                    name="app",
                    instance="vm:8080",
                    job="cadvisor",
                    envir="prod",
                    cpu=[MetricValue("1d", 87.5)],
                    memory=[MetricValue("1d", 97.0)],
                )
            ],
        )
        d = report.to_dict()
        self.assertIn("timestamp", d)
        t = d["targets"][0]
        self.assertEqual(t["name"], "srv")
        self.assertEqual(t["cpu"][0]["diff"], 1.2)
        self.assertEqual(t["url"], "https://ya.ru")
        self.assertEqual(t["endpoints"][0]["status"], "ok")
        self.assertEqual(t["endpoints"][0]["uptime"], 100.0)
        self.assertNotIn("diff", t["endpoints"][0])
        self.assertEqual(d["containers"][0]["name"], "app")
        # omitempty: дифф без значения не должен сериализоваться
        self.assertNotIn("diff", d["containers"][0]["cpu"][0])
        # пустые списки опускаются
        self.assertNotIn("memory", t)
        self.assertNotIn("oom", t)

    def test_endpoint_roundtrip(self):
        report = AnalysisReport(
            timestamp=datetime(2026, 8, 1, 10, 30, 0),
            targets=[
                TargetStats(
                    name="srv",
                    url="https://ya.ru",
                    endpoints=[
                        EndpointStatus(period="1d", status="ok", uptime=100.0, diff=0.0),
                        EndpointStatus(period="7d", status="unmonitored"),
                    ],
                )
            ],
        )
        restored = AnalysisReport.from_dict(report.to_dict())
        t = restored.targets[0]
        self.assertEqual(t.url, "https://ya.ru")
        self.assertEqual(t.endpoints[0].diff, 0.0)
        self.assertEqual(t.endpoints[1].status, "unmonitored")
        self.assertIsNone(t.endpoints[1].uptime)


class InstanceSelectorTest(unittest.TestCase):
    def test_builds_regex_with_escaping(self):
        sel = instance_selector(["vm-01:9100", "192.168.1.10:9100"])
        self.assertEqual(sel, r'instance=~"vm-01:9100|192\.168\.1\.10:9100"')

    def test_filters_empty_instances(self):
        sel = instance_selector(["", None, "a:1"])
        self.assertIn("a:1", sel)
        self.assertNotIn("||", sel)

    def test_fallback_when_empty(self):
        self.assertEqual(instance_selector([]), 'instance!=""')


class MountpointSelectorTest(unittest.TestCase):
    def test_builds_anchored_regex(self):
        sel = mountpoint_selector(["/", "/var/lib", "/data"])
        self.assertEqual(sel, r'mountpoint=~"^(/|/var/lib|/data)$"')

    def test_escapes_regex_specials(self):
        sel = mountpoint_selector(["/var/lib/data.v2"])
        self.assertEqual(sel, r'mountpoint=~"^(/var/lib/data\.v2)$"')

    def test_empty_falls_back_to_root(self):
        self.assertEqual(mountpoint_selector([]), r'mountpoint=~"^(/)$"')


class QueryBuilderTest(unittest.TestCase):
    def test_memory_query_uses_rollups_not_subquery(self):
        q = memory_query('instance=~"a"', "1d")
        self.assertNotIn(":2m", q)
        self.assertNotIn(":2m", q)
        self.assertIn(
            'avg_over_time(node_memory_MemAvailable_bytes{instance=~"a"}[1d])', q
        )
        self.assertIn('avg_over_time(node_memory_MemTotal_bytes{instance=~"a"}[1d])', q)

    def test_disk_query_has_server_side_filters(self):
        q = disk_query('instance=~"a"', 'mountpoint=~"^(/)$"', "7d")
        self.assertNotIn(":2m", q)
        self.assertIn('fstype!~"tmpfs|overlay|squashfs|ramfs|cgroup|devtmpfs"', q)
        self.assertIn("avg by (instance, mountpoint)", q)
        self.assertIn(
            'node_filesystem_avail_bytes{instance=~"a",mountpoint=~"^(/)$",'
            'fstype!~"tmpfs|overlay|squashfs|ramfs|cgroup|devtmpfs"}[7d]',
            q,
        )


class ContainerQueryBuilderTest(unittest.TestCase):
    def test_node_cores_filters_instances(self):
        from app.analyzer.container import node_cores_query

        self.assertEqual(
            node_cores_query('instance=~"a|b"'),
            'count by (instance) (node_cpu_seconds_total{instance=~"a|b"})',
        )

    def test_node_mem_total_filters_instances(self):
        from app.analyzer.container import node_mem_total_query

        self.assertEqual(
            node_mem_total_query('instance!=""'), 'node_memory_MemTotal_bytes{instance!=""}'
        )

    def test_mem_ratio_uses_rollups_not_subquery(self):
        from app.analyzer.container import container_mem_ratio_query

        q = container_mem_ratio_query('{name!=""}', "1d")
        self.assertNotIn(":2m", q)
        self.assertIn(
            "avg_over_time(container_memory_working_set_bytes{name!=\"\"}[1d])", q
        )
        self.assertIn(
            "avg_over_time(container_spec_memory_limit_bytes{name!=\"\"}[1d])", q
        )

    def test_ws_avg_uses_rollup_not_subquery(self):
        from app.analyzer.container import container_ws_avg_query

        self.assertEqual(
            container_ws_avg_query('{name!=""}', "1d"),
            'avg_over_time(container_memory_working_set_bytes{name!=""}[1d])',
        )


class ContainerSelectorTest(unittest.TestCase):
    def test_with_instances(self):
        from app.analyzer.container import container_selector

        sel = container_selector({"envir": "prod"}, ["vm-1:8080", "vm-2:8080"])
        self.assertIn('name!=""', sel)
        self.assertIn('envir="prod"', sel)
        self.assertIn(r'instance=~"vm-1:8080|vm-2:8080"', sel)

    def test_without_instances_falls_back(self):
        from app.analyzer.container import container_selector

        sel = container_selector({})
        self.assertIn('instance!=""', sel)


class BlackboxQueryBuilderTest(unittest.TestCase):
    def test_uptime_query(self):
        from app.analyzer.blackbox import endpoint_uptime_query

        self.assertEqual(
            endpoint_uptime_query("blackbox", "7d"),
            'avg_over_time(probe_success{job="blackbox"}[7d]) * 100',
        )

    def test_endpoint_url_prefers_target_label(self):
        from app.analyzer.blackbox import endpoint_url_of

        self.assertEqual(
            endpoint_url_of({"target": "https://ya.ru/", "instance": "bb:9115"}),
            "https://ya.ru",
        )
        self.assertEqual(endpoint_url_of({"instance": "https://api.example.com/"}), "https://api.example.com")


class _FakeClient:
    def __init__(self, by_query):
        self._by_query = by_query
        self.queries = []

    async def query(self, query):
        self.queries.append(query)
        return self._by_query.get(query)


class RunAnalysisEndpointsTest(unittest.TestCase):
    def _series(self, *tuples):
        return [Series({"target": url}, value) for url, value in tuples]

    def _run(self, urls_by_name, by_query, periods=None):
        import asyncio

        analysis = AnalysisConfig(
            cpu=False, memory=False, disk=False, oom=False, periods=periods or ["1d"]
        )
        engine = Engine(
            _FakeClient(by_query),
            analysis,
            ContainersConfig(),
            BlackboxConfig(job="blackbox"),
        )
        targets = [
            TargetInput(name=name, instance=f"vm-{i}:9100", url=url)
            for i, (name, url) in enumerate(urls_by_name)
        ]
        return asyncio.run(engine.run_analysis(targets))

    def test_ok_during_periods(self):
        by_query = {
            'avg_over_time(probe_success{job="blackbox"}[1d]) * 100': self._series(
                ("https://ya.ru", 100.0)
            ),
            'avg_over_time(probe_success{job="blackbox"}[7d]) * 100': self._series(
                ("https://ya.ru", 100.0)
            ),
        }
        report = self._run([("local", "https://ya.ru")], by_query, ["1d", "7d"])
        t = report.targets[0]
        self.assertEqual(t.url, "https://ya.ru")
        self.assertEqual([s.status for s in t.endpoints], ["ok", "ok"])
        self.assertEqual([s.uptime for s in t.endpoints], [100.0, 100.0])

    def test_down_when_partial_uptime(self):
        by_query = {
            'avg_over_time(probe_success{job="blackbox"}[1d]) * 100': self._series(
                ("https://api.example.com", 98.4)
            ),
        }
        report = self._run([("api", "https://api.example.com")], by_query)
        st = report.targets[0].endpoints[0]
        self.assertEqual(st.status, "down")
        self.assertEqual(st.uptime, 98.4)

    def test_unmonitored_when_no_series(self):
        by_query = {
            'avg_over_time(probe_success{job="blackbox"}[1d]) * 100': [],
        }
        report = self._run([("api", "https://missing.example.com")], by_query)
        self.assertEqual(report.targets[0].endpoints[0].status, "unmonitored")

    def test_ignores_other_endpoints(self):
        by_query = {
            'avg_over_time(probe_success{job="blackbox"}[1d]) * 100': self._series(
                ("https://ya.ru", 100.0), ("https://google.com", 100.0)
            ),
        }
        report = self._run([("local", "https://ya.ru")], by_query)
        self.assertEqual(report.targets[0].endpoints[0].status, "ok")

    def test_handles_trailing_slash(self):
        by_query = {
            'avg_over_time(probe_success{job="blackbox"}[1d]) * 100': self._series(
                ("https://ya.ru", 100.0)
            ),
        }
        report = self._run([("local", "https://ya.ru/")], by_query)
        self.assertEqual(report.targets[0].endpoints[0].status, "ok")

    def test_target_without_url_skipped(self):
        report = self._run([("local", "")], {})
        self.assertEqual(report.targets[0].endpoints, [])
        self.assertNotIn("url", report.targets[0].to_dict())


class EndpointDiffTest(unittest.TestCase):
    def test_uptime_diff(self):
        prev = AnalysisReport(
            timestamp=datetime(2026, 8, 1),
            targets=[
                TargetStats(
                    name="srv",
                    url="https://ya.ru",
                    endpoints=[EndpointStatus(period="1d", status="ok", uptime=100.0)],
                )
            ],
        )
        cur = AnalysisReport(
            timestamp=datetime(2026, 8, 2),
            targets=[
                TargetStats(
                    name="srv",
                    url="https://ya.ru",
                    endpoints=[EndpointStatus(period="1d", status="down", uptime=97.9)],
                )
            ],
        )
        compute_diffs(cur, prev)
        # Go-стиль округления: int(-20.5) усекается к нулю → -2.0
        self.assertEqual(cur.targets[0].endpoints[0].diff, -2.0)

    def test_skip_diff_when_no_previous_uptime(self):
        prev = AnalysisReport(
            timestamp=datetime(2026, 8, 1),
            targets=[
                TargetStats(
                    name="srv",
                    url="https://ya.ru",
                    endpoints=[EndpointStatus(period="1d", status="unmonitored")],
                )
            ],
        )
        cur = AnalysisReport(
            timestamp=datetime(2026, 8, 2),
            targets=[
                TargetStats(
                    name="srv",
                    url="https://ya.ru",
                    endpoints=[EndpointStatus(period="1d", status="ok", uptime=100.0)],
                )
            ],
        )
        compute_diffs(cur, prev)
        self.assertIsNone(cur.targets[0].endpoints[0].diff)


class NotableTest(unittest.TestCase):
    def _cc(self):
        return ContainersConfig(
            change_threshold=5.0, high_threshold=70.0, cpu_threshold=80.0, mem_threshold=95.0
        )

    def test_high_value_is_notable(self):
        cc = self._cc()
        cn = ContainerStat(name="a", instance="i", job="j", cpu=[MetricValue("1d", 87.5)])
        self.assertTrue(container_notable(cn, cc))
        self.assertTrue(container_hot(cn, cc))

    def test_low_quiet_not_notable(self):
        cc = self._cc()
        cn = ContainerStat(name="a", instance="i", job="j", cpu=[MetricValue("1d", 10.0, 0.0)])
        self.assertFalse(container_notable(cn, cc))
        self.assertFalse(container_hot(cn, cc))

    def test_change_beyond_threshold_is_notable(self):
        cc = self._cc()
        cn = ContainerStat(name="a", instance="i", job="j", cpu=[MetricValue("1d", 15.0, 6.0)])
        self.assertTrue(container_notable(cn, cc))
        self.assertFalse(container_hot(cn, cc))

    def test_change_below_threshold_not_notable(self):
        cc = self._cc()
        cn = ContainerStat(name="a", instance="i", job="j", cpu=[MetricValue("1d", 15.0, 4.9)])
        self.assertFalse(container_notable(cn, cc))

    def test_memory_hot_threshold(self):
        cc = self._cc()
        cn = ContainerStat(name="a", instance="i", job="j", memory=[MetricValue("1d", 96.0)])
        self.assertTrue(container_hot(cn, cc))
        cn2 = ContainerStat(name="b", instance="i", job="j", memory=[MetricValue("1d", 94.0)])
        self.assertFalse(container_hot(cn2, cc))


class TrimReportTest(unittest.TestCase):
    def test_keeps_only_notable_containers(self):
        cc = ContainersConfig(high_threshold=70.0, change_threshold=5.0)
        report = AnalysisReport(
            timestamp=datetime(2026, 8, 1),
            targets=[
                TargetStats(
                    name="srv",
                    url="https://ya.ru",
                    endpoints=[EndpointStatus(period="1d", status="ok", uptime=100.0)],
                )
            ],
            containers=[
                ContainerStat(name="hot", instance="i", job="j", cpu=[MetricValue("1d", 95.0)]),
                ContainerStat(name="quiet", instance="i", job="j", cpu=[MetricValue("1d", 10.0)]),
            ],
        )
        trimmed = trim_report_containers(report, cc)
        self.assertEqual([c.name for c in trimmed.containers], ["hot"])
        self.assertEqual(len(trimmed.targets), 1)
        self.assertEqual(len(report.containers), 2)  # исходный не меняется
        self.assertIn("containers", trimmed.to_dict())
        self.assertEqual(len(trimmed.targets[0].endpoints), 1)  # эндпоинты сохраняются


if __name__ == "__main__":
    unittest.main()
