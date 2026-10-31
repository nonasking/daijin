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

import json
import math
import os
import pathlib
import sys
import time
import types
import unittest

# ---------------------------------------------------------------------------
# Import.
#
# daijin_mqtt.py only connects to the broker from main(), so it imports like any module.
# paho is stubbed when it is not installed, so the tests run on a bare Python.
# ---------------------------------------------------------------------------

VOICE_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))


def _install_paho_stub():
    """Minimal stand-in for paho.mqtt.client — importable, inert."""
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


try:
    import paho.mqtt.client  # noqa: F401
except ImportError:
    _install_paho_stub()

sys.path.insert(0, VOICE_DIR)
import selfdb  # noqa: E402
selfdb.use(os.path.join(__import__('tempfile').mkdtemp(), 'test.db'))   # never touch voice/daijin.db from tests
import daijin_mqtt as dm  # noqa: E402
import fleet_watch as fw  # noqa: E402


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

    def test_decimal_point_does_not_split_a_sentence(self):
        # Fixed 2026-09-17: a period only ends a sentence when whitespace follows it.
        # "36.1도야" used to be spoken as "36." then "1도야".
        self.assertEqual(split_stream(["원주율은 3.14 정도야."]), ["원주율은 3.14 정도야."])
        self.assertEqual(split_stream(["몸 온도는 36.1도야. 쾌적해."]), ["몸 온도는 36.1도야.", "쾌적해."])
        for size in (1, 2, 5):
            deltas = ["칩 온도 45.3도, 실내 24.0도야. 괜찮아."[k:k + size]
                      for k in range(0, len("칩 온도 45.3도, 실내 24.0도야. 괜찮아."), size)]
            with self.subTest(delta=size):
                self.assertEqual(split_stream(deltas), ["칩 온도 45.3도, 실내 24.0도야.", "괜찮아."])


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ---------------------------------------------------------------------------
# Node names: spoken 레드/블루 (red/blue) ↔ internal 1번/2번
# ---------------------------------------------------------------------------

class NodeNameTest(unittest.TestCase):

    def test_spoken_names_normalize_to_internal_ids(self):
        cases = {
            "레드 돌리고 블루 돌려 줘": "1번 돌리고 2번 돌려 줘",
            "Red 모터 돌려": "1번 모터 돌려",
            "blue 간식 줘": "2번 간식 줘",
            "블루 먼저, 그 다음에 레드": "2번 먼저, 그 다음에 1번",
            "일 번이랑 이 번": "1번이랑 2번",       # older spoken form still accepted
            "dev1이랑 두 번": "1번이랑 2번",
        }
        for src, want in cases.items():
            with self.subTest(src=src):
                self.assertEqual(dm.normalize_nodes(src), want)

    def test_korean_color_words_are_led_commands_not_node_names(self):
        self.assertEqual(dm.normalize_nodes("빨간 불 켜 줘"), "빨간 불 켜 줘")
        self.assertEqual(dm.normalize_nodes("파랑으로 바꿔"), "파랑으로 바꿔")
        self.assertEqual(dm.normalize_nodes("한 번 더"), "한 번 더")
        self.assertEqual(dm.normalize_nodes("12번 버스"), "12번 버스")

    def test_speech_uses_color_names_with_correct_particles(self):
        cases = {
            "1번이랑 2번 둘 다 온라인이야.": "레드랑 블루 둘 다 온라인이야.",
            "1번은 온라인, 2번을 돌릴게.": "레드는 온라인, 블루를 돌릴게.",
            "dev2가 응답이 없어.": "블루가 응답이 없어.",
            "2번(dev2)이 돌아왔어.": "블루가 돌아왔어.",
            "10번 반복했어.": "10번 반복했어.",          # other numbers untouched
            "1번 모터 명령 보냈고 ack 받았어.": "레드 모터 명령 보냈어.",
        }
        for src, want in cases.items():
            with self.subTest(src=src):
                self.assertEqual(dm.speak_fix(src), want)

    def test_hallucination_list_follows_the_stt_prompt(self):
        # _PROMPT_SENTS must be derived from STT_PROMPT (it used to drift).
        for sent in dm._PROMPT_SENTS:
            self.assertIn(sent, dm.normalize_nodes(dm.STT_PROMPT))


