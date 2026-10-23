#!/usr/bin/env python3
"""Arduino builds one folder per sketch, so the shared headers live in each sketch as a copy. Keep the copies equal."""
import filecmp, os, unittest

SKETCHES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "sketches")
SHARED = ("broker_choice.h", "tls_time.h", "ca_cert.h")
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


if __name__ == "__main__":
    unittest.main()
