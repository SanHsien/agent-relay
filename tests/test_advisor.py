import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_relay import advisor, dispatch, envelope  # noqa: E402
from agent_relay.advisor import Quota  # noqa: E402
from agent_relay.store import Store  # noqa: E402

ALL = ["claude", "codex", "agy"]
FREE = {"claude": Quota("claude", 10, 10), "codex": Quota("codex", 10, 10), "agy": Quota("agy")}


class ComplexityTests(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(advisor.complexity("修正 race condition 的根因", "")[0], "high")
        self.assertEqual(advisor.complexity("README 錯字", "修正錯字")[0], "low")
        self.assertEqual(advisor.complexity("加 power 函式", "寫測試")[0], "medium")
        self.assertEqual(advisor.complexity("加功能", "x", ["src/"])[0], "medium")
        self.assertEqual(advisor.complexity("加功能", "x" * 4000, ["a", "b", "c", "d", "e", "f"])[0], "high")


class RoutingScenarioTests(unittest.TestCase):
    """The decision table, scenario by scenario."""

    def route(self, subject, body="", paths=None, mode="build", quotas=FREE, allowed=ALL):
        d = advisor.decide(subject, body, paths, mode, quotas, allowed)
        return d.runtime, d.model, d.effort

    def test_hard_task_goes_to_opus(self):
        self.assertEqual(self.route("重構鎖的競態"), ("claude", "opus", None))

    def test_hard_task_falls_back_to_codex_high_when_claude_is_spent(self):
        quotas = {**FREE, "claude": Quota("claude", 95, 40, resets_at=time.time() + 600)}
        self.assertEqual(self.route("重構鎖的競態", quotas=quotas), ("codex", None, "high"))

    def test_weekly_limit_also_blocks(self):
        quotas = {**FREE, "claude": Quota("claude", 10, 96)}
        self.assertEqual(self.route("重構鎖的競態", quotas=quotas)[0], "codex")

    def test_medium_task_goes_to_sonnet(self):
        self.assertEqual(self.route("加 power 函式"), ("claude", "sonnet", None))

    def test_mechanical_build_skips_read_only_agy(self):
        self.assertEqual(self.route("README 錯字", "修正錯字"), ("claude", "haiku", None))

    def test_mechanical_plan_uses_agy(self):
        self.assertEqual(self.route("README 錯字檢查", "列出錯字", mode="plan")[0], "agy")

    def test_only_allowed_runtimes(self):
        self.assertEqual(self.route("重構鎖的競態", allowed=["codex"]), ("codex", None, "high"))

    def test_everything_spent_means_wait(self):
        soon, later = time.time() + 300, time.time() + 900
        quotas = {"claude": Quota("claude", 99, 10, resets_at=later), "codex": Quota("codex", 92, 10, resets_at=soon)}
        d = advisor.decide("重構", "", None, "build", quotas, ["claude", "codex"])
        self.assertIsNone(d.runtime)
        self.assertEqual(d.wait_until, soon)

    def test_fallbacks_listed(self):
        d = advisor.decide("重構鎖的競態", "", None, "build", FREE, ALL)
        self.assertEqual(d.fallbacks[0]["runtime"], "codex")


class QuotaSourceTests(unittest.TestCase):
    def test_claude_status_file_and_passed_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            now = time.time()
            path.write_text(json.dumps({"rate_limits": {"five_hour": {"used_percentage": 93, "resets_at": now + 600},
                                                        "seven_day": {"used_percentage": 50, "resets_at": now - 5}}}))
            q = advisor.claude_quota(path, now)
            self.assertEqual((q.five_hour, q.weekly), (93.0, 0.0))
            self.assertTrue(q.blocked())
            self.assertAlmostEqual(q.resets_at, now + 600)
            self.assertEqual(advisor.claude_quota(Path(tmp) / "missing.json").five_hour, None)

    def test_codex_reset_checker_json(self):
        out = json.dumps({"usage": {"primary_window": {"used_percent": 91, "reset_at": 2000000000},
                                    "secondary_window": {"used_percent": 14, "reset_at": 2100000000}}})
        fake = mock.Mock(returncode=0, stdout=out)
        with mock.patch("subprocess.run", return_value=fake) as run:
            q = advisor.codex_quota(["node", "checker.js"])
        self.assertEqual(run.call_args.args[0], ["node", "checker.js", "--json"])
        self.assertEqual((q.five_hour, q.weekly, q.resets_at), (91.0, 14.0, 2000000000.0))
        self.assertTrue(q.blocked())

    def test_codex_checker_missing_or_broken(self):
        self.assertIsNone(advisor.codex_quota(None).five_hour)
        with mock.patch("subprocess.run", side_effect=OSError("no node")):
            self.assertIsNone(advisor.codex_quota(["node", "x"]).five_hour)


class HandoffCardTests(unittest.TestCase):
    def test_card_structure(self):
        body = "## 目標\n加 power\n\n## 驗收條件（DoD）\nunittest 全綠\n\n## 回報\n三段"
        d = advisor.decide("加 power 函式", body, ["calc.py"], "build", FREE, ALL)
        card = advisor.handoff_card(d, "加 power 函式", body, ["calc.py"])
        self.assertTrue(card.startswith("---\nstatus: pending\n"))
        for needed in ("runtime: claude", "model: sonnet", "complexity: medium", "## 派給誰", "## 範圍", "- `calc.py`",
                       "## 驗收條件（DoD）\nunittest 全綠", "## 備案", "## 派工當下的額度"):
            self.assertIn(needed, card)

    def test_card_does_not_repeat_sections(self):
        body = "## 要做什麼\n加註解\n\n## 驗收條件（DoD）\n有註解"
        quotas = {**FREE, "agy": Quota("agy")}
        card = advisor.handoff_card(advisor.decide("加註解", body, None, "build", quotas, ALL), "加註解", body)
        self.assertEqual(card.count("## 要做什麼"), 1)
        self.assertEqual(card.count("## 驗收條件"), 1)
        self.assertIn("## 要做什麼\n加註解\n", card)
        self.assertIn("agy：5 小時 未知", card)
        plain = advisor.handoff_card(advisor.decide("x", "直接做 X", None, "build", FREE, ALL), "x", "直接做 X")
        self.assertIn("## 要做什麼\n直接做 X", plain)

    def test_reasons_not_repeated(self):
        quotas = {**FREE, "codex": Quota("codex", 95, 1)}
        d = advisor.decide("重構鎖的競態", "", None, "build", quotas, ALL)
        self.assertEqual(sum("codex 額度不足" in r for r in d.reasons), 1)

    def test_card_when_waiting(self):
        d = advisor.decide("重構", "", None, "build", {"claude": Quota("claude", 99, 1), "codex": Quota("codex", 99, 1)},
                           ["claude", "codex"])
        self.assertIn("runtime: wait", advisor.handoff_card(d, "重構", ""))


class DispatchAutoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        patch = mock.patch.dict(os.environ, {"AGENT_RELAY_HOME": str(d / "home"), "GIT_AUTHOR_NAME": "t",
                                             "GIT_AUTHOR_EMAIL": "t@e.com", "GIT_COMMITTER_NAME": "t",
                                             "GIT_COMMITTER_EMAIL": "t@e.com"})
        patch.start()
        self.addCleanup(patch.stop)
        self.repo = d / "repos" / "demo"
        self.repo.mkdir(parents=True)
        import subprocess
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "a.py").write_text("x\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "i"], check=True)
        self.cfg = {"store": str(d / "store"), "agent": "cc", "doorbells": [],
                    "dispatch": {"runtimes": ["claude", "codex"], "repo_roots": [str(d / "repos")], "context": False}}
        self.store = Store(self.cfg["store"])

    def send(self, subject):
        env = envelope.build(sender="commander", to="cc", subject=subject, body="b", kind="task",
                             dispatch={"runtime": "auto", "repo": str(self.repo), "timeout_minutes": 1})
        self.store.deliver(env)
        return env

    def reply(self, env):
        return next(e for _, e in self.store.list("commander") if e.get("reply_to") == env["id"])

    def run_with(self, env, quotas, codes):
        seen = []

        def fake_build(spec, prompt, slice_id, log_dir, worktree, cfg):
            seen.append((spec["runtime"], spec.get("model"), spec.get("effort")))
            return ["x", "-c", codes[len(seen) - 1]], "t", None

        with mock.patch.object(dispatch, "build_argv", side_effect=fake_build), \
                mock.patch.object(dispatch, "resolve_executable", return_value=sys.executable), \
                mock.patch.object(advisor, "snapshot", return_value=quotas), \
                mock.patch.object(dispatch.time, "sleep"):
            report = dispatch.dispatch(env["id"], "cc", self.cfg, sync=False)
        return report, seen

    def test_auto_picks_by_quota(self):
        env = self.send("重構鎖的競態")
        quotas = {**FREE, "claude": Quota("claude", 95, 10)}
        report, seen = self.run_with(env, quotas, ["pass"])
        self.assertEqual(seen, [("codex", None, "high")])
        self.assertEqual(report["status"], "ok")
        self.assertIn("分派（runtime auto）", self.reply(env)["body"])

    def test_auto_deferred_when_everything_is_spent(self):
        env = self.send("重構鎖的競態")
        quotas = {"claude": Quota("claude", 99, 1, resets_at=time.time() + 60), "codex": Quota("codex", 99, 1)}
        report, seen = self.run_with(env, quotas, [])
        self.assertEqual((report["status"], seen), ("deferred", []))
        self.assertIn("status: deferred", self.reply(env)["body"])

    def test_rate_limit_switches_to_fallback(self):
        env = self.send("重構鎖的競態")
        report, seen = self.run_with(env, FREE, ["print('Error: 429 Too Many Requests'); raise SystemExit(1)", "pass"])
        self.assertEqual([s[0] for s in seen], ["claude", "codex"])
        self.assertEqual(report["status"], "ok")
        self.assertIn("rate-limited", self.reply(env)["body"])

    def test_transient_failure_resumes_in_place_once(self):
        env = self.send("加 power 函式")
        report, seen = self.run_with(env, FREE, ["print('ECONNRESET'); raise SystemExit(1)", "pass"])
        self.assertEqual([s[0] for s in seen], ["claude", "claude"])
        self.assertEqual(report["status"], "ok")

    def test_real_failure_not_retried(self):
        env = self.send("加 power 函式")
        report, seen = self.run_with(env, FREE, ["print('AssertionError'); raise SystemExit(1)"])
        self.assertEqual((len(seen), report["status"]), (1, "failed"))


class FailureClassTests(unittest.TestCase):
    def test_patterns(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "o.log"
            for text, want in (("HTTP 429 Too Many Requests", "rate-limited"), ("usage limit reached", "rate-limited"),
                               ("ECONNRESET", "transient"), ("503 Service Unavailable", "transient"),
                               ("我們討論了額度與 quota 設計", None), ("AssertionError", None)):
                log.write_text(text, encoding="utf-8")
                self.assertEqual(dispatch.classify_failure(log), want, text)


if __name__ == "__main__":
    unittest.main()