class TestPortableBackends(unittest.TestCase):
    """Linux-portability seams: STT engine selection and the stdlib multipart body sent to Groq.
    sec() is swapped for a dict so no secrets file is read."""

    def _with_secrets(self, d):
        orig = dm.sec
        dm.sec = lambda k: d.get(k, "")
        self.addCleanup(setattr, dm, "sec", orig)

    def test_stt_engine_defaults_to_groq_only_when_a_key_exists(self):
        orig = dm._whisper_server_up
        dm._whisper_server_up = lambda: False
        self.addCleanup(setattr, dm, "_whisper_server_up", orig)
        self._with_secrets({})
        self.assertEqual(dm._stt_engine(), "whisper")
        dm._whisper_server_up = lambda: True
        self.assertEqual(dm._stt_engine(), "whisper_server")                     # a running local server wins
        self._with_secrets({"STT_ENGINE": "groq", "GROQ_API_KEY": "gsk_x"})
        self.assertEqual(dm._stt_engine(), "groq")                               # unless the choice is explicit
        dm._whisper_server_up = lambda: False
        self._with_secrets({"GROQ_API_KEY": "gsk_x"})
        self.assertEqual(dm._stt_engine(), "groq")
        self._with_secrets({"GROQ_API_KEY": "gsk_x", "STT_ENGINE": "whisper"})   # explicit wins
        self.assertEqual(dm._stt_engine(), "whisper")

    def test_multipart_body_is_parseable_and_carries_the_vocabulary_prompt(self):
        import email.parser, email.policy
        body, ctype = dm._multipart({"model": "whisper-large-v3-turbo", "prompt": "레드, 블루"},
                                    "clip.wav", b"RIFF\x00\x01\xff")
        self.assertTrue(ctype.startswith("multipart/form-data; boundary="))
        msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(
            f"Content-Type: {ctype}\r\n\r\n".encode() + body)
        parts = {p.get_param("name", header="content-disposition"): p for p in msg.iter_parts()}
        self.assertEqual(set(parts), {"model", "prompt", "file"})
        # form fields carry no charset header (HTTP multipart convention: raw UTF-8), so read the bytes
        self.assertEqual(parts["prompt"].get_payload(decode=True).decode("utf-8"), "레드, 블루")
        self.assertEqual(parts["file"].get_filename(), "clip.wav")
        self.assertEqual(parts["file"].get_content_type(), "audio/wav")
        self.assertEqual(parts["file"].get_payload(decode=True), b"RIFF\x00\x01\xff")

    def test_claude_env_injects_token_from_secrets_without_overriding_the_shell(self):
        self._with_secrets({"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-secretsfile"})
        prev = os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        self.addCleanup(lambda: os.environ.update({"CLAUDE_CODE_OAUTH_TOKEN": prev}) if prev else None)
        self.assertEqual(dm._claude_env()["CLAUDE_CODE_OAUTH_TOKEN"], "sk-ant-oat01-secretsfile")
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "from-shell"
        self.addCleanup(os.environ.pop, "CLAUDE_CODE_OAUTH_TOKEN", None)
        self.assertEqual(dm._claude_env()["CLAUDE_CODE_OAUTH_TOKEN"], "from-shell")
        self._with_secrets({})
        self.assertEqual(dm._claude_env()["CLAUDE_CODE_OAUTH_TOKEN"], "from-shell")


class TestLocalHooks(unittest.TestCase):
    """The optional voice/local_hooks.py extension point. The file itself is not part of the repo,
    so the tests plug in a stand-in module."""

    def _with_hooks(self, hooks):
        orig = dm.HOOKS
        dm.HOOKS = hooks
        self.addCleanup(setattr, dm, "HOOKS", orig)

    def test_without_hooks_everything_goes_to_the_agent(self):
        self._with_hooks(None)
        self.assertFalse(dm.intercepted(None, "안녕", "voice", 0.0))

    def test_intercept_can_answer_by_itself_through_ctx_say(self):
        spoken = []
        orig = dm.speak_stream
        dm.speak_stream = lambda client, it, t0: spoken.extend(it)
        self.addCleanup(setattr, dm, "speak_stream", orig)

        def intercept(text, kind, ctx):
            if kind == "voice" and "불 꺼" in text:
                ctx.say("껐어.")
                return True
            return False
        self._with_hooks(types.SimpleNamespace(intercept=intercept))
        self.assertTrue(dm.intercepted(None, "거실 불 꺼 줘", "voice", 0.0))
        self.assertEqual(spoken, ["껐어."])
        self.assertFalse(dm.intercepted(None, "오늘 날씨 어때", "voice", 0.0))
        self.assertFalse(dm.intercepted(None, "거실 불 꺼 줘", "ask", 0.0))

    def test_a_crashing_hook_falls_through_to_the_agent(self):
        def intercept(text, kind, ctx):
            raise RuntimeError("boom")
        self._with_hooks(types.SimpleNamespace(intercept=intercept))
        self.assertFalse(dm.intercepted(None, "안녕", "voice", 0.0))


