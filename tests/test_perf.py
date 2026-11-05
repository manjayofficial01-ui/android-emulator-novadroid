"""Tests for the smoothness engine (core/perf.py). No device needed."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import perf as PERF  # noqa: E402

GFX = """
Applications Graphics Acceleration Info:
Uptime: 1234 Realtime: 5678

** Graphics info for pid 1234 [com.example.app] **

Stats since: 1700000000000
Total frames rendered: 200
Janky frames: 40 (20.00%)
50th percentile: 8ms
90th percentile: 20ms
95th percentile: 40ms
99th percentile: 90ms
Number Missed Vsync: 10
Number High input latency: 2
Number Slow UI thread: 5
Number Slow bitmap uploads: 0
Number Slow issue draw commands: 1
Number Frame deadline missed: 12
"""

CLEAN_GFX = """
** Graphics info for pid 99 [com.example.fast] **
Total frames rendered: 1000
Janky frames: 3 (0.30%)
50th percentile: 5ms
95th percentile: 9ms
"""


class TestParseGfxinfo(unittest.TestCase):
    def test_parses_all_fields(self):
        d = PERF.parse_gfxinfo(GFX)
        self.assertEqual(d["frames"], 200)
        self.assertEqual(d["janky"], 40)
        self.assertEqual(d["janky_pct"], 20.0)
        self.assertEqual(d["percentiles"][95], 40)
        self.assertEqual(d["percentiles"][50], 8)
        self.assertEqual(d["counters"]["Missed Vsync"], 10)
        self.assertEqual(d["counters"]["Slow UI thread"], 5)
        self.assertIn("com.example.app", d["packages"])

    def test_empty_is_safe(self):
        d = PERF.parse_gfxinfo("")
        self.assertIsNone(d["frames"])
        self.assertEqual(d["percentiles"], {})
        self.assertEqual(d["counters"], {})

    def test_garbage_is_safe(self):
        d = PERF.parse_gfxinfo("dumpsys: neither user nor service")
        self.assertIsNone(d["frames"])


class TestScore(unittest.TestCase):
    def test_perfect_is_100(self):
        self.assertEqual(PERF.score(0.0, None), 100.0)

    def test_jank_drops_the_score(self):
        # 20% jank -> -40; p95 40ms vs 16.7 budget -> over=1.395 -> -34.9
        s = PERF.score(20.0, 40)
        self.assertLess(s, 30)
        self.assertGreaterEqual(s, 0)

    def test_never_out_of_range(self):
        self.assertEqual(PERF.score(500.0, 5000), 0.0)
        self.assertEqual(PERF.score(None, None), 100.0)

    def test_verdict_bands(self):
        self.assertEqual(PERF.verdict_for(95), "smooth")
        self.assertEqual(PERF.verdict_for(75), "mostly smooth")
        self.assertEqual(PERF.verdict_for(60), "janky")
        self.assertEqual(PERF.verdict_for(10), "stuttering")


class TestRendererClassification(unittest.TestCase):
    def test_host_gpu(self):
        self.assertEqual(PERF.classify_renderer(
            "GLES: NVIDIA GeForce RTX 3060/PCIe/SSE2"), "host")
        self.assertEqual(PERF.classify_renderer(" Mesa Intel(R) UHD "), "host")

    def test_software(self):
        self.assertEqual(PERF.classify_renderer("SwiftShader Device"),
                         "swiftshader")
        self.assertEqual(PERF.classify_renderer("llvmpipe (LLVM 15)"),
                         "lavapipe")
        self.assertEqual(PERF.classify_renderer("ANGLE (Google, Vulkan)"),
                         "angle")

    def test_unknown(self):
        self.assertEqual(PERF.classify_renderer(""), "unknown")
        self.assertEqual(PERF.classify_renderer(None), "unknown")


class TestApiLevel(unittest.TestCase):
    def test_from_sysdir(self):
        self.assertEqual(
            PERF.api_level({"image.sysdir.1":
                            "system-images/android-37/google_apis_playstore/x86_64/"}),
            37)

    def test_missing(self):
        self.assertEqual(PERF.api_level({}), 0)


class TestRecommend(unittest.TestCase):
    def test_suggests_four_cores_on_a_big_host(self):
        recs = PERF.recommend({"cpu_count": 16, "mem": {"total_mb": 32768},
                               "emulator_count": 0},
                              {"hw.cpu.ncore": "8"})
        by_key = {r["key"]: r for r in recs}
        self.assertEqual(by_key["hw.cpu.ncore"]["suggested"], "4")

    def test_respects_api37_ram_floor(self):
        recs = PERF.recommend({"cpu_count": 8, "mem": {"total_mb": 8192},
                               "emulator_count": 0},
                              {"hw.ramSize": "1536M",
                               "image.sysdir.1": "system-images/android-37/x/"})
        by_key = {r["key"]: r for r in recs}
        self.assertEqual(int(by_key["hw.ramSize"]["suggested"]), 4096)

    def test_nothing_to_do_when_optimal(self):
        recs = PERF.recommend(
            {"cpu_count": 8, "mem": {"total_mb": 16384}, "emulator_count": 0},
            {"hw.cpu.ncore": "4", "hw.ramSize": "4096", "hw.gpu.enabled": "yes",
             "hw.gpu.mode": "auto", "hw.keyboard": "yes",
             "fastboot.forceColdBoot": "no", "vm.heapSize": "576"})
        self.assertEqual(recs, [])

    def test_flags_software_gpu(self):
        recs = PERF.recommend({"cpu_count": 8, "mem": {"total_mb": 16384},
                               "emulator_count": 0},
                              {"hw.gpu.mode": "swiftshader_indirect"})
        by_key = {r["key"]: r for r in recs}
        self.assertEqual(by_key["hw.gpu.mode"]["suggested"], "auto")


class TestBottlenecks(unittest.TestCase):
    def test_names_software_renderer_first(self):
        sample = {"renderer": "swiftshader", "software_render": True,
                  "janky_pct": 40.0, "guest": {}}
        causes = PERF.bottlenecks(sample, {"cpu_pct": 20, "mem": {"pct": 40}})
        self.assertEqual(causes[0]["id"], "software_render")
        self.assertEqual(causes[0]["severity"], "high")

    def test_names_host_cpu(self):
        causes = PERF.bottlenecks({"renderer": "host", "janky_pct": 5,
                                   "guest": {}},
                                  {"cpu_pct": 97, "mem": {"pct": 50}})
        self.assertIn("host_cpu", [c["id"] for c in causes])

    def test_clean_sample_has_no_causes(self):
        causes = PERF.bottlenecks({"renderer": "host", "janky_pct": 1.0,
                                   "guest": {"mem": {"pct": 40},
                                             "load": {"load1": 0.5}}},
                                  {"cpu_pct": 30, "mem": {"pct": 50}})
        self.assertEqual(causes, [])

    def test_every_cause_has_a_fix(self):
        causes = PERF.bottlenecks({"renderer": "lavapipe", "janky_pct": 60,
                                   "guest": {"mem": {"pct": 95},
                                             "load": {"load1": 8.0}}},
                                  {"cpu_pct": 99, "mem": {"pct": 96}})
        self.assertGreater(len(causes), 1)
        for c in causes:
            self.assertTrue(c["what"] and c["why"] and c["fix"])


class TestRuntimeFlags(unittest.TestCase):
    def test_software_render_gets_gpu_host_and_no_snapshot(self):
        flags = PERF.runtime_flags({"renderer": "swiftshader",
                                    "software_render": True},
                                   {"cpu_count": 8, "mem": {"total_mb": 16384}})
        self.assertIn("-gpu host", flags)
        self.assertIn("-no-snapshot", flags)

    def test_caps_cores_at_four(self):
        flags = PERF.runtime_flags({}, {"cpu_count": 64,
                                        "mem": {"total_mb": 65536}})
        cores = [f for f in flags if f.startswith("-cores")]
        self.assertEqual(cores, ["-cores 4"])


class TestHistory(unittest.TestCase):
    def test_starts_empty_and_records(self):
        PERF._HISTORY.clear()
        self.assertEqual(PERF.history()["count"], 0)
        PERF._HISTORY.append({"t": 1, "serial": "s", "score": 80.0,
                              "janky_pct": 2.0, "p95_ms": 12,
                              "renderer": "host"})
        PERF._HISTORY.append({"t": 2, "serial": "s", "score": 60.0,
                              "janky_pct": 9.0, "p95_ms": 25,
                              "renderer": "host"})
        h = PERF.history()
        self.assertEqual(h["count"], 2)
        self.assertEqual(h["avg"], 70.0)
        self.assertEqual(h["min"], 60.0)
        self.assertEqual(h["max"], 80.0)
        PERF._HISTORY.clear()


class TestAutoTune(unittest.TestCase):
    def test_unknown_avd_errors_cleanly(self):
        r = PERF.autotune(mock.Mock(), "definitely_not_an_avd")
        self.assertFalse(r["ok"])
        self.assertIn("not found", r["err"])

    def test_rejects_unsafe_name(self):
        r = PERF.autotune(mock.Mock(), "../../evil")
        self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main()
