import os
import tempfile
import unittest
from datetime import datetime

from app.analyzer.analyzer import AnalysisReport, ContainerStat, MetricValue, TargetStats
from app.config.config import ContainersConfig
from app.storage.storage import Storage


def _report(n=0, containers=2):
    cc_list = [
        ContainerStat(name=f"c{i}", instance="vm", job="j", cpu=[MetricValue("1d", 95.0)])
        for i in range(containers)
    ]
    return AnalysisReport(
        timestamp=datetime(2026, 8, 1, 0, 0, n),
        targets=[TargetStats(name="srv")],
        containers=cc_list,
    )


class StorageTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="infra_stats_test_")
        self.path = os.path.join(self._tmpdir, "test.db")
        self.addCleanup(_rmtree, self._tmpdir)

    def _store(self, max_reports=10):
        return Storage(path=self.path, max_reports=max_reports)

    def test_keeps_full_last_and_trimmed_history(self):
        store = self._store()
        cc = ContainersConfig(high_threshold=70.0, change_threshold=5.0)

        store.add_report(_report(0), cc)
        store.add_report(_report(1), cc)

        last, ok = store.get_last_report()
        self.assertTrue(ok)
        self.assertEqual(len(last.containers), 2)  # полный, для диффов

        history = store.get_all_reports()
        self.assertEqual(len(history), 2)
        for r in history:
            self.assertEqual(len(r.containers), 2)  # все контейнеры значимые (95%)

    def test_trimmed_history_drops_quiet_containers(self):
        store = self._store()
        cc = ContainersConfig(high_threshold=70.0, change_threshold=5.0)

        report = AnalysisReport(
            timestamp=datetime(2026, 8, 1),
            targets=[TargetStats(name="srv")],
            containers=[
                ContainerStat(name="hot", instance="vm", job="j", cpu=[MetricValue("1d", 95.0)]),
                ContainerStat(name="quiet", instance="vm", job="j", cpu=[MetricValue("1d", 10.0)]),
            ],
        )
        store.add_report(report, cc)

        last, _ = store.get_last_report()
        self.assertEqual(len(last.containers), 2)  # полный сохранён
        self.assertEqual(len(store.get_all_reports()[0].containers), 1)  # история урезана

    def test_caps_history_but_keeps_last(self):
        store = self._store(max_reports=3)
        cc = ContainersConfig(high_threshold=70.0, change_threshold=5.0)
        for i in range(5):
            store.add_report(_report(i), cc)
        self.assertEqual(len(store.get_all_reports()), 3)
        self.assertTrue(store.get_last_report()[1])

    def test_persists_history_across_restart(self):
        cc = ContainersConfig(high_threshold=70.0, change_threshold=5.0)
        store = self._store()
        store.add_report(_report(0), cc)
        store.add_report(_report(1), cc)
        store.add_notification({"timestamp": "2026-08-01T00:00:00", "success": True, "chat_id": "x"})

        reopened = self._store()
        last, ok = reopened.get_last_report()  # последний отчёт поднят из БД
        self.assertTrue(ok)
        self.assertEqual(last.timestamp, datetime(2026, 8, 1, 0, 0, 1))
        self.assertEqual(len(reopened.get_all_reports()), 2)
        self.assertEqual(reopened.get_all_reports()[1].targets[0].name, "srv")
        self.assertEqual(len(reopened.get_notifications()), 1)

    def test_last_is_none_on_empty_db(self):
        store = self._store()
        self.assertFalse(store.get_last_report()[1])

    def test_roundtrip_matches_original(self):
        cc = ContainersConfig(high_threshold=70.0, change_threshold=5.0)
        report = _report(0)
        report.targets[0].cpu.append(MetricValue("7d", 42.5, diff=2.0))
        report.targets[0].disks = []
        from app.analyzer.analyzer import DiskStat, OOMEvent

        report.targets[0].add_disk("/", MetricValue("1d", 61.0, diff=1.5))
        report.targets[0].oom.append(OOMEvent("1d", 2, diff=1))
        store = self._store()
        store.add_report(report, cc)

        stored = store.get_all_reports()[0]
        self.assertEqual(stored.to_dict(), report.to_dict())

    def test_clear(self):
        store = self._store()
        cc = ContainersConfig(high_threshold=70.0, change_threshold=5.0)
        store.add_report(_report(0), cc)
        store.add_notification({"timestamp": "2026-08-01T00:00:00", "success": True, "chat_id": "x"})
        store.clear()
        self.assertFalse(store.get_last_report()[1])
        self.assertEqual(store.get_all_reports(), [])
        self.assertEqual(store.get_notifications(), [])

    def test_caps_notifications(self):
        store = self._store()
        for i in range(55):
            store.add_notification({"timestamp": f"2026-08-01T00:00:{i:02d}", "success": True, "chat_id": str(i)})
        notifications = store.get_notifications()
        self.assertEqual(len(notifications), 50)
        self.assertEqual(notifications[-1]["chat_id"], "54")


def _rmtree(path: str) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