FAKE_CLAUDE = r'''#!/usr/bin/env python3
# Stand-in for the claude CLI. FAKE_MODE picks the behaviour; a resumed call is recognised by --resume.
import json, os, sys, time
mode = os.environ.get("FAKE_MODE", "ok")
resumed = "--resume" in sys.argv
resumed_id = sys.argv[sys.argv.index("--resume") + 1] if resumed else None   # like the real CLI, a resumed session keeps its id
def ev(o): print(json.dumps(o), flush=True)
def say(text, sid):
    ev({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}})
    ev({"type": "stream_event", "event": {"type": "content_block_stop"}})
    ev({"type": "result", "is_error": False, "session_id": sid})
if mode == "hang":
    time.sleep(30)
elif mode == "gone" and resumed:
    print("No conversation found with session ID: old-session", file=sys.stderr); sys.exit(1)
elif mode == "transient" and resumed:
    ev({"type": "result", "is_error": True, "result": "API Error: 529 overloaded"}); sys.exit(1)
elif "--input-format" in sys.argv:              # resident: one turn per user message on stdin, same session throughout
    sid = resumed_id or "new-session"
    for line in sys.stdin:
        if not line.strip(): continue
        say("응, 알았어. 다 됐어.", sid)
else:
    say("응, 알았어. 다 됐어.", resumed_id or "new-session")
'''


class TestBrainStream(unittest.TestCase):
    """brain_stream against a fake claude CLI: what gets spoken, and what happens to the pinned session."""

    def setUp(self):
        import tempfile, stat
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d, True)
        fake = os.path.join(d, "claude")
        with open(fake, "w") as f:
            f.write(FAKE_CLAUDE)
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
        self.session = os.path.join(d, "session")
        for name, val in (("CLAUDE", fake), ("SESSION_FILE", self.session), ("sec", lambda k: ""),
                          ("_resume_failures", 0), ("BRAIN_TIMEOUT_S", 20)):
            orig = getattr(dm, name)
            setattr(dm, name, val)
            self.addCleanup(setattr, dm, name, orig)
        self.addCleanup(os.environ.pop, "FAKE_MODE", None)
        self.addCleanup(dm._resident.stop, "test")
        self.secrets = {}
        dm.sec = lambda k: self.secrets.get(k, "")

    def _run(self, mode, session=None):
        os.environ["FAKE_MODE"] = mode
        if session:
            with open(self.session, "w") as f:
                f.write(session)
        return list(dm.brain_stream("안녕"))

    def _saved(self, suffix=""):
        try:
            with open(self.session + suffix) as f:
                return f.read()
        except FileNotFoundError:
            return None

    def test_first_turn_speaks_sentences_and_pins_the_session(self):
        self.assertEqual(self._run("ok"), ["응, 알았어.", "다 됐어."])
        self.assertEqual(self._saved(), "new-session")

    def test_missing_session_starts_a_new_one_and_keeps_the_old_id(self):
        self.assertEqual(self._run("gone", session="old-session"), ["응, 알았어.", "다 됐어."])
        self.assertEqual(self._saved(), "new-session")
        self.assertEqual(self._saved(".prev"), "old-session")

    def test_transient_error_apologises_but_keeps_the_memory(self):
        out = self._run("transient", session="old-session")
        self.assertEqual(out, ["미안, 지금 생각이 잘 안 돼. 다시 말해줄래?"])
        self.assertEqual(self._saved(), "old-session")          # not replaced by a fresh session

    def test_repeated_failures_eventually_give_up_on_the_session(self):
        for _ in range(dm.RESUME_GIVE_UP - 1):
            self._run("transient", session="old-session")
            self.assertEqual(self._saved(), "old-session")
        self.assertEqual(self._run("transient"), ["응, 알았어.", "다 됐어."])
        self.assertEqual(self._saved(), "new-session")
        self.assertEqual(self._saved(".prev"), "old-session")

    def test_a_hung_agent_is_killed_and_says_so(self):
        dm.BRAIN_TIMEOUT_S = 0.5
        import time
        t = time.monotonic()
        self.assertEqual(self._run("hang"), ["미안, 너무 오래 걸려서 멈췄어."])
        self.assertLess(time.monotonic() - t, 10)
        self.assertFalse(dm._resident.alive())                   # killed, not left hanging around

    def test_the_resident_process_serves_turn_after_turn(self):
        self.assertEqual(self._run("ok"), ["응, 알았어.", "다 됐어."])
        pid = dm._resident.p.pid
        self.assertEqual(self._run("ok"), ["응, 알았어.", "다 됐어."])
        self.assertEqual(dm._resident.p.pid, pid)                 # same process
        self.assertEqual(dm._resident.turns, 2)
        self.assertEqual(dm._resident.sid, "new-session")

    def test_a_dead_resident_is_restarted_on_the_saved_session(self):
        self._run("ok")
        dm._resident.p.kill(); dm._resident.p.wait()
        self.assertEqual(self._run("ok"), ["응, 알았어.", "다 됐어."])
        self.assertTrue(dm._resident.alive())
        self.assertEqual(dm._resident.sid, "new-session")

    def test_resident_off_means_a_process_per_turn(self):
        self.secrets["BRAIN_RESIDENT"] = "0"
        self.assertEqual(self._run("ok"), ["응, 알았어.", "다 됐어."])
        self.assertIsNone(dm._resident.p)


