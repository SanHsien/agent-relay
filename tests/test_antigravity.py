import json
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

    def test_signed_followup_reply_can_reach_same_conversation(self):
        self.mail(kind='reply', reply_to='original-task')
        self.assertEqual(self.deliver()['status'], 'notified')

    def test_invalid_signature_is_refused(self):
        self.mail()
        self.verify.return_value = 'bad signature'
        self.assertEqual(self.deliver()['status'], 'idle')
        self.run.assert_not_called()

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
