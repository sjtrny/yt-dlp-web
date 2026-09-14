"""Public-safe integration tests for stock, package overrides, and plugins.

Run with the project's installed requirements:
    python -m unittest discover -s tests -v

For the system plugin mount check, mount tests/fixtures/yt-dlp/plugins read-only
at /etc/yt-dlp/plugins and set YTDLP_TEST_SYSTEM_PLUGINS=1. All media is generated
locally; the tests need no internet access and do not use /downloads.
"""

from importlib.metadata import distribution
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

import yt_dlp


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "entrypoint.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
UNSET = object()

PROBE = """
import inspect, json, os, shutil
from importlib.metadata import distribution
from pathlib import Path
import yt_dlp
from yt_dlp import YoutubeDL
from yt_dlp.postprocessor.common import PostProcessor
print(json.dumps({
    'package': str(Path(yt_dlp.__file__).resolve()),
    'ydl': str(Path(inspect.getfile(YoutubeDL)).resolve()),
    'postprocessor': str(Path(inspect.getfile(PostProcessor)).resolve()),
    'metadata': str(Path(distribution('yt-dlp').locate_file('')).resolve()),
    'pythonpath': os.environ.get('PYTHONPATH', ''),
    'helper': shutil.which('container-override-probe'),
}))
"""


class RuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = TemporaryDirectory(prefix="yt-dlp-web-tests-")
        cls.temp_path = Path(cls.temp.name)
        cls.bundle = cls.temp_path / "bundle"
        cls.bundle.mkdir()
        package = Path(yt_dlp.__file__).resolve().parent
        metadata = distribution("yt-dlp")
        metadata_files = [
            file for file in metadata.files or ()
            if file.name == "METADATA" and file.parent.name.endswith(".dist-info")
        ]
        if len(metadata_files) != 1:
            raise RuntimeError("Tests require an installed yt-dlp distribution with dist-info metadata")
        shutil.copytree(package, cls.bundle / "yt_dlp", ignore=shutil.ignore_patterns("__pycache__"))
        metadata_path = Path(metadata.locate_file(metadata_files[0])).parent
        shutil.copytree(metadata_path, cls.bundle / metadata_path.name)
        (cls.bundle / "yt_dlp" / "_container_override_marker.py").write_text("MARKER = 'neutral-override'\n")
        (cls.bundle / "bin").mkdir()
        helper = cls.bundle / "bin" / "container-override-probe"
        helper.write_text("#!/bin/sh\nprintf '%s\\n' neutral-helper\n")
        helper.chmod(0o755)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def invoke(self, *command, override=UNSET, plugins=False):
        env = os.environ.copy()
        for name in ("YTDLP_OVERRIDE_DIR", "YTDLP_NO_PLUGINS", "PYTHONPATH"):
            env.pop(name, None)
        env["PYTHONNOUSERSITE"] = "1"
        env["XDG_CONFIG_HOME"] = str(self.temp_path / "empty-config")
        if override is not UNSET:
            env["YTDLP_OVERRIDE_DIR"] = str(override)
        if plugins:
            if env.get("YTDLP_TEST_SYSTEM_PLUGINS") != "1":
                env["XDG_CONFIG_HOME"] = str(FIXTURES)
        else:
            env["YTDLP_NO_PLUGINS"] = "1"
        return subprocess.run(
            [sys.executable, str(ENTRYPOINT), *command],
            cwd=ROOT, env=env, text=True, capture_output=True, timeout=150,
        )

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_stock_backend_and_dispatch(self):
        result = self.invoke(sys.executable, "-c", PROBE)
        self.assert_success(result)
        self.assertIn("backend: stock", result.stderr)
        self.assertEqual(json.loads(result.stdout)["package"], str(Path(yt_dlp.__file__).resolve()))

    def test_complete_override_precedes_stock_package_and_helper(self):
        result = self.invoke(sys.executable, "-c", PROBE, override=self.bundle)
        self.assert_success(result)
        self.assertIn("backend: custom", result.stderr)
        selected = json.loads(result.stdout)
        for field in ("package", "ydl", "postprocessor"):
            self.assertTrue(Path(selected[field]).is_relative_to(self.bundle / "yt_dlp"))
        self.assertEqual(selected["metadata"], str(self.bundle))
        self.assertEqual(selected["pythonpath"].split(os.pathsep)[0], str(self.bundle))
        self.assertEqual(selected["helper"], str(self.bundle / "bin" / "container-override-probe"))
        marker = self.invoke(sys.executable, "-c", "from yt_dlp._container_override_marker import MARKER; print(MARKER)", override=self.bundle)
        self.assert_success(marker)
        self.assertEqual(marker.stdout.strip(), "neutral-override")
        helper = self.invoke("container-override-probe", override=self.bundle)
        self.assert_success(helper)
        self.assertEqual(helper.stdout.strip(), "neutral-helper")

    def test_invalid_override_never_dispatches_command(self):
        empty = self.temp_path / "empty"
        empty.mkdir(exist_ok=True)
        broken = self.temp_path / "broken"
        (broken / "yt_dlp").mkdir(parents=True, exist_ok=True)
        (broken / "yt_dlp" / "__init__.py").write_text("raise RuntimeError('neutral broken fixture')\n")
        for override in ("", "relative/path", self.temp_path / "missing", empty, broken):
            with self.subTest(override=str(override)):
                result = self.invoke(sys.executable, "-c", "print('COMMAND_DISPATCHED')", override=override)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("COMMAND_DISPATCHED", result.stdout)
                self.assertIn("Cannot", result.stderr)

    def test_override_without_own_metadata_is_rejected(self):
        incomplete = self.temp_path / "missing-metadata"
        incomplete.mkdir()
        shutil.copytree(self.bundle / "yt_dlp", incomplete / "yt_dlp")
        result = self.invoke(sys.executable, "-c", "print('COMMAND_DISPATCHED')", override=incomplete)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("distribution metadata", result.stderr)
        self.assertNotIn("COMMAND_DISPATCHED", result.stdout)

    def test_dispatched_command_exit_status_is_preserved(self):
        result = self.invoke(sys.executable, "-c", "raise SystemExit(23)")
        self.assertEqual(result.returncode, 23)

    def test_override_with_invalid_distribution_metadata_is_rejected(self):
        malformed = self.temp_path / "malformed-metadata"
        shutil.copytree(self.bundle, malformed)
        [metadata_file] = malformed.glob("*.dist-info/METADATA")
        for content in ("", "Name: unrelated\nVersion: 1.0\n", "Name: yt-dlp\n"):
            with self.subTest(content=content):
                metadata_file.write_text(content)
                result = self.invoke(sys.executable, "-c", "print('COMMAND_DISPATCHED')", override=malformed)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("distribution metadata", result.stderr)
                self.assertNotIn("COMMAND_DISPATCHED", result.stdout)

    def test_native_plugin_is_loaded_by_python_api(self):
        code = """
import inspect
from yt_dlp import YoutubeDL
with YoutubeDL({'quiet': True}) as ydl:
    extractor = ydl.get_info_extractor('ContainerSmoke')
    print(inspect.getfile(type(extractor)))
"""
        for override in (UNSET, self.bundle):
            with self.subTest(override="stock" if override is UNSET else "custom"):
                result = self.invoke(sys.executable, "-c", code, override=override, plugins=True)
                self.assert_success(result)
                if os.environ.get("YTDLP_TEST_SYSTEM_PLUGINS") == "1":
                    expected = Path("/etc/yt-dlp/plugins")
                else:
                    expected = FIXTURES / "yt-dlp" / "plugins"
                self.assertTrue(Path(result.stdout.strip()).is_relative_to(expected))

    def test_real_web_downloads_with_stock_and_override(self):
        for override in (UNSET, self.bundle):
            with self.subTest(override="stock" if override is UNSET else "custom"):
                result = self.invoke(sys.executable, str(ROOT / "tests" / "_app_smoke.py"), override=override, plugins=True)
                self.assert_success(result)
                self.assertIn("Ran 3 tests", result.stderr)

    def test_real_live_stops_with_stock_and_override(self):
        for override in (UNSET, self.bundle):
            with self.subTest(override="stock" if override is UNSET else "custom"):
                result = self.invoke(
                    sys.executable, str(ROOT / "tests" / "_live_stop_smoke.py"),
                    override=override, plugins=True,
                )
                self.assert_success(result)
                self.assertIn("Ran 5 tests", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