class TestTtsFallback(unittest.TestCase):
    """Engine order and fallback in tts_stream, with stand-in engines (no network, no audio tools)."""

    def setUp(self):
        self.calls = []
        def engine(name, chunks=(b"\x01\x00",), fail_after=None):
            def run(text):
                self.calls.append(name)
                for i, c in enumerate(chunks):
                    if fail_after is not None and i == fail_after:
                        raise RuntimeError(f"{name} down")
                    yield c
                if fail_after is not None and fail_after >= len(chunks):
                    raise RuntimeError(f"{name} down")
            return run
        self.engine = engine
        for name, val in (("_TTS_ENGINES", {"edge": engine("edge"), "eleven": engine("eleven"), "yuna": engine("yuna"),
                                            "supertonic": engine("supertonic")}),
                          ("IS_MAC", True), ("sec", lambda k: ""), ("supertonic", None)):
            orig = getattr(dm, name)
            setattr(dm, name, val)
            self.addCleanup(setattr, dm, name, orig)

    def test_order_starts_with_the_configured_engine_and_skips_unusable_ones(self):
        self.assertEqual(dm._tts_order(), ["edge", "yuna"])                       # no ElevenLabs key
        dm.sec = lambda k: {"TTS_ENGINE": "eleven", "ELEVEN_API_KEY": "k"}.get(k, "")
        self.assertEqual(dm._tts_order(), ["eleven", "edge", "yuna"])
        dm.IS_MAC = False
        self.assertEqual(dm._tts_order(), ["eleven", "edge"])                     # no say on Linux
        dm.supertonic = object()                                                  # package installed
        dm.sec = lambda k: {"TTS_ENGINE": "supertonic"}.get(k, "")
        self.assertEqual(dm._tts_order(), ["supertonic", "edge"])
        dm.sec = lambda k: ""
        self.assertEqual(dm._tts_order(), ["edge", "supertonic"])                 # local engine is the first fallback

    def test_a_failing_engine_falls_through_to_the_next(self):
        dm.sec = lambda k: {"TTS_ENGINE": "eleven", "ELEVEN_API_KEY": "k"}.get(k, "")
        dm._TTS_ENGINES["eleven"] = self.engine("eleven", fail_after=0)
        self.assertEqual(list(dm.tts_stream("안녕")), [b"\x01\x00"])
        self.assertEqual(self.calls, ["eleven", "edge"])

    def test_edge_is_retried_once_before_moving_on(self):
        dm._TTS_ENGINES["edge"] = self.engine("edge", fail_after=0)
        self.assertEqual(list(dm.tts_stream("안녕")), [b"\x01\x00"])
        self.assertEqual(self.calls, ["edge", "edge", "yuna"])

    def test_a_sentence_already_half_spoken_is_not_restarted_on_another_engine(self):
        dm._TTS_ENGINES["edge"] = self.engine("edge", chunks=(b"\x01\x00", b"\x02\x00"), fail_after=1)
        self.assertEqual(list(dm.tts_stream("안녕")), [b"\x01\x00"])
        self.assertEqual(self.calls, ["edge"])

    def test_all_engines_down_yields_nothing(self):
        dm.IS_MAC = False
        dm._TTS_ENGINES["edge"] = self.engine("edge", fail_after=0)
        self.assertEqual(list(dm.tts_stream("안녕")), [])


