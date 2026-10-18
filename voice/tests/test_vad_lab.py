#!/usr/bin/env python3
"""voice/vad_lab.py: the device's end-of-speech rule replayed on block loudness, and kept in step with the firmware."""
import os, re, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import vad_lab as V  # noqa: E402

FIRMWARE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "sketches", "14-daijin", "14-daijin.ino")

def blocks(*parts):
    """(초, RMS) 조각들 → 32ms 블록 RMS 목록."""
    out = []
    for sec, level in parts:
        out += [float(level)] * round(sec * 1000 / V.BLOCK_MS)
    return out

def cut_s(rms, variant):
    stop = V.simulate(rms, **V.VARIANTS[variant])
    return None if stop is None else (stop + 1) * V.BLOCK_MS / 1000


class TestEndOfSpeech(unittest.TestCase):
    def test_a_thinking_pause_mid_sentence_is_not_the_end(self):
        # "그렇지." … 1.4 s … "왜냐하면 …" (the longest pause in the calibration recordings)
        rms = blocks((0.5, 20), (0.6, 600), (1.4, 20), (2.0, 600), (2.5, 20))
        self.assertLess(cut_s(rms, "vad1"), 2.5)                  # the old rule ended inside the pause
        self.assertAlmostEqual(cut_s(rms, "vad2"), 4.5 + 1.5, delta=0.05)   # the new one 1.5 s after the last word

    def test_soft_speech_is_speech_not_silence(self):
        # a sentence that trails off softly (RMS 200, under the old 240 floor for silence)
        rms = blocks((0.3, 20), (1.0, 600), (0.9, 200), (2.0, 20))
        self.assertLess(cut_s(rms, "vad1"), 2.1)                  # counted the soft ending as quiet
        self.assertAlmostEqual(cut_s(rms, "vad2"), 2.2 + 1.5, delta=0.05)

    def test_breath_between_quiet_runs_restarts_the_count(self):
        # quiet, a soft breath (RMS 110), quiet again: only an unbroken quiet run ends the turn
        rms = blocks((0.3, 20), (1.0, 600), (0.9, 20), (0.1, 110), (0.9, 20), (1.0, 600), (2.0, 20))
        self.assertAlmostEqual(cut_s(rms, "vad2"), 4.2 + 1.5, delta=0.05)

    def test_the_floor_is_not_dragged_up_by_long_speech(self):
        rms = blocks((0.3, 20), *[(0.2, 700), (0.2, 260)] * 15, (2.0, 20))
        trace = []
        V.simulate(rms, **V.VARIANTS["vad2"], trace=trace)
        self.assertLess(max(t[3] for t in trace if t[6] >= 300), 40)      # stays near the room's quiet level
        trace = []
        V.simulate(rms, **V.VARIANTS["vad1"], trace=trace)
        self.assertGreater(max(t[3] for t in trace if t[6] >= 300), 100)  # the old rule followed the speech up

    def test_nothing_ends_before_one_second_or_without_speech(self):
        self.assertIsNone(cut_s(blocks((3.0, 20)), "vad2"))
        self.assertIsNone(cut_s(blocks((0.2, 600), (3.0, 20)), "vad2"))   # 200 ms of sound is not a turn


class TestMatchesFirmware(unittest.TestCase):
    def test_default_variant_is_what_the_firmware_does(self):
        src = open(FIRMWARE, encoding="utf-8").read()
        num = lambda pat: float(re.search(pat, src).group(1))
        v = V.VARIANTS[V.DEFAULT]
        self.assertEqual(num(r"#define VAD_SILENCE_MS (\d+)"), v["silence_ms"])
        self.assertEqual(num(r"VAD_SPEECH_MS = (\d+)"), v["speech_ms"])
        self.assertEqual(num(r"VAD_ON_MIN\s*=\s*([\d.]+)f"), v["on_min"])
        self.assertEqual(num(r"VAD_OFF_MIN\s*=\s*([\d.]+)f"), v["off_min"])
        self.assertEqual(num(r"onTh\s*=\s*fmaxf\(VAD_ON_MIN, vadFloor \* ([\d.]+)f\)"), v["on_mult"])
        self.assertEqual(num(r"offTh = fmaxf\(VAD_OFF_MIN, vadFloor \* ([\d.]+)f\)"), v["off_mult"])
        self.assertEqual(v["consecutive"], "else vadSilenceMs = 0;" in src)
        self.assertEqual(v["floor_from"] == "below_off", "else if (rms < offTh) vadFloor +=" in src)


if __name__ == "__main__":
    unittest.main()
