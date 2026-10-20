"""Read-only dashboard regressions; no backup script or scheduler is started."""
import ast
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app

ROOT = Path(__file__).resolve().parents[1]
PAGES = ("overview", "backup", "restore", "schedules", "settings", "logs", "statistics")


class CommandCenterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="command-center-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        config = self.root / "_config"
        config.mkdir()
        self.schedule = {
            **app._default_schedule(), "id": "existing-weekly", "name": "Existing weekly job",
            "frequency": "weekly", "weekdays": [0, 2, 6], "time": "02:37", "retention": 19,
            "mode": "selected", "containers": ["example_db"], "skip_network": True,
            "container_mounts": {"example_db": {"all": False, "mounts": ["vol:db"], "skip_network": True}},
        }
        (config / "schedules.json").write_text(json.dumps({"schedules": [self.schedule]}), encoding="utf-8")
        (config / "settings.json").write_text('{"destination":"existing/archive","manual_retention":23}', encoding="utf-8")
        run = self.root / "existing-weekly" / "2026-01-02_023700"
        (run / "example" / "example_db").mkdir(parents=True)
        (run / "_BACKUP_OK.txt").write_bytes(b"Existing completion marker\n")
        (run / "example" / "example_db" / "volume-db.tar.gz").write_bytes(b"existing archive")
        self.patches = [
            patch.object(app, "BACKUP_ROOT", str(self.root)),
            patch.object(app, "SCHEDULES_PATH", str(config / "schedules.json")),
            patch.object(app, "SETTINGS_PATH", str(config / "settings.json")),
            patch.object(app, "LEGACY_PATH", str(config / "schedule.json")),
            patch.object(app, "list_containers", return_value=[
                {"name": "example_db", "project": "example", "state": "running", "image": "postgres"}]),
            patch.object(app, "list_mounts", return_value=[]),
            patch.object(app, "list_self_mounts", return_value=[]),
            patch.object(app, "read_log_tail", return_value=["Existing backup log\n"]),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def snapshot(self):
        return {str(path.relative_to(self.root)): (
            hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
            for path in self.root.rglob("*") if path.is_file()}

    def get(self, path):
        with urlopen(self.base + path, timeout=10) as response:
            return response.headers, response.read()

    def test_dashboard_reads_preserve_settings_schedules_and_backups(self):
        before = self.snapshot()
        for endpoint in ("/", "/assets/command-center.css", "/assets/command-center.js",
                         "/api/state", "/api/schedules", "/api/settings", "/api/runs",
                         "/api/mounts", "/api/self-mounts", "/api/logs"):
            with self.subTest(endpoint=endpoint):
                headers, body = self.get(endpoint)
                self.assertTrue(body)
                if endpoint.endswith(".css"):
                    self.assertIn("text/css", headers["Content-Type"])
                elif endpoint.endswith(".js"):
                    self.assertIn("text/javascript", headers["Content-Type"])
        self.assertEqual(self.snapshot(), before)
        _, body = self.get("/api/schedules")
        self.assertEqual(json.loads(body)["schedules"], [self.schedule])

    def test_existing_restore_points_remain_discoverable(self):
        before = self.snapshot()
        _, body = self.get("/api/runs")
        runs = json.loads(body)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["run"], "existing-weekly/2026-01-02_023700")
        self.assertEqual(runs[0]["projects"][0]["containers"], ["example_db"])
        self.assertEqual(self.snapshot(), before)

    def test_exact_public_pages_and_existing_controls(self):
        import re
        _, body = self.get("/")
        html = body.decode()
        self.assertEqual(set(re.findall(r'<section id="tab-([^"]+)"', html)), set(PAGES))
        self.assertEqual(set(re.findall(r'<button data-tab="([^"]+)"', html)), set(PAGES))
        for control in ("ctn", "scheds", "runs", "setRoot", "setDest", "setManualRet",
                        "btnStop", "progBar", "containerSearch", "logFilter", "downloadLog",
                        "statsPeriod", "statsProject"):
            self.assertIn(f'id="{control}"', html)

    def test_only_explicit_presentation_assets_are_served(self):
        for path in ("/assets/../app.py", "/assets/missing.js", "/assets/icon.svg",
                     "/api/external-status"):
            with self.subTest(path=path), self.assertRaises(HTTPError) as raised:
                self.get(path)
            self.assertEqual(raised.exception.code, 404)

    def test_app_has_no_local_monitor_import_dependency(self):
        tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
        imports = [node.module.split(".")[0] for node in ast.walk(tree)
                   if isinstance(node, ast.ImportFrom) and node.module]
        self.assertTrue(set(imports).issubset(sys.stdlib_module_names))

    def test_build_context_explicitly_allows_only_runtime_files(self):
        allowed = [line[1:] for line in (ROOT / ".dockerignore").read_text().splitlines()
                   if line.startswith("!")]
        self.assertEqual(allowed, ["Dockerfile", "app.py", "backup.sh", "restore.sh",
                                  "assets/", "assets/command-center.css", "assets/command-center.js"])
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn("COPY assets/command-center.css assets/command-center.js /usr/local/bin/assets/", dockerfile)
        self.assertNotIn("COPY . ", dockerfile)

    def test_scripts_remain_executable_in_packaged_image(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        for name in ("backup.sh", "restore.sh", "app.py"):
            self.assertIn("/usr/local/bin/" + name, dockerfile)
        self.assertIn('ENTRYPOINT ["python3", "/usr/local/bin/app.py"]', dockerfile)


if __name__ == "__main__":
    unittest.main()