class TestFleetWatch(unittest.TestCase):
    """fleet_watch state transitions: what gets spoken, and what stays quiet."""

    class Client:
        def __init__(self):
            self.said, self.asked = [], []
        def publish(self, topic, payload, *a, **kw):
            if topic == fw.T_SAY: self.said.append(payload)
            if topic == fw.T_ASK: self.asked.append(payload)

    class Msg:
        def __init__(self, node, **status):
            self.topic = f"daijin/dev/{node}/status"
            self.payload = json.dumps(status).encode()

    def setUp(self):
        self.c = self.Client()
        for name, val in (("nodes", {}), ("started", 0.0), ("local", None), ("_last_payload", {}), ("HOOKS", None),
                          ("sec", lambda k: "")):
            orig = getattr(fw, name)
            setattr(fw, name, val)
            self.addCleanup(setattr, fw, name, orig)

    def test_first_sight_of_a_node_is_a_baseline_not_news(self):
        fw.on_message(self.c, None, self.Msg("dev1", online=False))
        self.assertEqual(self.c.said, [])

    def test_going_offline_and_coming_back_are_spoken_once_each_in_plain_words(self):
        fw.on_message(self.c, None, self.Msg("dev1", online=True, ts=0, up_s=100))
        fw.on_message(self.c, None, self.Msg("dev1", online=False))
        self.assertEqual(self.c.said, [])                                   # not yet: it may be a reboot
        fw.pending_check(self.c, now=fw.time.time() + fw.OFF_GRACE_S + 1)
        self.assertEqual(self.c.said, ["레드가 방금 꺼졌어. 전원이나 네트워크를 확인해 줘."])
        fw.pending_check(self.c, now=fw.time.time() + fw.OFF_GRACE_S + 30)   # said once
        fw.on_message(self.c, None, self.Msg("dev1", online=True, ts=0, up_s=3))
        self.assertEqual(self.c.said[-1], "레드가 다시 켜졌어. 방금 재부팅한 것 같아.")
        self.assertEqual(len(self.c.said), 2)
        self.assertEqual(self.c.asked, [])                                  # the agent is not involved

    def test_a_short_outage_is_not_news(self):
        fw.on_message(self.c, None, self.Msg("dev1", online=True, ts=0, up_s=100))
        for _ in range(5):                                                  # unplugged and replugged, flashed, rebooted
            fw.on_message(self.c, None, self.Msg("dev1", online=False))
            fw.pending_check(self.c, now=fw.time.time() + 10)
            fw.on_message(self.c, None, self.Msg("dev1", online=True, ts=0, up_s=4))
        fw.pending_check(self.c, now=fw.time.time() + fw.OFF_GRACE_S + 1)
        self.assertEqual(self.c.said, [])

    def test_a_long_uptime_return_is_not_called_a_reboot(self):
        fw.on_message(self.c, None, self.Msg("dev2", online=False))
        fw.on_message(self.c, None, self.Msg("dev2", online=True, ts=0, up_s=4000))
        self.assertEqual(self.c.said, ["블루가 다시 켜졌어."])

    def test_fleet_report_ask_routes_the_context_to_the_agent_instead(self):
        fw.sec = lambda k: "ask" if k == "FLEET_REPORT" else ""
        fw.on_message(self.c, None, self.Msg("dev1", online=True, ts=0, up_s=100))
        fw.on_message(self.c, None, self.Msg("dev1", online=False))
        fw.pending_check(self.c, now=fw.time.time() + fw.OFF_GRACE_S + 1)
        self.assertEqual(self.c.said, [])
        self.assertEqual(len(self.c.asked), 1)
        self.assertIn("[시스템 알림]", self.c.asked[0])

    def test_heartbeats_while_online_are_not_news(self):
        for up in (10, 40, 70):
            fw.on_message(self.c, None, self.Msg("dev1", online=True, ts=0, up_s=up))
        self.assertEqual(self.c.said, [])

    def test_local_hooks_can_mute_the_reports(self):
        fw.HOOKS = types.SimpleNamespace(mute_reports=lambda: True)
        fw.on_message(self.c, None, self.Msg("dev1", online=True, ts=0, up_s=100))
        fw.on_message(self.c, None, self.Msg("dev1", online=False))
        self.assertEqual(self.c.said, [])

    def test_unknown_node_ids_get_a_particle_too(self):
        self.assertEqual(nodes_mod.subject("dev1"), "레드가")
        self.assertEqual(nodes_mod.subject("그린"), "그린이")
        self.assertEqual(nodes_mod.subject("dev9"), "dev9가")


import nodes as nodes_mod          # noqa: E402
import fleet_status                # noqa: E402
import nodectl                     # noqa: E402


class TestNodeTable(unittest.TestCase):
    """voice/nodes.json is the one place node names live; everything else resolves through it."""

    def test_every_spelling_resolves_to_the_node_id(self):
        for word in ("레드", "red", "RED", "1", "1번", "dev1"):
            self.assertEqual(nodes_mod.resolve(word), "dev1", word)
        for word in ("블루", "Blue", "2", "2번", "dev2"):
            self.assertEqual(nodes_mod.resolve(word), "dev2", word)
        self.assertEqual(nodes_mod.resolve("all"), "all")
        self.assertEqual(nodes_mod.resolve("dev9"), "dev9")             # unknown names pass through

    def test_mentioned_finds_names_aliases_and_numbers_but_not_other_numbers(self):
        self.assertEqual(nodes_mod.mentioned("레드랑 BLUE 둘 다"), {"dev1", "dev2"})
        self.assertEqual(nodes_mod.mentioned("1번 꺼 줘"), {"dev1"})
        self.assertEqual(nodes_mod.mentioned("11번 버스"), set())
        self.assertEqual(nodes_mod.mentioned("1번 레드", numbers=False), {"dev1"})
        self.assertEqual(nodes_mod.mentioned("2번", numbers=False), set())

    def test_mentioned_wants_a_word_not_a_substring(self):
        for text in ("bored 라는 말", "blueberry 먹었어", "1번째 줄", "hatred"):
            with self.subTest(text=text):
                self.assertEqual(nodes_mod.mentioned(text), set())
        self.assertEqual(nodes_mod.mentioned("Red 불"), {"dev1"})                    # a real word, any case
        self.assertEqual(nodes_mod.mentioned("레드말고 블루만"), {"dev1", "dev2"})     # Korean takes particles

    def test_fleet_lines_use_spoken_names_and_judge_by_heartbeat_age(self):
        now = 1_800_000_000
        lines = fleet_status.render([
            ("daijin/dev/dev1/status", '{"online":false,"node":"dev1"}'),
            ("daijin/dev/dev2/status", '{"online":true,"ts":%d,"up_s":50,"rssi":-60,"chip_c":40.0}' % (now - 400)),
            ("daijin/dev/dev2/status", '{"online":true,"ts":%d,"up_s":440,"rssi":-61,"chip_c":41.0}' % (now - 10)),
        ], now=now)
        self.assertEqual(len(lines), 2)                                  # the newer dev2 heartbeat wins
        self.assertTrue(lines[0].startswith("레드: OFFLINE"))
        self.assertTrue(lines[1].startswith("블루: ONLINE"), lines[1])
        stale = fleet_status.describe("dev2", '{"online":true,"ts":%d}' % (now - 200), now=now)
        self.assertIn("STALE(200초 무응답)", stale)


