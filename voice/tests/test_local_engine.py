#!/usr/bin/env python3
"""The local thinking engine, the engine fallback chain and the rule-based command fast path.
No model, no broker: the model endpoint and the tools are stand-ins."""
import json, os, sys, tempfile, types, unittest

VOICE_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
sys.path.insert(0, VOICE_DIR)
try:
    import paho.mqtt.client  # noqa: F401
except ImportError:
    from test_daijin_mqtt import _install_paho_stub
    _install_paho_stub()
import selfdb
selfdb.use(os.path.join(tempfile.mkdtemp(), "t.db"))
import daijin_mqtt as dm
import local_mind, fastpath, reflect


def canned(*messages):
    """local_mind.chat stand-in that returns the given assistant messages in order and records what it was sent."""
    seen = []
    it = iter(messages)
    def chat(msgs, tools=None, **kw):
        seen.append([dict(m) for m in msgs])
        return next(it)
    return chat, seen


def call(name, **args):
    return {"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class LocalCase(unittest.TestCase):
    def patch(self, obj, name, val):
        orig = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(setattr, obj, name, orig)


class TestLocalMind(LocalCase):
    def test_tool_round_then_answer(self):
        chat, seen = canned({"content": "", "tool_calls": [call("node_cmd", node="red", command="motor")]},
                              {"content": "레드 모터 돌렸어."})
        self.patch(local_mind, "chat", chat)
        ran = []
        def run(name, args):
            ran.append((name, args)); return "OK: dev1 <- motor (ack: motor:servo-poke:120)", True
        self.assertEqual(local_mind.think("레드 모터 돌려 줘", run=run), "레드 모터 돌렸어.")
        self.assertEqual(ran, [("node_cmd", {"node": "red", "command": "motor"})])
        self.assertEqual(seen[1][-1]["role"], "tool")                              # the result went back to the model

    def test_claiming_an_action_without_one_is_corrected(self):
        chat, _ = canned({"content": "네, 레드 모터 돌렸어요!"})
        self.patch(local_mind, "chat", chat)
        self.assertEqual(local_mind.think("레드 모터 돌려 줘", run=lambda *a: ("", False)), local_mind.NO_ACTION_REPLY)

    def test_a_failed_action_does_not_license_the_claim(self):
        chat, _ = canned({"content": "", "tool_calls": [call("node_cmd", node="red", command="motor")]},
                           {"content": "레드 모터 돌렸어."})
        self.patch(local_mind, "chat", chat)
        out = local_mind.think("레드 모터 돌려 줘", run=lambda *a: ("FAIL: dev1 오프라인", False))
        self.assertEqual(out, local_mind.NO_ACTION_REPLY)

    def test_invented_commands_are_not_sent(self):
        res, ok = local_mind.run_tool("node_cmd", {"node": "red", "command": "start_motor"})
        self.assertFalse(ok)
        self.assertIn("없는 명령", res); self.assertIn("motor", res)
        self.assertTrue(local_mind.valid_command("led:off")); self.assertFalse(local_mind.valid_command("rm -rf"))

    def test_han_characters_are_dropped(self):
        chat, _ = canned({"content": "体温 41.5도예요."})
        self.patch(local_mind, "chat", chat)
        self.assertEqual(local_mind.think("몸 온도?", run=lambda *a: ("", False)), "41.5도예요.")

    def test_history_and_state_reach_the_model(self):
        chat, seen = canned({"content": "산책 얘기했었지."})
        self.patch(local_mind, "chat", chat)
        local_mind.think("아까 뭐 얘기했지?", system_extra="[지금 상태] 온도 41도", history=[("산책 다녀왔어", "좋았겠다")])
        msgs = seen[0]
        self.assertIn("[지금 상태] 온도 41도", msgs[0]["content"])
        self.assertEqual([m["role"] for m in msgs[1:]], ["user", "assistant", "user"])
        self.assertEqual(msgs[1]["content"], "산책 다녀왔어")

    def test_round_limit(self):
        loops = [{"content": "", "tool_calls": [call("fleet_status")]}] * local_mind.MAX_ROUNDS
        chat, _ = canned(*loops)
        self.patch(local_mind, "chat", chat)
        self.assertIn("너무 길어졌어", local_mind.think("x", run=lambda *a: ("ok", False)))


class TestEngineChain(LocalCase):
    def setUp(self):
        self.cfg = {}
        real = dm.sec
        self.patch(dm, "sec", lambda k: self.cfg.get(k, real(k) if k not in ("BRAIN_ENGINE", "FAST_COMMANDS") else ""))

    def test_engine_list(self):
        self.assertEqual(dm.engines(), ["claude"])
        self.cfg["BRAIN_ENGINE"] = "local"; self.assertEqual(dm.engines(), ["local"])
        self.cfg["BRAIN_ENGINE"] = "claude, local"; self.assertEqual(dm.engines(), ["claude", "local"])
        self.cfg["BRAIN_ENGINE"] = "gpt"; self.assertEqual(dm.engines(), ["claude"])

    def _thinkers(self, claude, local):
        self.patch(dm, "_THINKERS", {"claude": claude, "local": local})

    def test_falls_back_when_claude_says_nothing(self):
        def claude(text, out):
            out["failed"] = "error"; return; yield
        def local(text, out):
            yield "로컬이 답해."
        self._thinkers(claude, local)
        self.cfg["BRAIN_ENGINE"] = "claude,local"
        self.assertEqual(list(dm.brain_stream("안녕")), ["로컬이 답해."])
        self.assertEqual(dm._last_engine["name"], "local")

    def test_no_fallback_once_a_sentence_was_spoken(self):
        def claude(text, out):
            yield "반쯤 말했어."; out["failed"] = "error"
        def local(text, out):
            raise AssertionError("must not run")
        self._thinkers(claude, local)
        self.cfg["BRAIN_ENGINE"] = "claude,local"
        self.assertEqual(list(dm.brain_stream("안녕")), ["반쯤 말했어."])

    def test_last_engine_failure_apologises_by_reason(self):
        def fail(reason):
            def f(text, out):
                out["failed"] = reason; return; yield
            return f
        self._thinkers(fail("auth"), fail("unreachable"))
        self.cfg["BRAIN_ENGINE"] = "claude"
        self.assertEqual(list(dm.brain_stream("x")), [dm.AUTH_FAIL_SENTENCE])
        self.cfg["BRAIN_ENGINE"] = "claude,local"
        self.assertEqual(list(dm.brain_stream("x")), ["미안, 지금 생각이 잘 안 돼. 다시 말해줄래?"])

    def test_local_engine_unreachable_reports_failure(self):
        self.patch(local_mind, "reachable", lambda timeout=0.4: False)
        out = {}
        self.assertEqual(list(dm.local_think("안녕", out)), [])
        self.assertEqual(out["failed"], "unreachable")

    def test_fast_commands_default_follows_the_first_engine(self):
        self.assertFalse(dm.fast_commands_on())
        self.cfg["BRAIN_ENGINE"] = "local,claude"; self.assertTrue(dm.fast_commands_on())
        self.cfg["FAST_COMMANDS"] = "0"; self.assertFalse(dm.fast_commands_on())
        self.cfg["BRAIN_ENGINE"] = "claude"; self.cfg["FAST_COMMANDS"] = "1"; self.assertTrue(dm.fast_commands_on())

    def test_reflect_follows_the_engine_unless_told(self):
        self.cfg["BRAIN_ENGINE"] = "local"
        real = reflect.sec
        self.patch(reflect, "sec", lambda k: "" if k == "REFLECT_ENGINE" else real(k))
        self.assertEqual(reflect.reflect_engine(), "local")
        self.patch(reflect, "sec", lambda k: "claude" if k == "REFLECT_ENGINE" else real(k))
        self.assertEqual(reflect.reflect_engine(), "claude")


class TestFastPath(unittest.TestCase):
    def test_clear_commands(self):
        n = dm.normalize_nodes
        cases = {
            "레드 모터 돌려 줘": ("dev1", "motor"),
            "블루 모터 돌려": ("dev2", "motor"),
            "블루 간식 줘": ("dev2", "motor"),                  # nodes.json phrase
            "불 꺼 줘": ("all", "led:off"),
            "전부 불 꺼": ("all", "led:off"),
            "레드 불 빨간색으로 켜 줘": ("dev1", "led:red"),
            "블루 불 켜": ("dev2", "led:green"),
            "레드 부저 울려": ("dev1", "buzz:ok"),
            "블루 알람 울려 줘": ("dev2", "buzz:alarm"),
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(fastpath.parse(n(text)), want)

    def test_anything_unclear_goes_to_the_model(self):
        n = dm.normalize_nodes
        for text in ("레드 모터 돌려도 돼?", "레드 모터 돌리지 마", "레드랑 블루 모터 돌려", "모터 돌려 줘",
                     "블루 켜져 있어?", "오늘 기분 어때", "레드 간식 줘", "오늘 저녁에 레드 모터를 한 번 돌려 주면 좋겠는데 괜찮을까"):
            with self.subTest(text=text):
                self.assertIsNone(fastpath.parse(n(text)))

    def test_replies_follow_the_actual_result(self):
        self.assertEqual(fastpath.reply_for("dev1", "motor", True), "레드 모터 돌렸어.")
        self.assertEqual(fastpath.reply_for("all", "led:off", True), "다들 불 껐어.")
        self.assertEqual(fastpath.reply_for("dev2", "motor", False, "FAIL: dev2 오프라인"), "블루는 지금 꺼져 있어서 못 했어.")
        self.assertEqual(fastpath.reply_for("dev1", "buzz:ok", False, "err:unknown:buzz"), "레드는 그 기능이 없어.")
        self.assertEqual(fastpath.reply_for("dev1", "motor", False, "OK: (ack: 없음)"), "레드한테 보냈는데 대답이 없어.")


if __name__ == "__main__":
    unittest.main()


import daijin_common as common  # noqa: E402


class TestClockAnswers(unittest.TestCase):
    def test_time_date_weekday(self):
        import time
        now = time.mktime((2026, 10, 11, 19, 5, 0, 0, 0, -1))          # a Sunday evening
        self.assertEqual(fastpath.answer_clock("지금 몇 시야?", now), "지금 오후 7시 5분이야.")
        self.assertEqual(fastpath.answer_clock("오늘 며칠이야?", now), "오늘은 10월 11일 일요일이야.")
        self.assertEqual(fastpath.answer_clock("오늘 무슨 요일이야?", now), "오늘은 일요일이야.")
        noon = time.mktime((2026, 10, 12, 12, 0, 0, 0, 0, -1))
        self.assertEqual(fastpath.answer_clock("몇 시야", noon), "지금 오후 12시야.")

    def test_other_days_and_schedules_go_to_the_model(self):
        for q in ("내일 무슨 요일이야?", "어제 몇 시에 잤지?", "회의 몇 시에 시작해?", "몇 시간 남았어?", "오늘 기분 어때?"):
            with self.subTest(q=q):
                self.assertIsNone(fastpath.answer_clock(q))


class TestBrokerChoice(unittest.TestCase):
    def setUp(self):
        for k in ("DAIJIN_BROKER", "DAIJIN_LOCAL_BROKER_HOST", "DAIJIN_LOCAL_BROKER_PORT"):
            self.addCleanup(os.environ.pop, k, None)

    def test_env_overrides_the_secrets_file(self):
        os.environ["DAIJIN_BROKER"] = "local"
        self.assertEqual(common.sec("BROKER"), "local")
        self.assertEqual(common.broker(), "local")
        self.assertEqual(common.broker_address(), ("127.0.0.1", 1883))
        os.environ["DAIJIN_LOCAL_BROKER_HOST"] = "192.168.0.10"; os.environ["DAIJIN_LOCAL_BROKER_PORT"] = "1884"
        self.assertEqual(common.broker_address(), ("192.168.0.10", 1884))

    def test_anything_but_local_means_cloud(self):
        os.environ["DAIJIN_BROKER"] = "nonsense"
        self.assertEqual(common.broker(), "cloud")


class TestLocalHistory(unittest.TestCase):
    def setUp(self):
        self.addCleanup(selfdb.use, None)
        selfdb.use(os.path.join(tempfile.mkdtemp(), "h.db"))

    def test_recent_turns_and_the_day_asked_about(self):
        import time
        now = time.mktime((2026, 10, 11, 20, 0, 0, 0, 0, -1))
        selfdb.episode("voice", user_text="나 다음 주에 부산 출장 가", reply="잘 다녀와!", ts=now - 86400 - 3600)   # yesterday 19:00
        for i in range(10):
            selfdb.episode("voice", user_text=f"오늘 질문 {i}", reply="응", ts=now - 600 + i)
        plain, note = dm.local_history("안녕", now=now)
        self.assertEqual((len(plain), note), (dm.LOCAL_HISTORY_TURNS, ""))
        self.assertFalse(any("부산" in u for u, _ in plain))                     # crowded out by today
        hist, note = dm.local_history("어제 나랑 무슨 얘기 했었지?", now=now)
        self.assertIn("어제(10월 10일) 대화 기록", note); self.assertIn("부산 출장", note)
        self.assertEqual(len(hist), dm.LOCAL_HISTORY_TURNS)

    def test_the_same_question_asked_before_is_left_out(self):
        import time
        now = time.time()
        selfdb.episode("voice", user_text="어제 무슨 얘기 했지?", reply="기억이 안 나네.", ts=now - 60)
        hist, _ = dm.local_history("어제 무슨 얘기 했지", now=now)
        self.assertEqual(hist, [])
