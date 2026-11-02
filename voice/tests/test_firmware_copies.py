#!/usr/bin/env python3
"""Arduino builds one folder per sketch, so the shared headers live in each sketch as a copy. Keep the copies equal."""
import filecmp, os, unittest

SKETCHES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "sketches")
SHARED = ("broker_choice.h", "tls_time.h", "ca_cert.h", "ota.h")
PAIRS = (("14-daijin", "15-home-node"),)      # 12·13 은 옛 스케치: ca_cert.h 가 있어도 더 손대지 않으니 보지 않는다


class TestSharedHeaders(unittest.TestCase):
    def test_each_shared_header_is_the_same_in_every_sketch(self):
        for a, b in PAIRS:
            for name in SHARED:
                with self.subTest(header=name):
                    pa, pb = os.path.join(SKETCHES, a, name), os.path.join(SKETCHES, b, name)
                    self.assertTrue(os.path.exists(pa) and os.path.exists(pb), name)
                    self.assertTrue(filecmp.cmp(pa, pb, shallow=False),
                                    f"{a}/{name} 와 {b}/{name} 가 다르다: 한쪽을 고쳤으면 다른 쪽에도 복사할 것")


class TestOtaRollbackHook(unittest.TestCase):
    def test_the_rollback_override_has_c_linkage(self):
        # Without extern "C" the core's weak verifyRollbackLater (false) wins and no OTA image ever waits for confirmation.
        src = open(os.path.join(SKETCHES, "15-home-node", "ota.h")).read()
        self.assertIn('extern "C" bool verifyRollbackLater()', src)


class TestHomeBrokerAcl(unittest.TestCase):
    def test_the_home_broker_carries_the_vps_acl_unchanged_and_iot_cannot_reach_the_agent(self):
        voice = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        vps = open(os.path.join(voice, "mosquitto", "daijin.acl")).read()
        home = open(os.path.join(voice, "mosquitto", "home.acl")).read()
        self.assertTrue(home.endswith(vps))
        iot = home.split("user iot", 1)[1].split("\nuser ", 1)[0]
        for agent_topic in ("daijin/ask", "daijin/say", "daijin/text", "daijin/#"):
            self.assertNotIn(agent_topic, iot)


class TestBrokerAcl(unittest.TestCase):
    def test_every_node_has_exactly_its_own_topics_and_nothing_wider(self):
        import json
        voice = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        nodes = json.load(open(os.path.join(voice, "nodes.json")))
        acl = open(os.path.join(voice, "mosquitto", "daijin.acl")).read()
        block = acl.split("user nodes", 1)[1].split("\nuser ", 1)[0]
        for nid in nodes:
            for want in (f"write daijin/dev/{nid}/status", f"write daijin/dev/{nid}/state",
                         f"write daijin/dev/{nid}/event", f"read  daijin/dev/{nid}/cmd"):
                with self.subTest(node=nid, line=want):
                    self.assertIn(want, block)
        self.assertNotIn("#", block.replace("# ", ""))          # no wildcard for the nodes login
        self.assertNotIn("readwrite", block)


if __name__ == "__main__":
    unittest.main()
