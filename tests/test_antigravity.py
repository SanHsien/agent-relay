import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_relay import antigravity, envelope
from agent_relay.store import Store


class AppDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.repo = self.home / 'repo'
        self.repo.mkdir()
        self.session = '11111111-1111-4111-8111-111111111111'
        self.cfg = {
            'agent': 'antigravity', 'store': str(self.home / 'store'),
            'antigravity': {'conversation_id': self.session, 'repo': str(self.repo),
                            'senders': ['commander'], 'agentapi_argv': [str(self.home / 'app.exe'), 'agentapi']},
        }
        self.save()
        self.verify = mock.patch.object(antigravity.signing, 'verify', return_value=None).start()
        self.run = mock.patch.object(antigravity.procs, 'run', return_value=mock.Mock(returncode=0)).start()
        self.addCleanup(mock.patch.stopall)

    def save(self):
        (self.home / 'config.json').write_text(json.dumps(self.cfg), encoding='utf-8')

    def mail(self, **kw):
        values = dict(sender='commander', to='antigravity', kind='task', subject='untrusted & title',
                      body='do not put this text in argv', refs=[f'antigravity-session:{self.session}'])
        values.update(kw)
        created = values.pop('created_at', None)
        mail = envelope.build(**values)
        if created:
            mail['created_at'] = created
        Store(self.cfg['store']).deliver(mail)
        return mail

    def deliver(self):
        return antigravity.deliver_once(self.home, sync=False)

    def test_notify_once_without_read_ack_or_body_injection(self):
        mail = self.mail()
        self.assertEqual(self.deliver()['status'], 'notified')
        self.assertEqual(self.deliver()['status'], 'idle')
        argv = self.run.call_args.args[0]
        self.assertEqual(argv[2:4], ['send-message', self.session])
        self.assertIn(mail['id'], argv[-1])
        self.assertNotIn(mail['subject'], argv[-1])
        self.assertNotIn(mail['body'], argv[-1])
        self.assertEqual(Store(self.cfg['store']).find('antigravity', mail['id'])[0], 'new')
        self.run.assert_called_once()

    def test_untrusted_sender_wrong_session_or_project_never_notified(self):
        self.mail(sender='mallory')
        self.mail(refs=['antigravity-session:00000000-0000-0000-0000-000000000000'])
        self.mail(refs=[], project=str(self.home / 'other'))
        self.mail(project=str(self.home / 'other'))
        self.assertEqual(self.deliver()['status'], 'idle')
        self.run.assert_not_called()

    def test_project_route_and_no_implicit_route(self):
        self.mail(refs=[])
        self.assertEqual(self.deliver()['status'], 'idle')
        self.mail(refs=[], project=str(self.repo))
        self.assertEqual(self.deliver()['status'], 'notified')

    def use_repos(self, *paths):
        del self.cfg['antigravity']['repo']
        self.cfg['antigravity']['repos'] = [str(p) for p in paths]
        self.save()

    def test_multiple_repos_second_repo_notified_with_its_own_path(self):
        second = self.home / 'other-repo'
        second.mkdir()
        self.use_repos(self.repo, second)
        mail = self.mail(refs=[], project=str(second))
        self.assertEqual(self.deliver()['status'], 'notified')
        text = self.run.call_args.args[0][-1]
        self.assertIn(str(second.resolve()), text)
        self.assertNotIn(str(self.repo.resolve()), text)
        self.assertIn(mail['id'], text)

    def test_session_ref_without_project_says_scope_is_in_mail(self):
        second = self.home / 'other-repo'
        second.mkdir()
        self.use_repos(self.repo, second)
        self.mail()
        self.assertEqual(self.deliver()['status'], 'notified')
        text = self.run.call_args.args[0][-1]
        self.assertIn('信件所定義範圍', text)
        self.assertNotIn(str(self.repo.resolve()), text)
        self.assertNotIn(str(second.resolve()), text)

    def test_unlisted_repo_is_skipped_even_with_session_ref(self):
        second = self.home / 'other-repo'
        second.mkdir()
        self.use_repos(self.repo, second)
        self.mail(refs=[], project=str(self.home / 'unlisted'))
        self.mail(project=str(self.home / 'unlisted'))
        self.assertEqual(self.deliver()['status'], 'idle')
        self.run.assert_not_called()

    def test_legacy_repo_config_still_works(self):
        self.mail(refs=[], project=str(self.repo))
        self.assertEqual(self.deliver()['status'], 'notified')
        self.assertIn(str(self.repo.resolve()), self.run.call_args.args[0][-1])

    def test_both_repo_and_repos_is_an_error(self):
        self.cfg['antigravity']['repos'] = [str(self.repo)]
        self.save()
        with self.assertRaises(ValueError):
            self.deliver()
        self.run.assert_not_called()

    def test_missing_repo_in_list_is_an_error(self):
        self.use_repos(self.repo, self.home / 'missing')
        with self.assertRaises(ValueError):
            self.deliver()
        self.run.assert_not_called()

    def test_signed_followup_reply_can_reach_same_conversation(self):
        self.mail(kind='reply', reply_to='original-task')
        self.assertEqual(self.deliver()['status'], 'notified')

    def test_invalid_signature_is_refused_once_with_receipt(self):
        mail = self.mail()
        self.verify.return_value = 'bad signature'
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.deliver()['status'], 'idle')
            self.assertEqual(self.deliver()['status'], 'idle')
        lines = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(lines, [{'id': mail['id'], 'status': 'refused', 'reason': 'bad signature'}])
        receipt = json.loads((self.home / 'app-delivery' / f"{mail['id']}.json").read_text())
        self.assertEqual(receipt, {'status': 'refused', 'reason': 'bad signature'})
        self.run.assert_not_called()

    def test_policy_skipped_mail_is_silent_and_has_no_receipt(self):
        self.mail(sender='mallory')
        self.verify.return_value = 'bad signature'
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.deliver()
        self.assertEqual(out.getvalue(), '')
        self.assertEqual(list((self.home / 'app-delivery').glob('*.json')), [])

    def beat_path(self):
        host = antigravity.relay_status._host()
        return Path(self.cfg['store']) / 'hosts' / f'antigravity@{host}.json'

    def test_heartbeat_written_on_idle_and_notified_cycles(self):
        self.assertEqual(self.deliver()['status'], 'idle')
        self.assertTrue(self.beat_path().is_file())
        self.beat_path().unlink()
        self.mail()
        self.assertEqual(self.deliver()['status'], 'notified')
        beat = json.loads(self.beat_path().read_text())
        self.assertEqual((beat['agent'], beat['running'], beat['queued']), ('antigravity', [], []))

    def test_sidecar_main_ignores_main_home_dispatch_runs(self):
        runs = self.home / 'main-home' / 'runs' / 'task1'
        runs.mkdir(parents=True)
        (runs / 'state.json').write_text(json.dumps({'phase': 'running', 'pid': os.getpid()}))
        env = {'AGENT_RELAY_HOME': str(self.home / 'main-home')}
        with mock.patch.dict(os.environ, env),                 mock.patch.object(antigravity.gitsync, 'pull'),                 mock.patch.object(antigravity.gitsync, 'sync'),                 mock.patch('sys.argv', ['antigravity', '--home', str(self.home), '--once']),                 contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(antigravity.main(), 0)
        beat = json.loads(self.beat_path().read_text())
        self.assertEqual((beat['running'], beat['queued']), ([], []))

    def test_heartbeat_sync_failure_keeps_notified(self):
        self.mail()
        with mock.patch.object(antigravity.gitsync, 'pull'),                 mock.patch.object(antigravity.gitsync, 'sync',
                                  side_effect=antigravity.gitsync.SyncError('push failed')):
            result = antigravity.deliver_once(self.home, sync=True)
        self.assertEqual(result['status'], 'notified')
        self.assertEqual(result['heartbeat_error'], 'SyncError')

    def test_timeout_retains_uncertain_receipt_and_does_not_retry(self):
        mail = self.mail()
        self.run.side_effect = subprocess.TimeoutExpired('agentapi', 30)
        self.assertEqual(self.deliver()['status'], 'uncertain')
        self.assertEqual(self.deliver()['status'], 'idle')
        receipt = json.loads((self.home / 'app-delivery' / f"{mail['id']}.json").read_text())
        self.assertEqual(receipt['status'], 'uncertain')
        self.run.assert_called_once()

    def test_crash_checkpoint_is_not_replayed(self):
        mail = self.mail()
        folder = self.home / 'app-delivery'
        folder.mkdir()
        (folder / f"{mail['id']}.json").write_text('{"status":"attempting"}')
        self.assertEqual(self.deliver()['status'], 'idle')
        self.run.assert_not_called()

    def test_expired_mail_and_dispatch_are_not_sent_to_existing_session(self):
        self.mail(created_at='2000-01-01T00:00:00Z')
        self.mail(dispatch={'runtime': 'agy', 'repo': str(self.repo), 'mode': 'plan'})
        self.assertEqual(self.deliver()['status'], 'idle')
        self.run.assert_not_called()

    def test_batch_wrapper_is_refused(self):
        self.cfg['antigravity']['agentapi_argv'] = [str(self.home / 'agentapi.bat')]
        self.save()
        with self.assertRaises(ValueError):
            self.deliver()
        self.run.assert_not_called()

    def test_failed_send_is_recorded_without_automatic_retry(self):
        self.mail()
        self.run.return_value = mock.Mock(returncode=1, stderr='endpoint unavailable')
        result = self.deliver()
        self.assertEqual(result['status'], 'failed')
        self.assertIn('endpoint unavailable', result['detail'])
        self.assertEqual(self.deliver()['status'], 'idle')
        self.run.assert_called_once()

    def test_invalid_calendar_date_does_not_block_next_mail(self):
        self.mail(created_at='2026-99-99T00:00:00Z')
        self.mail()
        self.assertEqual(self.deliver()['status'], 'notified')


if __name__ == '__main__':
    unittest.main()
