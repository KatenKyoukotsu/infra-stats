import os
import tempfile
import unittest

from app.config.config import BlackboxConfig, Config, TargetConfig, load_config


def _write_yaml(text: str) -> str:
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    tmp.write(text)
    tmp.close()
    return tmp.name


class TargetUrlConfigTest(unittest.TestCase):
    def test_parses_url_from_target(self):
        path = _write_yaml(
            """
victoria_metrics:
  url: "http://vm:8428"
targets:
  - name: "local-dev"
    instance: "node-exporter:9100"
    mountpoints:
      - "/var/lib"
    url: "https://ya.ru"
"""
        )
        self.addCleanup(os.unlink, path)
        cfg: Config = load_config(path)

        t: TargetConfig = cfg.targets[0]
        self.assertEqual(t.name, "local-dev")
        self.assertEqual(t.url, "https://ya.ru")

    def test_url_defaults_empty(self):
        path = _write_yaml(
            "victoria_metrics:\n  url: http://vm:8428\n"
            "targets:\n  - name: x\n    instance: y:9100\n"
        )
        self.addCleanup(os.unlink, path)
        cfg: Config = load_config(path)
        self.assertEqual(cfg.targets[0].url, "")


class BlackboxConfigTest(unittest.TestCase):
    def test_parses_job(self):
        path = _write_yaml(
            """
victoria_metrics:
  url: "http://vm:8428"
blackbox:
  job: "bb-prod"
"""
        )
        self.addCleanup(os.unlink, path)
        bb: BlackboxConfig = load_config(path).blackbox
        self.assertEqual(bb.job, "bb-prod")

    def test_job_defaults(self):
        path = _write_yaml("victoria_metrics:\n  url: http://vm:8428\n")
        self.addCleanup(os.unlink, path)
        bb: BlackboxConfig = load_config(path).blackbox
        self.assertEqual(bb.job, "blackbox")

    def test_provided_yaml_loads(self):
        here = os.path.dirname(os.path.abspath(__file__))
        cfg: Config = load_config(os.path.join(here, "..", "configs", "config.yaml"))
        self.assertEqual(cfg.blackbox.job, "blackbox")
        self.assertEqual(cfg.targets[0].url, "https://ya.ru")


if __name__ == "__main__":
    unittest.main()
