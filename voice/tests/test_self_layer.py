#!/usr/bin/env python3
"""The self layer: selfdb (episodes, asks, drives, policy, memories, persona) and the brain's state block.

Every test uses a throwaway SQLite file. No broker, no model, no audio."""
import json, os, pathlib, sys, tempfile, time, types, unittest

VOICE_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
sys.path.insert(0, VOICE_DIR)
try:
    import paho.mqtt.client  # noqa: F401
except ImportError:
    from test_daijin_mqtt import _install_paho_stub
    _install_paho_stub()
import selfdb
import daijin_mqtt as dm


class DbCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.dir, True)
        selfdb.use(os.path.join(self.dir, "t.db"))
        self.addCleanup(selfdb.use, None)


class TestSchema(DbCase):
    def test_opening_twice_is_harmless_and_wal(self):
        c = selfdb.db(); selfdb.db()
        self.assertEqual(c.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(c.execute("SELECT COUNT(*) FROM episodes").fetchone()[0], 0)

    def test_write_failure_does_not_raise(self):
        selfdb.use(os.path.join(self.dir, "nope", "t.db"))      # directory does not exist
        self.assertIsNone(selfdb.episode("voice", user_text="안녕"))


class TestEpisodesAndAsks(DbCase):
    def test_episode_round_trip(self):
        eid = selfdb.episode("voice", user_text="레드 모터 돌려 줘", reply="돌렸어.", stt_conf=-0.1, rms=900, clip_s=2.5,
                             turn_json='{"stt":0.4}', ts=1000)
        rows = selfdb.episodes_since(0)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["id"], rows[0]["user_text"], rows[0]["reply"], rows[0]["clip_s"]), (eid, "레드 모터 돌려 줘", "돌렸어.", 2.5))
        self.assertEqual(selfdb.last_voice_ts(), 1000)
        self.assertEqual(selfdb.episodes_since(0, kinds=["say"]), [])

    def test_ask_opens_pending_and_closes_with_latency(self):
        aid = selfdb.open_ask("node_loss", "ask", "레드가 7시간째 꺼져 있어.", "initiative", trigger="레드 오프라인 7h", drive_value=0.3, ts=5000)
        self.assertEqual([a["id"] for a in selfdb.open_asks()], [aid])
        self.assertTrue(selfdb.close_ask(aid, "engaged", response_text="응 일부러 꺼 뒀어", response_ts=5042))
        a = selfdb.recent_asks(1)[0]
        self.assertEqual((a["response_class"], a["latency_s"], a["label_source"], a["presence"]), ("engaged", 42, "rule", "unknown"))
        self.assertEqual(selfdb.open_asks(), [])

    def test_unanswered_has_no_latency_and_reflect_can_relabel(self):
        aid = selfdb.open_ask("contact", "say", "오늘은 아직 얘기를 못 했네.", "initiative", ts=100)
        selfdb.close_ask(aid, "unanswered", response_ts=700)
        self.assertIsNone(selfdb.recent_asks(1)[0]["latency_s"])
        selfdb.relabel_ask(aid, response_class="refused_context", wanted_to_ask_less=True)
        a = selfdb.recent_asks(1)[0]
        self.assertEqual((a["response_class"], a["label_source"], a["wanted_to_ask_less"]), ("refused_context", "reflect", 1))

    def test_bad_response_class_is_refused(self):
        aid = selfdb.open_ask("body", "say", "나 좀 뜨거워.", "initiative")
        with self.assertRaises(AssertionError):
            selfdb.close_ask(aid, "ignored")

    def test_episode_can_be_linked_to_the_ask_it_answers(self):
        aid = selfdb.open_ask("contact", "say", "x", "initiative")
        eid = selfdb.episode("voice", user_text="응")
        selfdb.link_episode(eid, aid)
        self.assertEqual(selfdb.episodes_since(0)[0]["ask_id"], aid)


class TestDrivesPolicyMemoryPersona(DbCase):
    def test_latest_drive_per_name(self):
        selfdb.record_drive("body", 0.2, {"temp": 45}, ts=10)
        selfdb.record_drive("body", 0.6, {"temp": 62}, ts=20)
        selfdb.record_drive("contact", 0.1, ts=15)
        latest = selfdb.latest_drives()
        self.assertEqual(latest["body"]["value"], 0.6)
        self.assertEqual(json.loads(latest["body"]["inputs"]), {"temp": 62})
        self.assertEqual(set(latest), {"body", "contact"})

    def test_policy_defaults_then_updates(self):
        p = selfdb.policy_get("contact", 8 * 3600)
        self.assertEqual((p["base_interval_s"], p["backoff_level"], p["last_ask_ts"]), (8 * 3600, 0, None))
        selfdb.policy_update("contact", backoff_level=2, avoid_hours=["09", "21", "09"])
        p = selfdb.policy_get("contact", 1)                       # existing row wins over the new default
        self.assertEqual((p["base_interval_s"], p["backoff_level"], json.loads(p["avoid_hours"])), (8 * 3600, 2, ["09", "21"]))
        with self.assertRaises(AssertionError):
            selfdb.policy_update("contact", foo=1)

    def test_memory_is_superseded_not_deleted(self):
        m1 = selfdb.add_memory("preference", "사용자는 아침에 잔소리를 싫어한다", importance=7, ts=100)
        m2 = selfdb.add_memory("preference", "사용자는 잔소리를 언제든 싫어한다", importance=8, supersedes=m1, ts=200)
        valid = selfdb.valid_memories()
        self.assertEqual([m["id"] for m in valid], [m2])
        old = selfdb.db().execute("SELECT valid_to FROM memories WHERE id=?", (m1,)).fetchone()
        self.assertEqual(old["valid_to"], 200)

    def test_top_memories_respect_count_and_char_budget(self):
        for i in range(8):
            selfdb.add_memory("fact", "기억 %d " % i + "가" * 30, importance=i)
        top = selfdb.top_memories(5, max_chars=200)
        self.assertLessEqual(len(top), 5)
        self.assertLessEqual(sum(len(m["text"]) for m in top), 200)
        self.assertEqual(top[0]["importance"], 7)                 # highest importance first

    def test_persona_proposal_needs_approval(self):
        self.assertIsNone(selfdb.persona_current())
        v1 = selfdb.persona_set_current("나는 다이진이야.", by="human")
        self.assertEqual(selfdb.persona_current()["id"], v1)
        v2 = selfdb.persona_propose("나는 다이진이야. 낮에는 잔소리 안 해.", "낮 잔소리 금지 추가", "reflect")
        self.assertEqual(selfdb.persona_current()["id"], v1)      # still the old one
        self.assertEqual([p["id"] for p in selfdb.persona_pending()], [v2])
        self.assertTrue(selfdb.persona_approve(v2))
        self.assertEqual(selfdb.persona_current()["id"], v2)
        self.assertEqual(selfdb.db().execute("SELECT status FROM persona_versions WHERE id=?", (v1,)).fetchone()[0], "superseded")
        self.assertFalse(selfdb.persona_approve(v1))              # cannot approve a superseded one


