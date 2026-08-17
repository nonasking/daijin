#!/usr/bin/env python3
"""Unit tests for the daijin brain's pure-Python logic.

Covers:
  * ``AdpcmEnc`` — the streaming IMA ADPCM encoder that feeds ``daijin/audio/out``.
    Verified against a reference IMA ADPCM decoder written below, which mirrors the
    C decoder actually running on the device (``sketches/14-daijin/14-daijin.ino``,
    ``adpcmNibble`` / ``onMqtt``). The encoder is only ever correct *as a pair* with
    that decoder, so the pair is what we test — including the wire detail that the
    low nibble of each byte is the earlier sample.
  * ``_SENT_END`` — the sentence-boundary regex driving the streaming TTS pipeline.

No network, no MQTT broker, no audio hardware, no secrets are touched.

Run:  python3 -m unittest discover -s voice/tests   (from the repo root)
"""

import importlib.util
import math
import os
import sys
import types
import unittest

# ---------------------------------------------------------------------------
# Import safety.
#
# daijin_mqtt.py is a script, not a library: at module level it constructs an MQTT
# client, reads secrets.local.txt for the broker credentials, connects to HiveMQ and
# finally calls loop_forever(). A plain `import daijin_mqtt` would therefore never
# return. Rather than restructure the production file (a __main__ guard would be the
# right fix, but that is a separate change), we load it defensively:
#
#   1. stub out `paho.mqtt.client` so the top-level import resolves with no paho
#      installed and no client object can reach a socket;
#   2. execute only the source *above* the first side-effecting top-level statement.
#      Everything above that line is imports, constants, regexes, classes and defs.
#
# The tests still exercise the real production source — nothing is copied.
# ---------------------------------------------------------------------------

SIDE_EFFECT_SENTINEL = "c = mqtt.Client("     # first top-level statement that acts
MODULE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir,
                           "daijin_mqtt.py")


def _install_paho_stub():
    """Minimal stand-in for paho.mqtt.client — importable, inert."""
    if "paho.mqtt.client" in sys.modules:
        return
    paho = types.ModuleType("paho")
    mqtt_pkg = types.ModuleType("paho.mqtt")
    client_mod = types.ModuleType("paho.mqtt.client")

    class _Client:                                    # never instantiated by the tests
        def __init__(self, *a, **kw):
            raise AssertionError("MQTT client must not be constructed in tests")

    class _CallbackAPIVersion:
        VERSION2 = 2

    client_mod.Client = _Client
    client_mod.CallbackAPIVersion = _CallbackAPIVersion
    paho.mqtt = mqtt_pkg
    mqtt_pkg.client = client_mod
    sys.modules["paho"] = paho
    sys.modules["paho.mqtt"] = mqtt_pkg
    sys.modules["paho.mqtt.client"] = client_mod


