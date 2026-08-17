import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from app.offline_media import probe_image, resolve_under, validate_inputs
from app.offline_pilot import main, write_manifest


class OfflinePilotTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.input_root = self.root / "input"
        self.output_root = self.root / "output"
        self.tools_root = self.root / "tools"
        self.input_root.mkdir()
        self.output_root.mkdir()
        self.tools_root.mkdir()
        self.font = self.root / "fixed-font.ttf"
        self.font.write_bytes(b"fixed-font")
        self.plan = self.input_root / "plan.json"
        self.plan.write_text(
            json.dumps(
                {
                    "slug": "product-reveal",
                    "lines": ["Sample product", "Made by hand", "Learn more"],
                }
            )
        )
        for name in ("ffmpeg", "ffprobe"):
            tool = self.tools_root / name
            tool.write_bytes(f"fake-{name}".encode())
            tool.chmod(0o755)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _tool_path(self, name):
        return str((self.tools_root / name).resolve())

    def _main_args(self, source, *, output=None, plan="plan.json"):
        arguments = [
            "--input-root",
            str(self.input_root),
            "--output-root",
            str(self.output_root),
            "--plan",
            plan,
            "--font-file",
            str(self.font),
        ]
        if output is not None:
            arguments.extend(["--output", output])
        arguments.append(source.name)
        return arguments

    def _process_fake(self, *, mutate_source=None, render_error=None):
        def run_process(command, **kwargs):
            self.assertIsInstance(command, list)
            self.assertNotIn("shell", kwargs)
            tool = Path(command[0]).name
            if command[1:2] == ["-version"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    f"{tool} version 7.1-static\nbuild details\n",
                    "",
                )
            if tool == "ffmpeg" and command[1:2] == ["-filters"]:
                filters = "\n".join(
                    f" .. {name} V->V available"
                    for name in ("crop", "drawtext", "scale", "xfade", "zoompan")
                )
                return subprocess.CompletedProcess(command, 0, filters, "")
            if tool == "ffprobe" and "-select_streams" in command:
                probed = Path(command[-1])
                self.assertNotEqual(probed.parent, self.input_root)
                self.assertEqual(probed.read_bytes(), b"real-source")
                if mutate_source is not None:
                    mutate_source.write_bytes(b"changed-original")
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps(
                        {
                            "streams": [
                                {
                                    "codec_type": "video",
                                    "codec_name": "mjpeg",
                                    "width": 720,
                                    "height": 1280,
                                }
                            ]
                        }
                    ),
                    "",
                )
            if tool == "ffmpeg":
                output = Path(command[-1])
                self.assertNotEqual(output, self.output_root / "product-reveal.mp4")
                for index, argument in enumerate(command):
                    if argument == "-i":
                        self.assertEqual(Path(command[index + 1]).read_bytes(), b"real-source")
                output.write_bytes(b"partial-video" if render_error else b"rendered-video")
                if render_error:
                    raise subprocess.CalledProcessError(8, command, stderr=render_error)
                return subprocess.CompletedProcess(command, 0, "", "")
            if tool == "ffprobe":
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps(
                        {
                            "streams": [
                                {
                                    "codec_type": "video",
                                    "codec_name": "h264",
                                    "width": 720,
                                    "height": 1280,
                                    "pix_fmt": "yuv420p",
                                    "r_frame_rate": "30/1",
                                }
                            ],
                            "format": {"duration": "2.000000"},
                        }
                    ),
                    "",
                )
            self.fail(f"unexpected process: {command}")

        return run_process

    def _run_main(self, arguments, process_fake):
        environment = {
            "OFFLINE_REQUIRE_APP_COMMIT": "1",
            "OFFLINE_APP_COMMIT": "a" * 40,
            "OFFLINE_APP_VERSION": "test-version",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("subprocess.run", side_effect=process_fake),
            patch("app.offline_pilot.shutil.which", side_effect=self._tool_path),
        ):
            return main(arguments)

    def test_resolve_under_rejects_escape(self):
        with self.assertRaises(ValueError):
            resolve_under(self.root, self.root / ".." / "escape.jpg", must_exist=False)

    def test_image_probe_and_validation_enforce_codec_and_dimension_limits(self):
        source = self.input_root / "asset.jpg"
        source.write_bytes(b"real-source")

        def probe(stream):
            completed = subprocess.CompletedProcess(
                [],
                0,
                json.dumps({"streams": [stream]}),
                "",
            )
            with patch("app.offline_media.subprocess.run", return_value=completed):
                return probe_image(source)

        with self.assertRaisesRegex(ValueError, "video"):
            probe({"codec_type": "audio", "codec_name": "mp3"})
        with self.assertRaisesRegex(ValueError, "470x470"):
            probe(
                {
                    "codec_type": "video",
                    "codec_name": "mjpeg",
                    "width": 469,
                    "height": 1280,
                }
            )
        with self.assertRaisesRegex(ValueError, "8192"):
            probe(
                {
                    "codec_type": "video",
                    "codec_name": "mjpeg",
                    "width": 8193,
                    "height": 720,
                }
            )
        unsupported = self.input_root / "asset.gif"
        unsupported.write_bytes(b"unsupported")
        with self.assertRaisesRegex(ValueError, "extension"):
            validate_inputs(self.input_root, [unsupported.name])

    def test_main_rejects_plan_and_output_path_escape_before_processes_run(self):
        source = self.input_root / "asset.jpg"
        source.write_bytes(b"real-source")
        (self.output_root / "nested").mkdir()
        cases = (
            self._main_args(source, output="../escape.mp4"),
            self._main_args(source, output="nested/output.mp4"),
            self._main_args(source, plan="../plan.json"),
        )
        for arguments in cases:
            stderr = io.StringIO()
            with patch("subprocess.run") as run_process, redirect_stderr(stderr):
                result = main(arguments)
            self.assertEqual(result, 1)
            self.assertIn("path", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            run_process.assert_not_called()

    def test_main_rejects_invalid_or_oversized_plan_before_processes_run(self):
        source = self.input_root / "asset.jpg"
        source.write_bytes(b"real-source")
        documents = (
            {"slug": "sample", "lines": ["Sample"], "secret": "unexpected"},
            {"slug": "UPPERCASE", "lines": ["Sample"]},
            {"slug": "sample", "lines": ["line\nbreak"]},
        )
        for document in documents:
            self.plan.write_text(json.dumps(document))
            with patch("subprocess.run") as run_process:
                self.assertEqual(main(self._main_args(source)), 1)
            run_process.assert_not_called()
        self.plan.write_bytes(b"x" * 8193)
        with patch("subprocess.run") as run_process:
            self.assertEqual(main(self._main_args(source)), 1)
        run_process.assert_not_called()

    def test_main_fails_before_render_when_required_ffmpeg_filter_is_missing(self):
        source = self.input_root / "asset.jpg"
        source.write_bytes(b"real-source")
        normal_process = self._process_fake()

        def missing_drawtext(command, **kwargs):
            if Path(command[0]).name == "ffmpeg" and command[1:2] == ["-filters"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    " .. crop V->V\n .. scale V->V\n .. xfade VV->V\n .. zoompan V->V",
                    "",
                )
            return normal_process(command, **kwargs)

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = self._run_main(self._main_args(source), missing_drawtext)

        self.assertEqual(result, 1)
        self.assertIn("required FFmpeg filters unavailable: drawtext", stderr.getvalue())
        self.assertEqual(list(self.output_root.iterdir()), [])

    def test_main_snapshots_media_and_writes_portable_neutral_manifest(self):
        source = self.input_root / "asset.jpg"
        source.write_bytes(b"real-source")
        result = self._run_main(
            self._main_args(source),
            self._process_fake(mutate_source=source),
        )

        self.assertEqual(result, 0)
        self.assertEqual(source.read_bytes(), b"changed-original")
        output = self.output_root / "product-reveal.mp4"
        self.assertEqual(output.read_bytes(), b"rendered-video")
        manifest_path = self.output_root / "product-reveal.manifest.json"
        manifest_text = manifest_path.read_text()
        manifest = json.loads(manifest_text)
        self.assertNotIn(str(self.root), manifest_text)
        self.assertEqual(
            manifest["features"],
            {
                "ai_media_generation": False,
                "generated_music": False,
                "llm_scripting": False,
                "network": False,
                "publication": False,
                "stock_media": False,
                "tts": False,
            },
        )
        self.assertEqual(
            manifest["plan"],
            {
                "lines": ["Sample product", "Made by hand", "Learn more"],
                "slug": "product-reveal",
            },
        )
        self.assertEqual(
            manifest["sources"],
            [
                {
                    "codec": "mjpeg",
                    "height": 1280,
                    "name": "asset.jpg",
                    "sha256": hashlib.sha256(b"real-source").hexdigest(),
                    "size_bytes": len(b"real-source"),
                    "width": 720,
                }
            ],
        )
        self.assertEqual(manifest["output"]["path"], "product-reveal.mp4")
        self.assertEqual(manifest["output"]["sha256"], hashlib.sha256(b"rendered-video").hexdigest())
        self.assertEqual(manifest["identity"]["application"]["commit"], "a" * 40)
        self.assertEqual(manifest["identity"]["application"]["version"], "test-version")
        self.assertEqual(manifest["identity"]["tools"]["ffmpeg"]["name"], "ffmpeg")
        self.assertNotIn("command", manifest["render"])

    def test_main_removes_video_if_manifest_transaction_fails(self):
        source = self.input_root / "asset.jpg"
        source.write_bytes(b"real-source")
        stderr = io.StringIO()
        with (
            patch(
                "app.offline_pilot.write_manifest",
                side_effect=OSError("manifest transaction failed"),
            ),
            redirect_stderr(stderr),
        ):
            result = self._run_main(self._main_args(source), self._process_fake())

        self.assertEqual(result, 1)
        self.assertIn("manifest transaction failed", stderr.getvalue())
        self.assertEqual(list(self.output_root.iterdir()), [])

    def test_manifest_publication_never_overwrites_an_existing_destination(self):
        destination = self.output_root / "sample.manifest.json"
        destination.write_bytes(b"existing-manifest")

        with self.assertRaises(FileExistsError):
            write_manifest(destination, {"schema_version": 1})

        self.assertEqual(destination.read_bytes(), b"existing-manifest")
        self.assertEqual(list(self.output_root.iterdir()), [destination])

    def test_manifest_publication_never_follows_a_raced_in_symlink(self):
        destination = self.output_root / "sample.manifest.json"
        outside = self.root / "outside.manifest.json"
        destination.symlink_to(outside)

        with self.assertRaises(FileExistsError):
            write_manifest(destination, {"schema_version": 1})

        self.assertTrue(destination.is_symlink())
        self.assertFalse(outside.exists())


if __name__ == "__main__":
    unittest.main()