class TestStateBlock(DbCase):
    def setUp(self):
        super().setUp()
        dm._body.update({"device": None, "device_ts": 0, "nodes": {}})
        self.addCleanup(lambda: dm._body.update({"device": None, "device_ts": 0, "nodes": {}}))

    def test_empty_when_nothing_is_known(self):
        self.assertEqual(dm.state_block(), "")

    def test_body_nodes_ledger_memory_and_persona_are_summarised(self):
        now = 1_800_000_000
        dm.note_status(dm.T_DEV_STATUS, json.dumps({"online": True, "ts": now - 30, "temp_c": 41.2, "rssi": -57, "uptime_s": 5300}))
        dm.note_status("daijin/dev/dev1/status", json.dumps({"online": True, "ts": now - 20}))
        dm.note_status("daijin/dev/dev2/status", json.dumps({"online": False}))
        selfdb.open_ask("contact", "say", "오늘은 아직 얘기를 못 했네.", "initiative", ts=now - 3600)
        selfdb.close_ask(1, "refused_context", response_text="지금 바빠", response_ts=now - 3500)
        selfdb.add_memory("preference", "낮에 잔소리를 싫어한다", importance=7)
        selfdb.persona_set_current("나는 다이진이야. 장난을 좋아해.")
        s = dm.state_block(now=now)
        for piece in ("온도 41.2도", "신호 -57", "켜진 지 88분", "레드 켜짐", "블루 꺼짐", "사정이 있어 거절", "낮에 잔소리를 싫어한다", "장난을 좋아해"):
            self.assertIn(piece, s)
        self.assertNotIn("값)", s)                                 # fresh reading: no staleness note
        self.assertLessEqual(len(s), dm.STATE_MAX_CHARS)
        self.assertIn("[지금 상태", dm.sys_prompt())             # appended to the system prompt on every turn

    def test_old_readings_and_dead_device_are_marked(self):
        now = 1_800_000_000
        dm.note_status(dm.T_DEV_STATUS, json.dumps({"online": True, "ts": now - 1000, "temp_c": 40, "rssi": -60, "uptime_s": 60}))
        self.assertIn("(16분 전 값)", dm.state_block(now=now))
        dm.note_status(dm.T_DEV_STATUS, json.dumps({"online": False}))
        self.assertIn("꺼져 있음", dm.state_block(now=now))
        dm.note_status("daijin/dev/dev1/status", json.dumps({"online": True, "ts": now - 500}))
        self.assertIn("레드 켜짐 (하트비트 끊김)", dm.state_block(now=now))

    def test_garbage_status_is_ignored(self):
        dm.note_status(dm.T_DEV_STATUS, b"not json")
        self.assertIsNone(dm._body["device"])


class TestProactiveContext(DbCase):
    def test_say_lines_since_the_last_agent_turn_are_prefixed(self):
        now = time.time()
        selfdb.episode("voice", user_text="안녕", reply="안녕!", ts=now - 3000)
        selfdb.episode("say", reply="오늘 산책 다녀왔어?", ts=now - 2000)
        selfdb.episode("voice", user_text="응", reply="좋았겠다", ts=now - 1900)      # the agent heard about the walk already
        selfdb.episode("say", reply="물 좀 마셨어?", ts=now - 600)
        selfdb.episode("say", reply="내일 날씨 알려 줄까?", ts=now - 500)
        out = dm.with_proactive_context("오늘 먼저 말 건 거 어떻게 됐어?", now=now)
        self.assertTrue(out.endswith("오늘 먼저 말 건 거 어떻게 됐어?"))
        self.assertIn('"물 좀 마셨어?"', out); self.assertIn('"내일 날씨 알려 줄까?"', out)
        self.assertNotIn("산책", out)

    def test_nothing_said_means_text_unchanged(self):
        now = time.time()
        selfdb.episode("voice", user_text="안녕", reply="안녕!", ts=now - 100)
        self.assertEqual(dm.with_proactive_context("응", now=now), "응")

    def test_prefix_is_capped(self):
        now = time.time()
        for i in range(10):
            selfdb.episode("say", reply="아주 긴 문장 " * 8 + str(i), ts=now - 100 + i)
        out = dm.with_proactive_context("질문", now=now)
        self.assertLess(len(out), dm.PROACTIVE_MAX_CHARS + 80)


