"""`send --to antigravity` reads the sidecar's allow-list and refuses what the sidecar would skip."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_relay import antigravity_send, cli
from agent_relay.store import Store


class SendAllowlistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.sender_home = root / 'commander-home'
        self.sender_home.mkdir()
        self.store = root / 'store'
        self.recv = root / 'recv-home'
        self.recv.mkdir()
        self.rmwt = root / 'rmwt'
        (self.rmwt / 'rfo-fork-credit').mkdir(parents=True)
        self.other = root / 'rea'
        self.other.mkdir()
        self.write_receiver({'repos': [str(self.rmwt), str(self.other)]})
        env = {'AGENT_RELAY_HOME': str(self.sender_home), 'AGENT_RELAY_STORE': str(self.store),
               'AGENT_RELAY_AGENT': 'commander', 'AGENT_RELAY_ANTIGRAVITY_HOME': str(self.recv)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_receiver(self, settings):
        cfg = {'agent': 'antigravity', 'store': str(self.store),
               'antigravity': {'conversation_id': '11111111-1111-4111-8111-111111111111',
                               'senders': ['commander'], 'agentapi_argv': ['x'], **settings}}
        (self.recv / 'config.json').write_text(json.dumps(cfg), encoding='utf-8')

    def send(self, *extra, to='antigravity', kind='task'):
        out, err = io.StringIO(), io.StringIO()
        argv = ['send', '--to', to, '--kind', kind, '--subject', 's', '--body', 'b', '--no-sync', '--json', *extra]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def delivered(self, mailbox='antigravity'):
        return Store(str(self.store)).list(mailbox)

    def test_exact_listed_project_is_sent(self):
        code, _, err = self.send('--project', str(self.rmwt))
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(len(self.delivered()), 1)

    def test_subdirectory_of_listed_entry_is_rejected_and_names_the_closest_entry(self):
        sub = self.rmwt / 'rfo-fork-credit'
        code, _, err = self.send('--project', str(sub))
        self.assertEqual(code, 10)
        self.assertIn(str(self.rmwt.resolve()), err)  # the closest allowed entry
        self.assertIn('closest allowed entry', err)
        self.assertIn('exact match', err)
        self.assertEqual(self.delivered(), [])  # nothing was written

    def test_unknown_project_names_a_closest_entry_and_lists_the_allowed_ones(self):
        code, _, err = self.send('--project', str(self.rmwt.parent / 're'))
        self.assertEqual(code, 10)
        self.assertIn('closest allowed entry', err)
        self.assertIn(str(self.other.resolve()), err)
        self.assertEqual(self.delivered(), [])

    def test_override_requires_a_real_reason(self):
        sub = str(self.rmwt / 'rfo-fork-credit')
        for reason in ('', '   ', 'x'):
            code, _, err = self.send('--project', sub, '--allow-unlisted-project', reason)
            self.assertEqual(code, 10, reason)
            self.assertIn('reason', err)
        self.assertEqual(self.delivered(), [])

    def test_override_with_reason_sends_warns_and_leaves_an_audit_line(self):
        sub = str(self.rmwt / 'rfo-fork-credit')
        code, out, err = self.send('--project', sub, '--allow-unlisted-project', 'receiver adds the entry next')
        self.assertEqual(code, 0)
        self.assertIn('WARNING', err)
        self.assertIn('receiver adds the entry next', err)
        mail_id = json.loads(out)['id']
        audit = (self.sender_home / 'send-overrides.jsonl').read_text(encoding='utf-8').splitlines()
        self.assertEqual(json.loads(audit[0])['id'], mail_id)
        self.assertEqual(json.loads(audit[0])['reason'], 'receiver adds the entry next')

    def test_override_does_not_log_when_the_project_was_listed_anyway(self):
        code, _, _ = self.send('--project', str(self.rmwt), '--allow-unlisted-project', 'not actually needed')
        self.assertEqual(code, 0)
        self.assertFalse((self.sender_home / 'send-overrides.jsonl').exists())

    def test_unreadable_allowlist_warns_loudly_but_still_sends(self):
        for label, prepare in (
                ('missing', lambda: (self.recv / 'config.json').unlink()),
                ('broken json', lambda: (self.recv / 'config.json').write_text('{', encoding='utf-8')),
                ('no section', lambda: (self.recv / 'config.json').write_text('{}', encoding='utf-8')),
                ('bad repos', lambda: self.write_receiver({'repos': [str(self.rmwt / 'gone')]}))):
            with self.subTest(label):
                prepare()
                code, _, err = self.send('--project', str(self.rmwt / 'rfo-fork-credit'))
                self.assertEqual(code, 0)
                self.assertIn('WARNING: cannot read the Antigravity allow-list', err)
                self.assertIn('AGENT_RELAY_ANTIGRAVITY_HOME', err)

    def test_other_receivers_and_projectless_mail_are_not_checked(self):
        code, _, err = self.send('--project', str(self.rmwt / 'rfo-fork-credit'), to='codex')
        self.assertEqual((code, err), (0, ''))
        code, _, err = self.send()  # antigravity, session routing by the receiver, no project
        self.assertEqual((code, err), (0, ''))

    def test_new_session_ref_without_project_or_task_kind_is_rejected(self):
        code, _, err = self.send('--ref', 'antigravity-session:new')
        self.assertEqual(code, 10)
        self.assertIn('--kind task and --project', err)
        code, _, err = self.send('--ref', 'antigravity-session:new', '--project', str(self.rmwt), kind='reply')
        self.assertEqual(code, 10)
        self.assertEqual(self.delivered(), [])

    def test_receiver_home_comes_from_sender_config_when_env_is_unset(self):
        os.environ.pop('AGENT_RELAY_ANTIGRAVITY_HOME')
        cfg = {'antigravity_home': str(self.recv)}
        self.assertEqual(antigravity_send.receiver_home(cfg), self.recv)
        self.assertEqual(antigravity_send.receiver_home({}).name, '.agent-relay-antigravity')

    def test_legacy_single_repo_setting_is_read_like_the_sidecar_does(self):
        self.write_receiver({'repo': str(self.other)})
        code, _, _ = self.send('--project', str(self.other))
        self.assertEqual(code, 0)
        code, _, err = self.send('--project', str(self.rmwt))
        self.assertEqual(code, 10)
        self.assertIn(str(self.other.resolve()), err)


class ClosestAllowedTests(unittest.TestCase):
    def test_prefers_the_deepest_ancestor(self):
        allowed = [Path('/a'), Path('/a/b'), Path('/z')]
        entry, why = antigravity_send.closest_allowed(Path('/a/b/c/d'), allowed)
        self.assertEqual(entry, Path('/a/b'))
        self.assertIn('inside this entry', why)

    def test_parent_of_a_listed_repo_is_pointed_at_it(self):
        entry, why = antigravity_send.closest_allowed(Path('/a'), [Path('/a/b'), Path('/z')])
        self.assertEqual(entry, Path('/a/b'))
        self.assertIn('inside the project', why)


if __name__ == '__main__':
    unittest.main()
