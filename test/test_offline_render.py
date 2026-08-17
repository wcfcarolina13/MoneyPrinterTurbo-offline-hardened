import hashlib
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.offline_media import MediaAsset
from app.offline_render import RenderPlan, build_ffmpeg_command, render_video


class OfflineRenderTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.input_root = self.root / "input"
        self.output_root = self.root / "output"
        self.input_root.mkdir()
        self.output_root.mkdir()
        self.font = self.root / "fixed-font.ttf"
        self.font.write_bytes(b"fixed-font")
        self.assets = []
        for index in range(3):
            path = self.input_root / f"asset-{index}.jpg"
            content = f"source-{index}".encode()
            path.write_bytes(content)
            self.assets.append(
                MediaAsset(
                    path=path.resolve(),
                    sha256=hashlib.sha256(content).hexdigest(),
                    size_bytes=len(content),
                    width=720,
                    height=1280,
                    codec="mjpeg",
                )
            )

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def _output_probe(command):
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
                    "format": {"duration": "5.200000"},
                }
            ),
            "",
        )

    def test_render_plan_requires_explicit_bounded_copy(self):
        plan = RenderPlan(
            slug="product-reveal",
            lines=("Sample product", "Made by hand", "Learn more"),
        )
        self.assertEqual(plan.slug, "product-reveal")
        self.assertEqual(plan.lines[0], "Sample product")

        invalid = (
            {"slug": "", "lines": ("Sample",)},
            {"slug": "UPPERCASE", "lines": ("Sample",)},
            {"slug": "sample", "lines": ()},
            {"slug": "sample", "lines": ("one", "two", "three", "four")},
            {"slug": "sample", "lines": ("line\nbreak",)},
            {"slug": "sample", "lines": ("x" * 81,)},
        )
        for values in invalid:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    RenderPlan(**values)

    def test_command_uses_validated_assets_and_fixed_offline_policy(self):
        output = self.output_root / "sample.mp4"
        plan = RenderPlan(
            slug="sample",
            lines=("Sample product", "Made locally", "@sample"),
        )
        command = build_ffmpeg_command(
            self.assets,
            output,
            plan,
            font_file=self.font,
        )

        self.assertIsInstance(command, list)
        self.assertTrue(all(isinstance(argument, str) for argument in command))
        self.assertEqual(command[0], "ffmpeg")
        self.assertIn("-n", command)
        self.assertNotIn("-y", command)
        self.assertIn("-an", command)
        self.assertEqual(command[command.index("-s") + 1], "720x1280")
        self.assertEqual(command[command.index("-c:v") + 1], "libx264")
        self.assertEqual(command[command.index("-pix_fmt") + 1], "yuv420p")
        self.assertEqual(command[command.index("-movflags") + 1], "+faststart")
        self.assertEqual(command[command.index("-filter_complex_threads") + 1], "1")
        self.assertEqual(Path(command[-1]), output.resolve())
        input_paths = [
            command[index + 1]
            for index, argument in enumerate(command)
            if argument == "-i"
        ]
        self.assertEqual(input_paths, [str(asset.path) for asset in self.assets])
        filter_graph = command[command.index("-filter_complex") + 1]
        self.assertIn("zoompan", filter_graph)
        self.assertIn("xfade", filter_graph)
        self.assertIn("expansion=none", filter_graph)
        for line in plan.lines:
            self.assertIn(line, filter_graph)
        joined = " ".join(command).lower()
        for blocked in ("shell=true", "http://", "https://", "provider", "stock-media"):
            self.assertNotIn(blocked, joined)

    def test_long_copy_uses_smaller_type_and_filter_metacharacters_are_escaped(self):
        output = self.output_root / "sample.mp4"
        long_line = "Contact: @sample, today; 100%"
        plan = RenderPlan(slug="sample", lines=("Short", long_line))
        command = build_ffmpeg_command(
            self.assets,
            output,
            plan,
            font_file=self.font,
        )
        filter_graph = command[command.index("-filter_complex") + 1]

        short_size = re.search(
            r"text='Short':expansion=none:fontcolor=white:fontsize=(\d+)",
            filter_graph,
        )
        long_size = re.search(
            r"text='Contact\\: @sample\\, today\\; 100%':expansion=none:"
            r"fontcolor=white:fontsize=(\d+)",
            filter_graph,
        )
        self.assertIsNotNone(short_size)
        self.assertIsNotNone(long_size)
        self.assertEqual(int(short_size.group(1)), 52)
        self.assertLessEqual(int(long_size.group(1)), 40)

    def test_render_is_atomic_and_records_only_supplied_copy(self):
        output = self.output_root / "sample.mp4"
        plan = RenderPlan(
            slug="sample",
            lines=("Sample product", "Made locally", "@sample"),
        )
        rendered_temp = None

        def run_process(command, **kwargs):
            nonlocal rendered_temp
            self.assertNotIn("shell", kwargs)
            if Path(command[0]).name == "ffmpeg":
                self.assertEqual(kwargs["timeout"], 120)
                self.assertFalse(output.exists())
                rendered_temp = Path(command[-1])
                self.assertNotEqual(rendered_temp, output)
                self.assertEqual(rendered_temp.parent.parent, self.output_root.resolve())
                rendered_temp.write_bytes(b"rendered-video")
                return subprocess.CompletedProcess(command, 0, "", "")
            self.assertEqual(kwargs["timeout"], 30)
            return self._output_probe(command)

        with patch("app.offline_render.subprocess.run", side_effect=run_process):
            result = render_video(
                self.assets,
                output,
                plan,
                font_file=self.font,
            )

        self.assertEqual(output.read_bytes(), b"rendered-video")
        self.assertIsNotNone(rendered_temp)
        self.assertFalse(rendered_temp.parent.exists())
        self.assertEqual(result["slug"], "sample")
        self.assertEqual(result["copy"], list(plan.lines))
        self.assertEqual(result["sources"], [asset.sha256 for asset in self.assets])
        self.assertEqual(
            result["output"],
            {
                "audio_streams": 0,
                "codec": "h264",
                "duration_seconds": 5.2,
                "frame_rate": "30/1",
                "height": 1280,
                "path": str(output.resolve()),
                "pixel_format": "yuv420p",
                "sha256": hashlib.sha256(b"rendered-video").hexdigest(),
                "size_bytes": len(b"rendered-video"),
                "video_streams": 1,
                "width": 720,
            },
        )

    def test_render_publication_never_overwrites_a_raced_in_destination(self):
        output = self.output_root / "sample.mp4"
        plan = RenderPlan(slug="sample", lines=("Sample product",))

        def run_process(command, **kwargs):
            if Path(command[0]).name == "ffmpeg":
                Path(command[-1]).write_bytes(b"rendered-video")
                return subprocess.CompletedProcess(command, 0, "", "")
            output.write_bytes(b"existing-video")
            return self._output_probe(command)

        with patch("app.offline_render.subprocess.run", side_effect=run_process):
            with self.assertRaises(FileExistsError):
                render_video(
                    self.assets,
                    output,
                    plan,
                    font_file=self.font,
                )

        self.assertEqual(output.read_bytes(), b"existing-video")


if __name__ == "__main__":
    unittest.main()