class TestTurnLogging(DbCase):
    def test_a_turn_becomes_an_episode_unless_dropped(self):
        class C:
            def publish(self, *a, **kw): pass
        t = dm.Turn("voice"); t.text_in, t.text_out = "안녕", "응, 안녕!"; t.info.update(stt_conf=-0.2, rms=800, clip_s=1.5)
        t.done(C())
        d = dm.Turn("voice"); d.info["dropped"] = "silence"; d.done(C())
        rows = selfdb.episodes_since(0)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["user_text"], rows[0]["reply"], rows[0]["stt_conf"]), ("안녕", "응, 안녕!", -0.2))
        self.assertEqual(json.loads(rows[0]["turn_json"])["kind"], "voice")


if __name__ == "__main__":
    unittest.main()


import ledger_rules          # noqa: E402
import replay_journal        # noqa: E402


class TestLedgerRules(unittest.TestCase):
    def test_classification_by_wording(self):
        cases = {
            "지금 바빠, 이따 얘기하자": "refused_context",
            "나중에": "refused_context",
            "말 좀 살살해, 인마": "negative",
            "그만 좀 물어봐": "negative",
            "응": "acknowledged",
            "알았어.": "acknowledged",
            "고마워": "acknowledged",
            "응 일부러 꺼 뒀어, 공원에 갔다 와서 켤 거야": "engaged",
            "레드 모터 돌려 줘": "engaged",
            "": "unanswered",
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(ledger_rules.classify(text), want)

    def test_quiet_hours_wrap_midnight(self):
        q = ledger_rules.parse_quiet("23-08")
        self.assertEqual(q, (23, 8))
        self.assertTrue(ledger_rules.in_quiet(23, q)); self.assertTrue(ledger_rules.in_quiet(3, q))
        self.assertFalse(ledger_rules.in_quiet(8, q)); self.assertFalse(ledger_rules.in_quiet(12, q))
        self.assertTrue(ledger_rules.in_quiet(13, ledger_rules.parse_quiet("12-14")))
        self.assertIsNone(ledger_rules.parse_quiet("")); self.assertIsNone(ledger_rules.parse_quiet("25-3"))
        self.assertFalse(ledger_rules.in_quiet(3, None))

    def test_backoff_doubles_and_mute_wins(self):
        p = {"base_interval_s": 3600, "backoff_level": 2, "last_ask_ts": 1000, "muted_until": None, "avoid_hours": None}
        self.assertEqual(ledger_rules.next_allowed_ts(p), 1000 + 3600 * 4)
        p["muted_until"] = 99999
        self.assertEqual(ledger_rules.next_allowed_ts(p), 99999)

    def test_outcomes_change_policy_as_the_ledger_says(self):
        p = {"base_interval_s": 3600, "backoff_level": 1, "last_ask_ts": 0, "muted_until": None, "avoid_hours": '["21"]'}
        self.assertEqual(ledger_rules.apply_outcome(p, "engaged", 10), {"backoff_level": 0})
        self.assertEqual(ledger_rules.apply_outcome(p, "acknowledged", 10), {})
        self.assertEqual(ledger_rules.apply_outcome(p, "refused_context", 9), {"backoff_level": 2, "avoid_hours": ["09", "21"]})
        neg = ledger_rules.apply_outcome(p, "negative", 9, now=1000)
        self.assertEqual((neg["backoff_level"], neg["muted_until"]), (2, 1000 + 24 * 3600))
        gone = ledger_rules.apply_outcome(p, "negative", 9, repairs_in_a_row=2)
        self.assertEqual(gone["muted_until"], ledger_rules.FAR_FUTURE)
        p["backoff_level"] = 2
        self.assertEqual(ledger_rules.apply_outcome(p, "unanswered", 9), {"backoff_level": 2})   # capped while presence is unknown

    def test_repair_lines_never_deny(self):
        import time
        now = time.mktime((2026, 10, 12, 18, 0, 0, 0, 0, -1))
        self.assertEqual(ledger_rules.repair_line("explain", trigger="레드가 7시간째 꺼져 있어서"), "아까는 레드가 7시간째 꺼져 있어서 말 걸었어.")
        self.assertEqual(ledger_rules.repair_line("explain", trigger="14시간째 얘기를 못 해서", asked_ts=now - 4 * 3600, now=now),
                         "아까는 14시간째 얘기를 못 해서 말 걸었어.")
        self.assertEqual(ledger_rules.repair_line("explain", trigger="14시간째 얘기를 못 해서", asked_ts=now - 22 * 3600, now=now),
                         "어제는 14시간째 얘기를 못 해서 말 걸었어.")
        self.assertEqual(ledger_rules.repair_line("explain", trigger="신호가 약해서", asked_ts=now - 3 * 86400, now=now),
                         "지난번엔 신호가 약해서 말 걸었어.")
        self.assertEqual(ledger_rules.repair_line("promise", promise="저녁엔 안 물을게"), "앞으로는 저녁엔 안 물을게.")
        with self.assertRaises(ValueError):
            ledger_rules.repair_line("deny")
        self.assertEqual(ledger_rules.promise_for({"muted_until": ledger_rules.FAR_FUTURE}), "네가 풀어 줄 때까지 이 얘기는 안 꺼낼게")
        self.assertEqual(ledger_rules.promise_for({"avoid_hours": ["09"]}), "이 시간대엔 안 물을게")
        self.assertIsNone(ledger_rules.promise_for({}))


class TestReplay(DbCase):
    JOURNAL = """1700000000.1 host unit[1]: 🎧 클립 1 수신: 6 청크
1700000001.2 host unit[1]:   🗣️  나: 안녕, 다이진.  (음량 900, 신뢰도 -0.12)
1700000004.0 host unit[1]:   🤖 daijin: 안녕!  (+2.8s)
1700000004.5 host unit[1]:   🤖 daijin: 잘 지냈어?  (+3.3s)
1700000100.0 host unit[1]: 📢 say: 레드 방금 꺼졌어. 전원이나 네트워크를 확인해 줘.
1700000110.0 host unit[1]: 📨 ask: [시스템 알림] 홈 노드 dev2가 방금 오프라인이 됐어(브로커 LWT).
1700000116.0 host unit[1]:   🤖 daijin: 블루도 꺼졌어, 전원 쪽 같아.  (+6.0s)
1700000200.0 host unit[1]:   🗣️  나: 응 일부러 껐어, 공원 갔다 올게.  (음량 800, 신뢰도 -0.2)
1700000203.0 host unit[1]:   🤖 daijin: 알았어, 잘 다녀와.  (+3.0s)
1700009000.0 host unit[1]: 📢 say: 오늘은 아직 얘기를 못 했네.
"""

    def test_parse_groups_replies_under_their_turn(self):
        turns = replay_journal.parse(self.JOURNAL.splitlines())
        self.assertEqual([t["kind"] for t in turns], ["voice", "say", "ask", "voice", "say"])
        self.assertEqual(turns[0]["reply"], "안녕! 잘 지냈어?")
        self.assertEqual((turns[0]["rms"], turns[0]["conf"]), (900, -0.12))
        self.assertEqual(turns[2]["reply"], "블루도 꺼졌어, 전원 쪽 같아.")

    def test_replay_fills_episodes_and_settles_the_ledger(self):
        turns = replay_journal.parse(self.JOURNAL.splitlines())
        res = replay_journal.replay(turns, now=1700020000)
        self.assertEqual((res["episodes"], res["asks"]), (5, 3))
        asks = selfdb.recent_asks(10)[::-1]
        self.assertEqual([(a["source"], a["response_class"]) for a in asks],
                         [("fleet", "unanswered"), ("fleet", "engaged"), ("manual", "unanswered")])   # the reply belongs to the latest ask
        self.assertEqual(asks[1]["response_text"], "응 일부러 껐어, 공원 갔다 올게.")
        linked = [e for e in selfdb.episodes_since(0) if e["ask_id"]]
        self.assertEqual(len(linked), 1)
        again = replay_journal.replay(turns, now=1700020000)
        self.assertEqual((again["episodes"], again["skipped"]), (0, 5))                            # idempotent


import initiative            # noqa: E402


class FakeClient:
    def __init__(self):
        self.out = []
    def publish(self, topic, payload=None, qos=0, **kw):
        self.out.append((topic, payload))


class TestInitiativeDrives(DbCase):
    def test_contact_grows_with_silence(self):
        body = initiative.Body()
        now = 1_800_000_000
        d = initiative.compute_drives(body, now - 12 * 3600, now)
        self.assertAlmostEqual(d["contact"][0], 0.5)
        self.assertEqual(initiative.compute_drives(body, None, now)["contact"][0], 0.0)

    def test_node_loss_tracks_the_longest_outage(self):
        body = initiative.Body()
        now = 1_800_000_000
        body.note("daijin/dev/dev1/status", json.dumps({"online": False}), now - 7 * 3600)
        body.note("daijin/dev/dev2/status", json.dumps({"online": False}), now - 2 * 3600)
        body.note("daijin/dev/dev2/status", json.dumps({"online": True, "ts": now}), now)     # blue came back
        value, inputs, info = initiative.compute_drives(body, now, now)["node_loss"]
        self.assertAlmostEqual(value, 7 / 24, places=3)
        self.assertEqual(inputs["node"], "dev1")
        self.assertIn("레드가 7시간째", info["trigger"])

    def test_body_uses_fresh_readings_only(self):
        body = initiative.Body()
        now = 1_800_000_000
        body.note(initiative.T_DEV_STATUS, json.dumps({"online": True, "temp_c": 66, "rssi": -55}), now - 10)
        v, inputs, info = initiative.compute_drives(body, now, now)["body"]
        self.assertAlmostEqual(v, 0.8); self.assertIn("뜨거워", info["line"])
        body.note(initiative.T_DEV_STATUS, json.dumps({"online": True, "temp_c": 40, "rssi": -78}), now - 10)
        v, _, info = initiative.compute_drives(body, now, now)["body"]
        self.assertAlmostEqual(v, 0.9); self.assertIn("신호", info["line"])
        body.note(initiative.T_DEV_STATUS, json.dumps({"online": True, "temp_c": 90, "rssi": -90}), now - 1000)   # stale
        self.assertEqual(initiative.compute_drives(body, now, now)["body"][0], 0.0)


class TestLateLinking(DbCase):
    def test_episode_written_after_the_ask_closed_gets_linked_by_time(self):
        now = 1_800_000_000
        aid = selfdb.open_ask("contact", "say", "오늘은 아직 얘기를 못 했네.", "initiative", ts=now)
        selfdb.close_ask(aid, "engaged", response_text="응 바빴어", response_ts=now + 30)      # initiative saw text/in
        eid = selfdb.episode("voice", user_text="응 바빴어", reply="그랬구나", ts=now + 38)      # brain wrote the turn 8 s later
        other = selfdb.episode("voice", user_text="딴 얘기", ts=now + 900)
        self.assertEqual(selfdb.link_unlinked_episodes(), 1)
        rows = {e["id"]: e["ask_id"] for e in selfdb.episodes_since(0)}
        self.assertEqual((rows[eid], rows[other]), (aid, None))
        self.assertEqual(selfdb.link_unlinked_episodes(), 0)


class TestNodeStatePersists(DbCase):
    def test_offline_since_survives_a_restart(self):
        now = 1_800_000_000
        b = initiative.Body()
        b.note("daijin/dev/dev1/status", json.dumps({"online": False}), now - 5 * 3600)
        b2 = initiative.Body()                                                   # process restarted, retained offline arrives again
        b2.note("daijin/dev/dev1/status", json.dumps({"online": False}), now)
        self.assertEqual(b2.offline_since["dev1"], now - 5 * 3600)
        b2.note("daijin/dev/dev1/status", json.dumps({"online": True, "ts": now}), now + 10)
        self.assertNotIn("dev1", b2.offline_since)
        self.assertEqual(selfdb.node_state_all()["dev1"], (True, now + 10))


class TestInitiativeFilter(DbCase):
    def setUp(self):
        super().setUp()
        self.cfg = {"INITIATIVE_QUIET": "23-08", "INITIATIVE_DAILY_BUDGET": "3"}
        orig = initiative.sec
        initiative.sec = lambda k: self.cfg.get(k, "")
        self.addCleanup(setattr, initiative, "sec", orig)
        self.body = initiative.Body()
        self.noon = 1_800_000_000 - (time.localtime(1_800_000_000).tm_hour - 12) * 3600   # some day at 12:xx local
        self.device_on(self.noon)

    def device_on(self, at):
        self.body.note(initiative.T_DEV_STATUS, b'{"online": true, "rssi": -55}', now=at)

    def _ok(self, name="contact", value=0.9, now=None, hooks=None):
        return initiative.admissible(name, value, self.body, hooks or types.SimpleNamespace(), now or self.noon)

    def test_each_gate_in_order(self):
        self.assertEqual(self._ok(value=0.1), (False, "threshold"))
        self.assertEqual(self._ok(hooks=types.SimpleNamespace(mute_reports=lambda: True)), (False, "muted_by_hooks"))
        night = self.noon + 12 * 3600
        self.assertEqual(self._ok(now=night), (False, "quiet_hours"))
        self.device_on(night)
        self.assertEqual(self._ok("body", 0.95, now=night), (True, None))                      # danger overrides quiet
        self.device_on(self.noon)
        for i in range(3):
            selfdb.open_ask("contact", "say", f"x{i}", initiative.SOURCE, ts=self.noon - 60)
        self.assertEqual(self._ok(), (False, "daily_budget"))
        self.cfg["INITIATIVE_DAILY_BUDGET"] = "9"
        selfdb.policy_update("contact", last_ask_ts=self.noon - 60)
        self.assertEqual(self._ok(), (False, "interval"))
        selfdb.policy_update("contact", last_ask_ts=0, avoid_hours=[f"{time.localtime(self.noon).tm_hour:02d}"])
        self.assertEqual(self._ok(), (False, "avoid_hour"))
        selfdb.policy_update("contact", avoid_hours=None)
        self.body.last_brain_turn = self.noon - 5
        self.assertEqual(self._ok(), (False, "brain_busy"))
        self.body.last_brain_turn = 0
        self.assertEqual(self._ok(), (True, None))

    def test_no_one_to_hear_means_no_speaking(self):
        self.assertEqual(self._ok(now=self.noon + initiative.DEVICE_FRESH_S + 1), (False, "device_offline"))   # heartbeats stopped
        self.body.note(initiative.T_DEV_STATUS, b'{"online": false}', now=self.noon)                          # the broker's will
        self.assertEqual(self._ok(), (False, "device_offline"))
        self.body.device = None                                                                             # never seen
        self.assertEqual(self._ok(), (False, "device_offline"))
        self.device_on(self.noon)                                                                           # plugged back in
        self.assertEqual(self._ok(), (True, None))

    def test_a_line_held_back_for_a_dead_device_leaves_the_ledger_and_policy_alone(self):
        c = FakeClient(); ledger = initiative.Ledger()
        self.body.note(initiative.T_DEV_STATUS, b'{"online": false}', now=self.noon)
        selfdb.episode("voice", user_text="안녕", ts=self.noon - 30 * 3600)
        initiative.tick(c, self.body, ledger, types.SimpleNamespace(), dry_run=False, now=self.noon)
        self.assertEqual(c.out, [])
        self.assertEqual(selfdb.asks_since(0), [])
        self.assertEqual(selfdb.policy_get("contact", initiative.DRIVES["contact"]["base_interval_s"])["backoff_level"], 0)
        self.device_on(self.noon + 30)                                                                      # it says hello once it is on
        initiative.tick(c, self.body, ledger, types.SimpleNamespace(), dry_run=False, now=self.noon + 30)
        self.assertEqual([p for t, p in c.out if t == initiative.T_SAY], ["오늘은 아직 얘기를 못 했네."])

    def test_dry_run_tick_plans_without_speaking_and_keeps_the_interval(self):
        c = FakeClient()
        selfdb.episode("voice", user_text="안녕", ts=self.noon - 20 * 3600)                     # 20 h of silence → contact 0.83
        initiative.tick(c, self.body, initiative.Ledger(), types.SimpleNamespace(), dry_run=True, now=self.noon)
        topics = [t for t, _ in c.out]
        self.assertIn(initiative.T_PLAN, topics)
        self.assertNotIn(initiative.T_SAY, topics); self.assertNotIn(initiative.T_ASK, topics)
        plan = json.loads([p for t, p in c.out if t == initiative.T_PLAN][0])
        self.assertEqual((plan["drive"], plan["dry_run"], plan["text"]), ("contact", True, "오늘은 아직 얘기를 못 했네."))
        c.out.clear()
        initiative.tick(c, self.body, initiative.Ledger(), types.SimpleNamespace(), dry_run=True, now=self.noon + 60)
        self.assertEqual(c.out, [])                                                             # not again a minute later
        self.assertEqual(len(selfdb.asks_since(0)), 0)                                          # dry-run opens no ledger row

    def test_live_tick_speaks_once_and_the_ledger_records_it(self):
        c = FakeClient(); ledger = initiative.Ledger()
        selfdb.episode("voice", user_text="안녕", ts=self.noon - 20 * 3600)
        initiative.tick(c, self.body, ledger, types.SimpleNamespace(), dry_run=False, now=self.noon)
        said = [p for t, p in c.out if t == initiative.T_SAY]
        self.assertEqual(said, ["오늘은 아직 얘기를 못 했네."])
        ledger.on_bus(initiative.T_SAY, said[0].encode(), now=self.noon + 1)                     # the bus echoes it back
        a = selfdb.recent_asks(1)[0]
        self.assertEqual((a["source"], a["drive"], a["channel"], a["response_class"]), ("initiative", "contact", "say", None))
        self.assertAlmostEqual(a["drive_value"], 20 / 24, places=2)
        ledger.on_bus(initiative.T_TXT_IN, "지금 바빠, 이따 얘기하자".encode(), now=self.noon + 120)
        a = selfdb.recent_asks(1)[0]
        self.assertEqual((a["response_class"], a["latency_s"]), ("refused_context", 119))
        pol = selfdb.policy_get("contact", 1)
        self.assertEqual(pol["backoff_level"], 1)
        self.assertIn(f"{time.localtime(self.noon).tm_hour:02d}", json.loads(pol["avoid_hours"]))

    def test_fleet_and_manual_lines_are_ledgered_too_and_expire(self):
        ledger = initiative.Ledger()
        ledger.on_bus(initiative.T_SAY, "레드가 방금 꺼졌어. 전원이나 네트워크를 확인해 줘.".encode(), now=self.noon)
        ledger.on_bus(initiative.T_SAY, "새 펌웨어 올라왔어.".encode(), now=self.noon + 5)
        asks = selfdb.recent_asks(2)[::-1]
        self.assertEqual([(a["source"], a["drive"]) for a in asks], [("fleet", "fleet"), ("manual", "unknown")])
        ledger.expire(now=self.noon + 700)
        self.assertEqual([a["response_class"] for a in selfdb.recent_asks(2)], ["unanswered", "unanswered"])
        self.assertEqual(selfdb.policy_get("fleet", 1)["backoff_level"], 0)                       # fleet lines never touch policy

    def test_repair_is_prefixed_after_a_refusal(self):
        selfdb.open_ask("contact", "say", "오늘은 아직 얘기를 못 했네.", initiative.SOURCE, trigger="14시간째 얘기를 못 해서", ts=self.noon - 90000)
        selfdb.close_ask(selfdb.recent_asks(1)[0]["id"], "refused_context", response_text="바빠", response_ts=self.noon - 89990)
        info = initiative.compute_drives(self.body, self.noon - 20 * 3600, self.noon)["contact"][2]
        line, strategy = initiative.compose("contact", info, self.noon)
        self.assertEqual(strategy, "explain")
        self.assertTrue(line.startswith("어제는 14시간째 얘기를 못 해서 말 걸었어. 오늘은"), line)    # the reason it spoke then, a day ago


import reflect               # noqa: E402
import persona               # noqa: E402


class TestReflect(DbCase):
    def test_day_bounds_pick_yesterday_before_dawn(self):
        at_0330 = time.mktime((2026, 10, 11, 3, 30, 0, 0, 0, -1))
        start, end = reflect.day_bounds(now=at_0330)
        self.assertEqual(time.strftime("%Y-%m-%d", time.localtime(start)), "2026-10-10")
        self.assertEqual(end - start, 86400)
        start, _ = reflect.day_bounds("2026-10-03")
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M", time.localtime(start)), "2026-10-03 00:00")

    def test_extract_json_tolerates_fences_and_prose(self):
        self.assertEqual(reflect.extract_json('물론이지!\n```json\n{"note": "x", "memories": []}\n```'), {"note": "x", "memories": []})
        self.assertIsNone(reflect.extract_json("no json here"))
        self.assertIsNone(reflect.extract_json("{broken"))

    def test_apply_writes_memories_labels_and_a_persona_proposal(self):
        start, end = 1_800_000_000, 1_800_086_400
        selfdb.persona_set_current("나는 다이진이야.")
        old = selfdb.add_memory("preference", "사용자는 아침 잔소리를 싫어한다", importance=5, ts=start + 10)
        selfdb.episode("voice", user_text="말 좀 살살해", reply="미안", ts=start + 100)
        aid = selfdb.open_ask("contact", "say", "오늘은 아직 얘기를 못 했네.", "initiative", ts=start + 200)
        selfdb.close_ask(aid, "engaged", response_text="아 지금 바빠", response_ts=start + 260)
        out = {"memories": [{"kind": "preference", "text": "사용자는 잔소리를 언제든 싫어한다", "importance": 8, "source_episode": 1,
                             "replaces": "사용자는 아침 잔소리를 싫어한다"},
                            {"kind": "fact", "text": "사용자는 아침 잔소리를 싫어한다"},            # already known → skipped
                            {"kind": "weird", "text": "중요도 없는 것"}],
               "ask_labels": [{"ask_id": f"ask{aid}", "response_class": "refused_context", "wanted_to_ask_less": True},
                              {"ask_id": 999, "response_class": "negative"}],
               "persona_new": "나는 다이진이야. 바쁠 땐 안 물어.", "persona_change_summary": "바쁠 때 안 묻기", "note": "오늘 한 줄"}
        done = reflect.apply(out, start, end, now=end)
        self.assertEqual((done["memories"], done["replaced"], done["relabeled"], done["wanted"]), (2, 1, 1, 1))
        self.assertEqual([m["text"] for m in selfdb.valid_memories()], ["사용자는 잔소리를 언제든 싫어한다", "중요도 없는 것"])
        self.assertEqual(selfdb.db().execute("SELECT valid_to FROM memories WHERE id=?", (old,)).fetchone()[0], end)
        a = selfdb.recent_asks(1)[0]
        self.assertEqual((a["response_class"], a["label_source"], a["wanted_to_ask_less"]), ("refused_context", "reflect", 1))
        self.assertEqual(selfdb.persona_current()["text"], "나는 다이진이야.")                   # proposal only
        self.assertEqual([p["id"] for p in selfdb.persona_pending()], [done["persona"]])
        self.assertEqual(selfdb.db().execute("SELECT text FROM notes WHERE kind='reflect'").fetchone()[0], "오늘 한 줄")

    def test_prompt_contains_the_day_and_nothing_else(self):
        start, end = 1_800_000_000, 1_800_086_400
        selfdb.episode("voice", user_text="오늘 말", reply="응", ts=start + 10)
        selfdb.episode("voice", user_text="어제 말", reply="응", ts=start - 10)
        p = reflect.build_prompt(start, end)
        self.assertIn("오늘 말", p); self.assertNotIn("어제 말", p); self.assertIn('"memories"', p)


class TestPersonaCli(DbCase):
    def test_seed_import_export_approve(self):
        import contextlib, io
        def run(*argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = persona.main(list(argv))
            return rc, buf.getvalue()
        rc, out = run("show"); self.assertIn("없음", out)
        f = os.path.join(self.dir, "p.md"); pathlib.Path(f).write_text("나는 다이진이야.\n")
        self.assertEqual(run("import", f)[0], 0)
        self.assertEqual(selfdb.persona_current()["text"], "나는 다이진이야.")
        self.assertIn("같다", run("import", f)[1])
        vid = selfdb.persona_propose("나는 다이진이야. 밤엔 조용해.", "밤", "reflect")
        rc, out = run("show"); self.assertIn(f"제안 #{vid}", out); self.assertIn("+나는 다이진이야. 밤엔 조용해.", out)
        self.assertEqual(run("approve", str(vid))[0], 0)
        self.assertEqual(selfdb.persona_current()["id"], vid)
        self.assertEqual(run("approve", str(vid))[0], 1)                # already current
        g = os.path.join(self.dir, "out.md")
        self.assertEqual(run("export", g)[0], 0)
        self.assertEqual(pathlib.Path(g).read_text(), "나는 다이진이야. 밤엔 조용해.\n")


import selfdb_merge          # noqa: E402


class TestOfflineMerge(unittest.TestCase):
    """offline-mode.sh push: rows made on the Mac after the last sync go into the server DB without clobbering it."""

    def setUp(self):
        self.addCleanup(selfdb.use, None)
        d = tempfile.mkdtemp()
        self.server, self.mac = os.path.join(d, "server.db"), os.path.join(d, "mac.db")
        self.t0 = 1_800_000_000
        selfdb.use(self.server)                                     # the shared past: both sides have it
        old_ask = selfdb.open_ask("contact", "say", "오늘은 아직 얘기를 못 했네.", "initiative", trigger="t", ts=self.t0 - 500)
        selfdb.add_memory("fact", "사람은 커피를 좋아한다", ts=self.t0 - 400)
        selfdb.policy_update("contact", base_interval_s=28800, backoff_level=1, last_ask_ts=self.t0 - 500, avoid_hours='["14"]')
        selfdb.use(None)
        import sqlite3
        src, dst = sqlite3.connect(self.server), sqlite3.connect(self.mac)
        src.backup(dst); src.close(); dst.close()                   # offline-mode.sh sync (backup API, WAL included)
        selfdb_merge.mark(self.mac, "pulled", ts=self.t0)
        self.old_ask = old_ask

    def test_offline_rows_are_added_with_new_ids_and_links(self):
        selfdb.use(self.server)                                     # the server kept running meanwhile
        selfdb.episode("voice", user_text="서버에서 한 말", reply="응", ts=self.t0 + 50)
        selfdb.use(self.mac)                                        # the Mac, offline
        a = selfdb.open_ask("contact", "say", "어제는 바빴지?", "initiative", trigger="u", ts=self.t0 + 100)
        selfdb.close_ask(a, "engaged", response_text="응 좀", response_ts=self.t0 + 110)
        e = selfdb.episode("voice", user_text="응 좀", reply="고생했어", ts=self.t0 + 112, ask_id=a)
        selfdb.add_memory("event", "사람이 부산 출장을 다녀왔다", source_episode=e, ts=self.t0 + 200)
        selfdb.policy_update("contact", backoff_level=0, last_ask_ts=self.t0 + 100, avoid_hours='["09"]')
        selfdb.use(None)
        self.assertEqual(selfdb_merge.unpushed(self.mac), 3)

        done = selfdb_merge.merge(self.mac, self.server, self.t0)
        self.assertEqual((done["asks"], done["episodes"], done["memories"], done["policy"]), (1, 1, 1, 1))
        selfdb.use(self.server)
        eps = selfdb.episodes_since(self.t0)
        self.assertEqual([x["user_text"] for x in eps], ["서버에서 한 말", "응 좀"])
        ask = selfdb.recent_asks(1)[0]
        self.assertEqual((ask["text"], ask["response_class"]), ("어제는 바빴지?", "engaged"))
        self.assertEqual(eps[1]["ask_id"], ask["id"])               # the link follows the new id
        mem = [m for m in selfdb.valid_memories() if "부산" in m["text"]][0]
        self.assertEqual(mem["source_episode"], eps[1]["id"])
        pol = selfdb.policy_get("contact", 28800)
        self.assertEqual((pol["backoff_level"], pol["last_ask_ts"]), (0, self.t0 + 100))
        self.assertEqual(json.loads(pol["avoid_hours"]), ["09", "14"])   # hours to avoid are joined
        self.assertEqual(len(selfdb.recent_asks(10)), 2)            # the shared past is not doubled

        again = selfdb_merge.merge(self.mac, self.server, self.t0)  # running it twice is harmless
        self.assertEqual((again["asks"], again["episodes"], again["memories"]), (0, 0, 0))

    def test_a_memory_replaced_offline_is_closed_on_the_server_too(self):
        selfdb.use(self.mac)
        old = [m for m in selfdb.valid_memories()][0]
        selfdb.add_memory("fact", "사람은 요즘 차를 마신다", supersedes=old["id"], ts=self.t0 + 300)
        selfdb.use(None)
        done = selfdb_merge.merge(self.mac, self.server, self.t0)
        self.assertEqual((done["memories"], done["retired"]), (1, 1))
        selfdb.use(self.server)
        self.assertEqual([m["text"] for m in selfdb.valid_memories()], ["사람은 요즘 차를 마신다"])

    def test_the_server_keeps_its_newer_policy(self):
        selfdb.use(self.server)
        selfdb.policy_update("contact", backoff_level=3, last_ask_ts=self.t0 + 900)
        selfdb.use(self.mac)
        selfdb.policy_update("contact", backoff_level=0, last_ask_ts=self.t0 + 100)
        selfdb.use(None)
        selfdb_merge.merge(self.mac, self.server, self.t0)
        selfdb.use(self.server)
        self.assertEqual(selfdb.policy_get("contact", 28800)["backoff_level"], 3)

    def test_sync_marks_and_nothing_unpushed_after_a_push(self):
        self.assertEqual(selfdb_merge.last_sync(self.mac), self.t0)
        self.assertEqual(selfdb_merge.unpushed(self.mac), 0)
        selfdb.use(self.mac)
        selfdb.episode("voice", user_text="비상 모드에서", reply="응", ts=self.t0 + 10)
        selfdb.use(None)
        self.assertEqual(selfdb_merge.unpushed(self.mac), 1)
        selfdb_merge.mark(self.mac, "pushed", ts=self.t0 + 20)
        self.assertEqual(selfdb_merge.unpushed(self.mac), 0)
        self.assertEqual(selfdb_merge.last_sync(self.server), None)    # the marks stay on the Mac


class TestIntentionallyOff(DbCase):
    """"일부러 꺼 뒀어": the node stops counting toward node_loss until it comes back on."""

    def setUp(self):
        super().setUp()
        self.now = 1_800_000_000
        selfdb.node_state_set("dev1", False, self.now - 30 * 3600)
        selfdb.node_state_set("dev2", False, self.now - 10 * 3600)

    def loss(self):
        body = initiative.Body()
        return initiative.compute_drives(body, self.now, self.now)["node_loss"]

    def test_the_words_from_the_real_conversation_excuse_every_node_that_is_off(self):
        self.assertAlmostEqual(self.loss()[0], 1.0)
        got = initiative.excuse_from_text("일부러 꺼졌으니까 신경 쓰지 말라고 하고 싶었어요.", self.now)
        self.assertEqual(got, {"dev1", "dev2"})
        value, inputs, _ = self.loss()
        self.assertEqual((value, inputs["node"]), (0.0, None))

    def test_a_named_node_is_excused_alone(self):
        self.assertEqual(initiative.excuse_from_text("레드는 일부러 꺼 놨어", self.now), {"dev1"})
        value, inputs, _ = self.loss()
        self.assertEqual(inputs["node"], "dev2")                       # blue still counts
        self.assertAlmostEqual(value, 10 / 24, places=2)

    def test_coming_back_on_clears_the_excuse(self):
        initiative.excuse_from_text("1번 일부러 꺼 뒀어", self.now)
        selfdb.node_state_set("dev1", True, self.now + 10)
        selfdb.node_state_set("dev1", False, self.now + 20)
        self.assertNotIn("dev1", selfdb.node_excused())

    def test_switching_the_daijin_itself_off_excuses_no_node(self):
        for text in ("다이진 어제 일부러 꺼 뒀어", "널 일부러 꺼 둔 거야"):
            with self.subTest(text=text):
                self.assertEqual(initiative.excuse_from_text(text, self.now), set())
        self.assertEqual(initiative.excuse_from_text("다이진이랑 레드 일부러 꺼 뒀어", self.now), {"dev1"})   # a named node still counts

    def test_other_talk_about_switching_is_not_an_excuse(self):
        for text in ("레드 꺼 줘", "블루 켜 놔", "일부러 그런 거 아니야", "불 좀 꺼", "끄지 마"):
            with self.subTest(text=text):
                self.assertEqual(initiative.excuse_from_text(text, self.now), set())
        self.assertEqual(selfdb.node_excused(), set())

    def test_the_brain_is_told_too(self):
        initiative.excuse_from_text("블루 일부러 꺼 둔 거야", self.now)
        dm._body["nodes"]["dev2"] = ({"online": False}, self.now)
        self.addCleanup(dm._body["nodes"].pop, "dev2", None)
        self.assertIn("블루 꺼짐 (사람이 일부러 꺼 둠", dm.state_block(self.now))