def _load_daijin_mqtt():
    _install_paho_stub()
    with open(MODULE_PATH, encoding="utf-8") as f:
        lines = f.readlines()
    cut = next((i for i, ln in enumerate(lines) if ln.startswith(SIDE_EFFECT_SENTINEL)),
               None)
    if cut is None:
        raise AssertionError(
            "daijin_mqtt.py no longer starts its side effects with "
            f"{SIDE_EFFECT_SENTINEL!r}; update SIDE_EFFECT_SENTINEL in this test.")
    spec = importlib.util.spec_from_file_location("daijin_mqtt_under_test", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    exec(compile("".join(lines[:cut]), MODULE_PATH, "exec"), mod.__dict__)
    return mod


dm = _load_daijin_mqtt()


# ---------------------------------------------------------------------------
# Reference IMA ADPCM decoder — a transcription of the device's C decoder
# (14-daijin.ino: ADPCM_STEP / ADPCM_IDX / adpcmNibble / the onMqtt unpack loop).
# Written independently of the encoder so that a shared bug cannot hide.
# ---------------------------------------------------------------------------

REF_STEP = [
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230,
    253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963,
    1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327,
    3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442,
    11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794,
    32767]
REF_IDX = [-1, -1, -1, -1, 2, 4, 6, 8]


class RefAdpcmDec:
    """Mirrors adpcmNibble(): int32 predictor, clamped to int16, index clamped 0..88."""

    def __init__(self):
        self.pred = 0
        self.idx = 0

    def nibble(self, code):
        step = REF_STEP[self.idx]
        vp = step >> 3
        if code & 4:
            vp += step
        if code & 2:
            vp += step >> 1
        if code & 1:
            vp += step >> 2
        self.pred += -vp if (code & 8) else vp
        self.pred = max(-32768, min(32767, self.pred))
        self.idx = max(0, min(88, self.idx + REF_IDX[code & 7]))
        return self.pred

    def decode(self, data):
        """Bytes -> list of int16 samples. Low nibble first, exactly like onMqtt()."""
        out = []
        for byte in data:
            out.append(self.nibble(byte & 0x0F))
            out.append(self.nibble(byte >> 4))
        return out


# ---------------------------------------------------------------------------
# Signal helpers
# ---------------------------------------------------------------------------

def pcm_bytes(samples):
    out = bytearray()
    for s in samples:
        s = max(-32768, min(32767, int(s)))
        out += (s & 0xFFFF).to_bytes(2, "little")
    return bytes(out)


def sine(n, freq=440.0, rate=16000, amp=8000):
    return [int(amp * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)]


def rms(xs):
    if not xs:
        return 0.0
    return math.sqrt(sum(float(x) * x for x in xs) / len(xs))


def snr_db(orig, recon):
    err = [a - b for a, b in zip(orig, recon)]
    e = rms(err)
    if e == 0.0:
        return float("inf")
    return 20.0 * math.log10(rms(orig) / e)


def encode_all(enc, pcm):
    """encode() + flush(), i.e. one complete clip."""
    return enc.encode(pcm) + enc.flush()


class AdpcmRoundTripTest(unittest.TestCase):
    """Encoder ⇄ reference decoder, verified as a pair."""

    def test_tables_match_device_decoder(self):
        # A drift between the brain's tables and the sketch's tables would silently
        # turn every reply into noise; pin them together.
        self.assertEqual(list(dm._STEP), REF_STEP)
        self.assertEqual(list(dm._IDX), REF_IDX)
        self.assertEqual(len(REF_STEP), 89)

    def test_compression_ratio_is_4_to_1(self):
        pcm = pcm_bytes(sine(4096))
        data = encode_all(dm.AdpcmEnc(), pcm)
        self.assertEqual(len(data), len(pcm) // 4)   # 16 bits -> 4 bits per sample

    def test_nibble_packing_low_nibble_is_the_earlier_sample(self):
        # The device does `nibble(b & 0x0F)` then `nibble(b >> 4)`. If the encoder ever
        # packed high-first, audio would still "decode" but come out as garbage.
        enc = dm.AdpcmEnc()
        data = encode_all(enc, pcm_bytes([2000, -2000]))
        self.assertEqual(len(data), 1)
        lo, hi = data[0] & 0x0F, data[0] >> 4
        self.assertEqual(lo & 8, 0, "first sample is positive -> sign bit clear")
        self.assertEqual(hi & 8, 8, "second sample steps down -> sign bit set")

    def test_sine_round_trip_error_is_bounded(self):
        orig = sine(2000, freq=440.0)
        recon = RefAdpcmDec().decode(encode_all(dm.AdpcmEnc(), pcm_bytes(orig)))
        self.assertEqual(len(recon), len(orig))
        # 4:1 IMA ADPCM on a clean tone: >20 dB SNR is the accepted floor.
        self.assertGreater(snr_db(orig, recon), 20.0)

    def test_multi_tone_round_trip_error_is_bounded(self):
        # Speech-ish: a couple of harmonics, so the predictor cannot merely coast.
        orig = [max(-32768, min(32767, a + b)) for a, b in
                zip(sine(3000, 300.0, amp=6000), sine(3000, 1700.0, amp=3000))]
        recon = RefAdpcmDec().decode(encode_all(dm.AdpcmEnc(), pcm_bytes(orig)))
        self.assertGreater(snr_db(orig, recon), 18.0)

    def test_silence_decodes_to_exact_silence(self):
        # A zero run must not drift the predictor — otherwise the inter-sentence
        # 0.1 s gap that publish_pcm_stream inserts would be audible hiss.
        pcm = pcm_bytes([0] * 1024)
        recon = RefAdpcmDec().decode(encode_all(dm.AdpcmEnc(), pcm))
        self.assertEqual(len(recon), 1024)
        self.assertEqual(max(abs(s) for s in recon), 0)

    def test_step_edge_converges(self):
        # A hard edge is the worst case for a slew-limited codec: it cannot track the
        # jump instantly, but the step index must grow fast enough to catch up.
        orig = [0] * 64 + [12000] * 192 + [-12000] * 192
        recon = RefAdpcmDec().decode(encode_all(dm.AdpcmEnc(), pcm_bytes(orig)))
        self.assertEqual(len(recon), len(orig))
        # Within 32 samples (2 ms at 16 kHz) of each edge it must be close again...
        self.assertLess(abs(recon[64 + 32] - 12000), 1500)
        self.assertLess(abs(recon[256 + 32] - (-12000)), 1500)
        # ...and the plateaus themselves must settle tightly.
        self.assertLess(max(abs(s - 12000) for s in recon[160:256]), 600)
        self.assertLess(max(abs(s + 12000) for s in recon[352:448]), 600)

    def test_full_scale_does_not_wrap(self):
        # Clamping (not wrapping) at the int16 rails: a wrap would be a loud click.
        orig = [32767] * 256 + [-32768] * 256
        recon = RefAdpcmDec().decode(encode_all(dm.AdpcmEnc(), pcm_bytes(orig)))
        self.assertTrue(all(-32768 <= s <= 32767 for s in recon))
        self.assertGreater(recon[200], 30000)
        self.assertLess(recon[456], -30000)


class AdpcmStreamingStateTest(unittest.TestCase):
    """The encoder is fed TTS fragments as they arrive; state must survive the seams."""

    def test_chunked_encoding_matches_one_shot(self):
        pcm = pcm_bytes(sine(4000, 523.25))
        one_shot = encode_all(dm.AdpcmEnc(), pcm)

        for size in (2, 6, 64, 1000, 4093):     # incl. odd sizes that split a sample
            with self.subTest(chunk=size):
                enc = dm.AdpcmEnc()
                pieces = [enc.encode(pcm[i:i + size]) for i in range(0, len(pcm), size)]
                pieces.append(enc.flush())
                self.assertEqual(b"".join(pieces), one_shot)

    def test_odd_byte_split_is_carried_across_chunks(self):
        # A PCM fragment can end mid-sample; the stray byte must be held, not dropped.
        pcm = pcm_bytes(sine(64, 800.0))
        enc = dm.AdpcmEnc()
        first = enc.encode(pcm[:9])             # 4 whole samples + 1 dangling byte
        self.assertEqual(len(enc.rem), 1)
        rest = enc.encode(pcm[9:]) + enc.flush()
        self.assertEqual(first + rest, encode_all(dm.AdpcmEnc(), pcm))
        self.assertEqual(len(RefAdpcmDec().decode(first + rest)), 64)

    def test_flush_emits_pending_nibble_and_is_idempotent(self):
        enc = dm.AdpcmEnc()
        body = enc.encode(pcm_bytes(sine(5)))   # odd sample count -> half-full byte
        self.assertEqual(len(body), 2)          # 4 samples packed, 1 still pending
        tail = enc.flush()
        self.assertEqual(len(tail), 1)
        self.assertEqual(enc.flush(), b"")      # nothing left to emit
        self.assertEqual(enc.encode(b""), b"")

    def test_no_samples_produces_no_bytes(self):
        enc = dm.AdpcmEnc()
        self.assertEqual(enc.encode(b""), b"")
        self.assertEqual(enc.flush(), b"")

    def test_volume_scales_the_decoded_waveform(self):
        orig = sine(2000, 440.0, amp=16000)
        pcm = pcm_bytes(orig)
        loud = RefAdpcmDec().decode(encode_all(dm.AdpcmEnc(1.0), pcm))
        quiet = RefAdpcmDec().decode(encode_all(dm.AdpcmEnc(0.5), pcm))
        # TTS_VOLUME (default 0.6) exists to keep the small amp out of clipping.
        ratio = rms(quiet) / rms(loud)
        self.assertAlmostEqual(ratio, 0.5, delta=0.03)
        self.assertGreater(snr_db([s * 0.5 for s in orig], quiet), 20.0)

    def test_default_volume_is_transparent(self):
        # vol >= 0.999 skips the scaling loop entirely — no rounding drift.
        pcm = pcm_bytes(sine(512))
        self.assertEqual(encode_all(dm.AdpcmEnc(), pcm),
                         encode_all(dm.AdpcmEnc(1.0), pcm))


class AdpcmResetProtocolTest(unittest.TestCase):
    """seq == 0 resets the device decoder (adPred/adIdx = 0); the brain starts each
    clip with a fresh AdpcmEnc. The two resets must agree, or clip N+1 decodes with
    clip N's predictor and comes out distorted."""

    def test_fresh_encoder_starts_at_decoder_reset_state(self):
        enc = dm.AdpcmEnc()
        dec = RefAdpcmDec()
        self.assertEqual((enc.pred, enc.idx), (0, 0))
        self.assertEqual((dec.pred, dec.idx), (dec.pred, dec.idx))
        self.assertEqual((dec.pred, dec.idx), (0, 0))
        self.assertIsNone(enc.lo)
        self.assertEqual(enc.rem, b"")

    def test_new_clip_with_new_encoder_and_reset_decoder_matches(self):
        # Clip 1 leaves the encoder deep in a loud passage; clip 2 (a new AdpcmEnc, as
        # publish_pcm_stream builds per clip) must still decode correctly on a device
        # that reset its state at seq == 0.
        loud = pcm_bytes(sine(2000, 220.0, amp=30000))
        enc1 = dm.AdpcmEnc()
        encode_all(enc1, loud)
        self.assertNotEqual((enc1.pred, enc1.idx), (0, 0))   # state really did move

        quiet = sine(1000, 660.0, amp=4000)
        clip2 = encode_all(dm.AdpcmEnc(), pcm_bytes(quiet))  # fresh encoder per clip
        recon = RefAdpcmDec().decode(clip2)                  # decoder reset at seq 0
        self.assertGreater(snr_db(quiet, recon), 20.0)

    def test_stale_decoder_state_degrades_audio(self):
        # Negative control: without the seq==0 reset the same bytes decode badly.
        # This is what the test above is protecting against.
        quiet = sine(1000, 660.0, amp=4000)
        clip2 = encode_all(dm.AdpcmEnc(), pcm_bytes(quiet))
        stale = RefAdpcmDec()
        stale.pred, stale.idx = 20000, 60      # leftover state from a loud clip
        self.assertLess(snr_db(quiet, stale.decode(clip2)), 20.0)

    def test_encoder_reuse_across_clips_would_desync(self):
        # Documents *why* publish_pcm_stream must build a new AdpcmEnc per clip.
        pcm = pcm_bytes(sine(800, 440.0, amp=9000))
        shared = dm.AdpcmEnc()
        encode_all(shared, pcm_bytes(sine(800, 100.0, amp=30000)))
        second_clip = encode_all(shared, pcm)                  # reused encoder (wrong)
        fresh_clip = encode_all(dm.AdpcmEnc(), pcm)            # what the code does
        self.assertNotEqual(second_clip, fresh_clip)


# ---------------------------------------------------------------------------
# Sentence splitting (brain_stream)
# ---------------------------------------------------------------------------

def split_stream(deltas, sent_end=dm._SENT_END, emoji=dm.EMOJI):
    """Mirror of the buffering loop inside brain_stream().

    brain_stream() interleaves this logic with a `claude` subprocess and its
    stream-json parsing, so the loop cannot be called directly. The regexes under
    test (_SENT_END, EMOJI) are the production ones; only the surrounding buffering
    is restated here, byte for byte, so a change to the regex is caught.
    """
    out, buf = [], ""
    for delta in deltas:
        buf += delta
        while True:
            m = sent_end.search(buf)
            if not m:
                break
            sent = emoji.sub("", buf[:m.end()]).strip()
            buf = buf[m.end():]
            if sent:
                out.append(sent)
    rest = emoji.sub("", buf).strip()
    if rest:
        out.append(rest)
    return out


class SentenceSplitTest(unittest.TestCase):

    def test_splits_on_terminal_punctuation(self):
        self.assertEqual(split_stream(["안녕! 잘 지냈어? 나는 좋아."]),
                         ["안녕!", "잘 지냈어?", "나는 좋아."])

    def test_split_is_independent_of_delta_boundaries(self):
        # Token deltas arrive at arbitrary offsets — even mid-punctuation.
        text = "첫 문장이야. 두 번째 문장! 세 번째는 여기서 끝?"
        expected = ["첫 문장이야.", "두 번째 문장!", "세 번째는 여기서 끝?"]
        for size in (1, 2, 3, 7, 100):
            with self.subTest(delta=size):
                deltas = [text[i:i + size] for i in range(0, len(text), size)]
                self.assertEqual(split_stream(deltas), expected)

    def test_trailing_fragment_is_flushed(self):
        # The tail has no terminator; brain_stream must still speak it.
        self.assertEqual(split_stream(["끝났어. 근데 이건 마침표가 없어"]),
                         ["끝났어.", "근데 이건 마침표가 없어"])

    def test_ellipsis_and_repeated_marks_are_one_boundary(self):
        self.assertEqual(split_stream(["음... 그래?! 좋아"]),
                         ["음...", "그래?!", "좋아"])

    def test_closing_quote_or_bracket_stays_with_its_sentence(self):
        self.assertEqual(split_stream(['그가 "안녕." 이라고 했어.']),
                         ['그가 "안녕."', "이라고 했어."])
        self.assertEqual(split_stream(["(끝이야.) 다음."]), ["(끝이야.)", "다음."])

    def test_emoji_and_markdown_are_stripped(self):
        # They would otherwise be read aloud by the TTS engine.
        self.assertEqual(split_stream(["좋아 😊 정말!"]), ["좋아  정말!"])
        self.assertEqual(split_stream(["**굵게** 말해."]), ["굵게 말해."])

    def test_whitespace_only_chunk_is_not_yielded(self):
        # A delta carrying only spaces must not produce an empty TTS request; it is
        # held in the buffer and prepended to the next real sentence.
        self.assertEqual(split_stream(["안녕. ", "   ", "끝."]), ["안녕.", "끝."])

    def test_no_output_for_empty_stream(self):
        self.assertEqual(split_stream([]), [])
        self.assertEqual(split_stream(["", "  "]), [])

    def test_decimal_point_splits_a_sentence_known_limitation(self):
        # Documented, deliberate: the regex is punctuation-only, so "3.14" becomes two
        # fragments. Harmless for TTS (it is spoken as a pause) and the system prompt
        # asks for plain conversational Korean. Pinned so the behaviour is a choice,
        # not a surprise.
        self.assertEqual(split_stream(["원주율은 3.14 정도야."]),
                         ["원주율은 3.", "14 정도야."])


if __name__ == "__main__":
    unittest.main(verbosity=2)
