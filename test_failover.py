"""Automated tests that record timestamps to evidence/test_timestamps.csv.

LOCAL tests (always run): start primary and backup servers on localhost, check identity/health, chaos failure and recovery.
LIVE tests (opt-in via environment variables; skipped otherwise):
  PRIMARY_URL, BACKUP_URL         e.g. https://primary-xyz.onrender.com   -> health/identity of the deployed endpoints
  FAILOVER_HOST                   demo hostname hosted at the DNS provider -> DNS answers + which role users reach
  RESOLVERS                       comma list of DNS server IPs (default 8.8.8.8,1.1.1.1)
  RUN_FAILOVER_TEST=1 + CHAOS_TOKEN (+ FAILOVER_TIMEOUT seconds, default 1200)
                                  DESTRUCTIVE: fails the live primary, waits for real DNS failover, then recovers it.
Nothing is simulated in live mode: results come from the real internet, real DNS and the provider's health checks.
Run:  python -m unittest -v test_failover
"""
import csv
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tools"))
import dnsq  # noqa: E402
import endpoint  # noqa: E402

EVIDENCE = os.path.join(HERE, "evidence", "test_timestamps.csv")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def record(mode, check, result, detail=""):
    os.makedirs(os.path.dirname(EVIDENCE), exist_ok=True)
    new = not os.path.exists(EVIDENCE)
    with open(EVIDENCE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["ts_utc", "mode", "check", "result", "detail"])
        w.writerow([now(), mode, check, result, detail])


def http(url, method="GET", headers=None, timeout=15):
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(4096).decode("utf-8", "replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(4096).decode("utf-8", "replace"), dict(e.headers)
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e), {}


def role_of(status, body, headers):
    r = headers.get("X-Endpoint-Role")
    if r:
        return r
    for role in ("primary", "backup"):
        if "ENDPOINT-ROLE:" + role in body or "role=" + role in body:
            return role
    return ""


class LocalTests(unittest.TestCase):
    def start(self, role, token):
        httpd = endpoint.make_server(role, token, "127.0.0.1", 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return "http://127.0.0.1:%d" % httpd.server_address[1]

    def test_both_roles_identifiable_and_healthy(self):
        for role in ("primary", "backup"):
            base = self.start(role, "t")
            s, b, h = http(base + "/health")
            record("local", role + " /health", s, b.strip())
            self.assertEqual((s, b.strip()), (200, "HEALTHY role=" + role))
            s, b, h = http(base + "/")
            self.assertEqual(role_of(s, b, h), role)

    def test_failure_and_recovery(self):
        base = self.start("primary", "secret")
        self.assertEqual(http(base + "/_chaos/down", "POST", {"X-Chaos-Token": "wrong"})[0], 403)
        self.assertEqual(http(base + "/_chaos/down", "POST", {"X-Chaos-Token": "secret"})[0], 200)
        record("local", "primary chaos down", "sent")
        s, b, _ = http(base + "/health")
        record("local", "primary /health after down", s)
        self.assertEqual(s, 503)
        self.assertEqual(http(base + "/_chaos/up", "POST", {"X-Chaos-Token": "secret"})[0], 200)
        s, _, _ = http(base + "/health")
        record("local", "primary /health after up", s)
        self.assertEqual(s, 200)

    def test_chaos_disabled_without_token(self):
        base = self.start("backup", "")
        self.assertEqual(http(base + "/_chaos/down", "POST", {"X-Chaos-Token": ""})[0], 403)


PRIMARY = os.environ.get("PRIMARY_URL", "").rstrip("/")
BACKUP = os.environ.get("BACKUP_URL", "").rstrip("/")
HOST = os.environ.get("FAILOVER_HOST", "")
RESOLVERS = [r for r in os.environ.get("RESOLVERS", "8.8.8.8,1.1.1.1").split(",") if r]


@unittest.skipUnless(PRIMARY and BACKUP, "set PRIMARY_URL and BACKUP_URL for live endpoint tests")
class LiveEndpointTests(unittest.TestCase):
    def test_deployed_endpoints(self):
        for role, base in (("primary", PRIMARY), ("backup", BACKUP)):
            s, b, h = http(base + "/health", timeout=90)  # free hosts may be waking from sleep
            record("live", role + " /health", s, b.strip()[:80])
            self.assertEqual(s, 200, b)
            self.assertEqual(role_of(s, b, h), role)


@unittest.skipUnless(HOST, "set FAILOVER_HOST for live DNS tests")
class LiveDnsTests(unittest.TestCase):
    def test_dns_answers_recorded(self):
        ok = 0
        for ns in RESOLVERS:
            try:
                res = dnsq.query(HOST, ns)
            except Exception as e:
                record("live", "dns " + ns, "ERR", repr(e))
                continue
            ans = ";".join("%s:%s ttl=%s" % (a["type"], a["data"], a["ttl"]) for a in res["answers"])
            record("live", "dns " + ns, "rcode%d" % res["rcode"], ans)
            ok += bool(res["answers"])
        self.assertGreater(ok, 0, "no resolver returned an answer for %s" % HOST)

    def test_users_reach_primary_in_normal_operation(self):
        s, b, h = http("http://%s/" % HOST, timeout=90)
        record("live", "http via failover host", s, role_of(s, b, h))
        self.assertEqual(s, 200)
        self.assertEqual(role_of(s, b, h), "primary")


@unittest.skipUnless(HOST and PRIMARY and os.environ.get("RUN_FAILOVER_TEST") == "1" and os.environ.get("CHAOS_TOKEN"),
                     "DESTRUCTIVE: set FAILOVER_HOST, PRIMARY_URL, RUN_FAILOVER_TEST=1, CHAOS_TOKEN")
class LiveFailoverTest(unittest.TestCase):
    def test_real_dns_failover_and_recovery(self):
        tok = {"X-Chaos-Token": os.environ["CHAOS_TOKEN"]}
        timeout = float(os.environ.get("FAILOVER_TIMEOUT", "1200"))

        def poll_until(want_role):
            t_end = time.monotonic() + timeout
            while time.monotonic() < t_end:
                s, b, h = http("http://%s/" % HOST, timeout=10)
                r = role_of(s, b, h) if s == 200 else ""
                record("live", "poll via host", s, r or b[:60])
                if r == want_role:
                    return time.monotonic()
                time.sleep(5)
            return None

        self.assertEqual(http(PRIMARY + "/_chaos/down", "POST", tok, timeout=90)[0], 200)
        t_down, wall_down = time.monotonic(), now()
        record("live", "primary chaos down", "sent", wall_down)
        try:
            t_fail = poll_until("backup")
            self.assertIsNotNone(t_fail, "DNS did not fail over to backup within %ss" % timeout)
            record("live", "FAILOVER observed", "seconds", round(t_fail - t_down, 1))
        finally:
            self.assertEqual(http(PRIMARY + "/_chaos/up", "POST", tok, timeout=90)[0], 200)
            t_up = time.monotonic()
            record("live", "primary chaos up", "sent", now())
        t_back = poll_until("primary")
        self.assertIsNotNone(t_back, "DNS did not fail back to primary")
        record("live", "RECOVERY observed", "seconds", round(t_back - t_up, 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