class TestNodeCommand(unittest.TestCase):
    """nodectl.cmd against a stand-in broker: what it prints and returns for each kind of answer."""

    class M:
        def __init__(self, topic, text, retain=False):
            self.topic, self.payload, self.retain = topic, text.encode(), retain

    def _broker(self, status, acks):
        test, M = self, self.M
        class FakeBroker:
            local = True
            def __init__(self):
                self.published, self.inbox = [], []
                test.broker = self
            def subscribe(self, *topics):
                if status is not None:
                    self.inbox.append(M("daijin/dev/dev1/status", status, retain=True))
                self.inbox.append(M("daijin/dev/dev1/state", "pong", retain=True))    # stale ack from an earlier command
            def publish(self, topic, payload):
                self.published.append((topic, payload))
                if topic.endswith("/cmd") and payload in acks:
                    self.inbox.append(M("daijin/dev/dev1/state", acks[payload]))
                return True
            def wait(self, want, timeout):
                while self.inbox:
                    m = self.inbox.pop(0)
                    if want(m):
                        return m
                return None
            def close(self):
                pass
        orig = nodectl.Broker
        nodectl.Broker = FakeBroker
        self.addCleanup(setattr, nodectl, "Broker", orig)

    def _cmd(self, node, command):
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = nodectl.cmd(node, command)
        return rc, buf.getvalue()

    def test_offline_node_is_refused_without_sending(self):
        self._broker('{"online":false,"node":"dev1"}', {})
        rc, out = self._cmd("레드", "motor")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL: dev1 오프라인", out)
        self.assertEqual(self.broker.published, [])

    def test_ack_is_this_commands_answer_not_the_retained_one(self):
        self._broker('{"online":true,"ts":0}', {"led:red": "led:red"})
        rc, out = self._cmd("1번", "led:red")
        self.assertEqual(rc, 0)
        self.assertIn("OK: dev1 <- led:red (ack: led:red)", out)

    def test_unknown_command_reports_the_nodes_capabilities(self):
        self._broker('{"online":true,"ts":0}', {"step:90": "err:unknown:step:90", "caps": "caps:led,ping,motor,servo,poke"})
        rc, out = self._cmd("red", "step:90")
        self.assertEqual(rc, 1)
        self.assertIn("쓸 수 있는 기능: led,ping,motor,servo,poke", out)

    def test_no_answer_is_a_failure(self):
        self._broker('{"online":true,"ts":0}', {})
        rc, out = self._cmd("dev1", "motor")
        self.assertEqual(rc, 1)
        self.assertIn("ack: 없음", out)

    def test_servo_already_there_is_ok_with_a_hint(self):
        self._broker(None, {"servo:90": "servo:90:nochange"})
        rc, out = self._cmd("dev1", "servo:90")
        self.assertEqual(rc, 0)
        self.assertIn("상태 기록 없음", out)
        self.assertIn("'motor'", out)


class TestDeviceStatus(unittest.TestCase):
    """status.sh output for the main device: offline by LWT, and how old an online report is."""

    def test_lwt_offline_is_a_failure_line(self):
        self.assertTrue(nodectl.device_status_line('{"online":false}').startswith("FAIL: 디바이스 오프라인"))

    def test_online_report_carries_its_age(self):
        import json
        line = nodectl.device_status_line('{"online":true,"ts":1800000000,"temp_c":41.5,"rssi":-58}', now=1800000042)
        d = json.loads(line)
        self.assertEqual((d["age_s"], d["temp_c"], d["rssi"]), (42, 41.5, -58))

    def test_old_firmware_json_passes_through(self):
        import json
        line = nodectl.device_status_line('{"temp_c":40.0,"ssid":"home","rssi":-60,"uptime_s":12,"heap":1000}')
        self.assertEqual(json.loads(line)["temp_c"], 40.0)
        self.assertNotIn("age_s", line)


class TestHallucinationFilter(unittest.TestCase):
    """Whisper echoes prompt sentences on noise, but the prompt sentences are also what people really say."""

    def test_a_prompt_sentence_said_with_confidence_is_kept(self):
        self.assertFalse(dm._looks_hallucinated("전부 꺼 줘.", logprob=-0.05))
        self.assertFalse(dm._looks_hallucinated("응, 알았어", logprob=-0.4))

    def test_a_prompt_sentence_with_low_or_unknown_confidence_is_dropped(self):
        self.assertTrue(dm._looks_hallucinated("전부 꺼 줘.", logprob=-1.0))
        self.assertTrue(dm._looks_hallucinated("전부 꺼 줘."))                     # local whisper: no score

    def test_the_same_sentence_over_and_over_is_dropped_whatever_the_score(self):
        self.assertTrue(dm._looks_hallucinated("감사합니다. 감사합니다. 감사합니다.", logprob=-0.1))

    def test_ordinary_speech_is_never_touched(self):
        self.assertFalse(dm._looks_hallucinated("오늘 날씨 어때?", logprob=-1.2))
        self.assertFalse(dm._looks_hallucinated("오늘 날씨 어때?"))


class TestBrainStatusAndAlerts(unittest.TestCase):
    """What the brain publishes about itself: per-turn timing, status with last will, alerts with a cooldown."""

    class Client:
        def __init__(self):
            self.sent = []
        def publish(self, topic, payload=None, qos=0, retain=False, **kw):
            self.sent.append((topic, payload, retain))

    def setUp(self):
        for name, val in (("_client", None), ("_alerts", {}), ("_turns", 0), ("sec", lambda k: "")):
            orig = getattr(dm, name)
            setattr(dm, name, val)
            self.addCleanup(setattr, dm, name, orig)

    def test_turn_records_stage_marks_in_order_and_publishes_one_line(self):
        c = self.Client()
        t = dm.Turn("voice", 100.0)
        orig = dm.time.monotonic
        dm.time.monotonic = lambda: 100.4
        self.addCleanup(setattr, dm.time, "monotonic", orig)
        t.mark("stt"); dm.time.monotonic = lambda: 102.9; t.mark("first_sent"); t.mark("first_sent")   # second mark keeps the first
        dm.time.monotonic = lambda: 103.5; t.info.update(sentences=2, chunks=9); t.done(c)
        topic, payload, retain = c.sent[-1]
        d = json.loads(payload)
        self.assertEqual(topic, dm.T_BRAIN_TURN)
        self.assertFalse(retain)
        self.assertEqual((d["kind"], d["stt"], d["first_sent"], d["done"], d["sentences"]), ("voice", 0.4, 2.9, 3.5, 2))
        self.assertEqual(dm._turns, 1)

    def test_status_is_retained_and_the_will_says_offline(self):
        on, off = json.loads(dm.brain_status()), json.loads(dm.brain_status(online=False))
        self.assertTrue(on["online"]); self.assertFalse(off["online"])
        self.assertEqual(on["model"], dm.DEFAULT_MODEL)
        c = self.Client(); dm.publish_status(c)
        self.assertEqual((c.sent[0][0], c.sent[0][2]), (dm.T_BRAIN_STATUS, True))

    def test_alerts_are_retained_and_rate_limited_per_kind(self):
        c = self.Client(); dm._client = c
        dm._alert("auth", "first"); dm._alert("auth", "second"); dm._alert("worker", "other")
        kinds = [json.loads(p)["kind"] for t, p, r in c.sent if t == dm.T_BRAIN_ALERT]
        self.assertEqual(kinds, ["auth", "worker"])
        self.assertTrue(all(r for t, p, r in c.sent))
        dm.clear_alert()
        self.assertEqual(c.sent[-1], (dm.T_BRAIN_ALERT, b"", True))

    def test_an_auth_failure_is_spoken_and_does_not_replace_the_session(self):
        import tempfile, stat
        d = tempfile.mkdtemp(); self.addCleanup(__import__("shutil").rmtree, d, True)
        fake = os.path.join(d, "claude")
        with open(fake, "w") as f:
            f.write('#!/usr/bin/env python3\nimport sys\nprint("Error: authentication_error: OAuth token has expired. Please run /login", file=sys.stderr)\nsys.exit(1)\n')
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
        session = os.path.join(d, "session")
        with open(session, "w") as f:
            f.write("old-session")
        for name, val in (("CLAUDE", fake), ("SESSION_FILE", session)):
            orig = getattr(dm, name); setattr(dm, name, val); self.addCleanup(setattr, dm, name, orig)
        c = self.Client(); dm._client = c
        self.assertEqual(list(dm.brain_stream("안녕")), [dm.AUTH_FAIL_SENTENCE])
        self.assertEqual(pathlib.Path(session).read_text(), "old-session")
        self.assertEqual(json.loads(c.sent[-1][1])["kind"], "auth")


class TestTokenExpiry(unittest.TestCase):
    """The subscription token dies silently after a year; from 30 days before, the brain says so once a day."""

    def setUp(self):
        self.secrets = {}
        for name, val in (("_client", None), ("_alerts", {}), ("sec", lambda k: self.secrets.get(k, ""))):
            orig = getattr(dm, name); setattr(dm, name, val); self.addCleanup(setattr, dm, name, orig)
        self.now = time.mktime((2027, 8, 1, 12, 0, 0, 0, 0, -1))

    def test_no_date_means_no_opinion(self):
        self.assertIsNone(dm.token_days_left(self.now))
        self.secrets["CLAUDE_TOKEN_EXPIRES"] = "next september"
        self.assertIsNone(dm.token_days_left(self.now))
        self.assertEqual(dm.token_line(self.now), "")

    def test_far_away_is_quiet_and_near_is_said(self):
        self.secrets["CLAUDE_TOKEN_EXPIRES"] = "2027-09-15"
        self.assertEqual(dm.token_line(self.now), "")                          # 45 days: nothing
        late = self.now + 20 * 86400                                           # 25 days left
        self.assertAlmostEqual(dm.token_days_left(late), 24.5, places=1)
        self.assertIn("24일 뒤 만료돼", dm.token_line(late))
        self.assertIn("setup-token", dm.token_line(late))
        self.assertIn("3일 전에 만료됐어", dm.token_line(self.now + 48 * 86400))

    def test_state_block_carries_the_line_only_when_near(self):
        self.secrets["CLAUDE_TOKEN_EXPIRES"] = "2027-09-15"
        self.assertNotIn("토큰", dm.state_block(self.now))
        self.assertIn("클로드 토큰", dm.state_block(self.now + 20 * 86400))

    def test_alert_once_a_day(self):
        sent = []
        class C:
            def publish(self, topic, payload=None, qos=0, retain=False, **kw): sent.append((topic, json.loads(payload)))
        dm._client = C()
        self.secrets["CLAUDE_TOKEN_EXPIRES"] = "2027-09-15"
        orig = dm.time.time; self.addCleanup(setattr, dm.time, "time", orig)
        t = self.now + 20 * 86400
        dm.time.time = lambda: t
        dm.token_watch(); dm.token_watch()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], dm.T_BRAIN_ALERT)
        self.assertEqual(sent[0][1]["kind"], "token")
        self.assertIn("24일 전", sent[0][1]["text"])
        dm.time.time = lambda: t + 86400 + 1
        dm.token_watch()
        self.assertEqual(len(sent), 2)


class TestClipReassembly(unittest.TestCase):
    """daijin/audio/in chunks → one clip on the worker queue; a missing middle chunk is said out loud, not hidden."""

    class Msg:
        def __init__(self, clip, seq, last, payload=b"\x01\x02"):
            self.topic = dm.T_AUDIO_IN
            self.payload = bytes([clip, (seq >> 8) & 0xFF, seq & 0xFF, 1 if last else 0]) + payload

    def setUp(self):
        for name, val in (("_rx", {"clip": None, "parts": {}, "at": 0}), ("_clipq", dm.queue.Queue())):
            orig = getattr(dm, name); setattr(dm, name, val); self.addCleanup(setattr, dm, name, orig)

    def _feed(self, *msgs):
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            for m in msgs:
                dm.on_message(None, None, m)
        return buf.getvalue()

    def test_whole_clip_is_queued_without_a_gap_warning(self):
        out = self._feed(self.Msg(7, 0, False, b"aa"), self.Msg(7, 1, False, b"bb"), self.Msg(7, 2, True, b"cc"))
        self.assertEqual(dm._clipq.get_nowait(), ("pcm", b"aabbcc"))
        self.assertNotIn("유실", out)

    def test_a_clip_that_never_ends_is_said_so_when_the_next_one_starts_or_time_passes(self):
        out = self._feed(self.Msg(3, 0, False, b"aa"), self.Msg(3, 1, False, b"bb"))     # no last chunk: the device fell off
        self.assertNotIn("미완성", out); self.assertTrue(dm._clipq.empty())
        out = self._feed(self.Msg(4, 0, False, b"cc"))
        self.assertIn("클립 3 미완성: 2청크 받고 다음 클립이 왔다", out)
        dm._rx["at"] = dm.time.time() - 11
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            dm.rx_abandon("11초 조용함")
        self.assertIn("클립 4 미완성: 1청크 받고 11초 조용함", buf.getvalue())
        self.assertIsNone(dm._rx["clip"])

    def test_missing_middle_chunk_is_logged_and_the_rest_still_goes_on(self):
        out = self._feed(self.Msg(9, 0, False, b"aa"), self.Msg(9, 2, False, b"cc"), self.Msg(9, 3, True, b"dd"))
        self.assertEqual(dm._clipq.get_nowait(), ("pcm", b"aaccdd"))
        self.assertIn("클립 9: 청크 1개 유실 (seq 1)", out)
