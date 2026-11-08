"""`archive` talks to the LS conversation-management RPC; a fake LS stands in for it here."""

import contextlib
import http.server
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_relay import antigravity_archive as arch
from agent_relay import cli

CONV = '9c2e74fe-1c31-4ea5-8892-473a077b46ae'
OTHER = '24bfc046-6ad9-4dd5-9b01-8fcf35cfaa6e'
MAIL = '20261107T192645949Z-c2e317'
SECRET = 'csrf-secret-value-must-never-appear'


class FakeLS:
    """Records requests; on success rewrites the annotation file like the real LS does."""

    def __init__(self, gemini: Path, status=200, rewrite=True, body=b'{}'):
        self.requests, self.gemini, self.status, self.rewrite, self.body = [], gemini, status, rewrite, body
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get('Content-Length', 0))
                payload = json.loads(self.rfile.read(length))
                outer.requests.append({'path': self.path, 'token': self.headers.get('x-codeium-csrf-token'),
                                       'proto': self.headers.get('Connect-Protocol-Version'), 'body': payload})
                if outer.status == 200 and outer.rewrite:
                    for cid in payload['cascadeIds']:
                        (outer.gemini / 'annotations' / f'{cid}.pbtxt').write_text(
                            'title:"t" archived:true archival_status_timestamp:{seconds:%d nanos:5}' % (2000 + len(outer.requests)),
                            encoding='utf-8')
                self.send_response(outer.status)
                self.end_headers()
                self.wfile.write(outer.body)

            def log_message(self, *a):
                pass

        self.server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.gemini = Path(self.tmp.name)
        (self.gemini / 'annotations').mkdir()
        (self.gemini / 'daemon').mkdir()
        log = self.gemini / 'sidecar_data' / 'agent-relay' / 'logs'
        log.mkdir(parents=True)
        (log / 'sidecar.log').write_text('\n'.join([
            'not json at all',
            json.dumps({'id': MAIL, 'conversation_id': CONV, 'mode': 'new-conversation', 'status': 'notified', 'exit': 0}),
            json.dumps({'id': '20261107T202928110Z-541831', 'conversation_id': OTHER, 'mode': 'new-conversation',
                        'status': 'failed'}),
            json.dumps({'id': 'x-follow', 'conversation_id': OTHER, 'mode': 'follow-up', 'status': 'notified'}),
        ]) + '\n', encoding='utf-8')
        self.annotation = self.gemini / 'annotations' / f'{CONV}.pbtxt'
        self.annotation.write_text('title:"[Relay] x" archived:false', encoding='utf-8')
        env = {k: v for k, v in os.environ.items() if not k.startswith('ANTIGRAVITY_')}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def hub(self, ls: FakeLS, pid=None):
        (self.gemini / 'daemon' / f'hub_{ls.port}.json').write_text(
            json.dumps({'pid': pid or os.getpid(), 'httpPort': ls.port, 'httpsPort': 1, 'csrfToken': SECRET}),
            encoding='utf-8')

    def fake(self, **kw):
        ls = FakeLS(self.gemini, **kw)
        self.addCleanup(ls.close)
        self.hub(ls)
        return ls

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(['archive', '--gemini-dir', str(self.gemini), *args])
        return code, out.getvalue(), err.getvalue()

    def test_archive_by_conversation_posts_the_rpc_and_reads_the_annotation_back(self):
        ls = self.fake()
        code, out, err = self.run_cli('--conversation', CONV, '--json')
        self.assertEqual(code, 0, err)
        self.assertNotIn(SECRET, out + err)
        report = json.loads(out)
        self.assertTrue(report['verified'])
        self.assertEqual(report['http'], 200)
        self.assertFalse(report['before']['archived'])
        self.assertTrue(report['after']['archived'])
        (request,) = ls.requests
        self.assertEqual(request['path'], arch.RPC)
        self.assertEqual(request['proto'], '1')
        self.assertEqual(request['token'], SECRET)
        self.assertEqual(request['body']['cascadeIds'], [CONV])
        self.assertIs(request['body']['mergeAnnotations'], True)
        self.assertIs(request['body']['annotations']['archived'], True)
        self.assertNotIn('title', request['body']['annotations'])  # merge: the title is left alone

    def test_archive_by_mail_resolves_full_and_short_ids_from_the_sidecar_log(self):
        ls = self.fake()
        for ref in (MAIL, 'c2e317'):
            code, out, err = self.run_cli('--mail', ref)
            self.assertEqual(code, 0, err)
            self.assertIn(CONV, out)
        self.assertEqual({r['body']['cascadeIds'][0] for r in ls.requests}, {CONV})

    def test_mail_that_opened_nothing_or_failed_is_refused_without_calling_the_ls(self):
        ls = self.fake()
        for ref in ('541831', 'ffffff', '20261107T202928110Z-541831', 'x-follow'):
            code, _, err = self.run_cli('--mail', ref)
            self.assertEqual(code, 11, ref)
            self.assertIn('opened no conversation', err)
        self.assertEqual(ls.requests, [])

    def test_short_mail_id_under_six_characters_never_matches_by_suffix(self):
        ls = self.fake()
        code, _, err = self.run_cli('--mail', '17')
        self.assertEqual(code, 11)
        self.assertEqual(ls.requests, [])

    def test_conversation_the_relay_did_not_open_needs_allow_foreign(self):
        ls = self.fake()
        code, _, err = self.run_cli('--conversation', OTHER)
        self.assertEqual(code, 11)
        self.assertIn('--allow-foreign', err)
        self.assertEqual(ls.requests, [])
        code, _, _ = self.run_cli('--conversation', OTHER, '--allow-foreign')
        # LS fake wrote OTHER's annotation, so the read-back passes
        self.assertEqual(code, 0)
        self.assertEqual(ls.requests[0]['body']['cascadeIds'], [OTHER])

    def test_malformed_conversation_id_is_refused(self):
        ls = self.fake()
        for bad in ('../x', CONV.upper(), 'not-a-uuid'):
            code, _, err = self.run_cli('--conversation', bad, '--allow-foreign')
            self.assertEqual(code, 11, bad)
        self.assertEqual(ls.requests, [])

    def test_ls_error_is_reported_and_the_token_never_appears_in_output(self):
        self.fake(status=403, body=('{"message":"bad csrf %s"}' % SECRET).encode())
        code, out, err = self.run_cli('--mail', 'c2e317')
        self.assertEqual(code, 11)
        self.assertIn('HTTP 403', err)
        self.assertNotIn(SECRET, out + err)

    def test_http_200_without_an_archived_annotation_is_a_failure_not_success(self):
        self.fake(rewrite=False)
        ticks = iter(range(0, 10000))
        with mock.patch.object(arch.time, 'sleep', lambda s: None),                 mock.patch.object(arch.time, 'monotonic', lambda: next(ticks)):
            code, _, err = self.run_cli('--mail', 'c2e317')
        self.assertEqual(code, 11)
        self.assertIn('does not show archived:true', err)

    def test_already_archived_conversation_reports_whether_the_timestamp_was_refreshed(self):
        self.annotation.write_text('title:"t" archived:true archival_status_timestamp:{seconds:1000 nanos:1}',
                                   encoding='utf-8')
        self.fake()
        code, out, _ = self.run_cli('--mail', 'c2e317', '--json')
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertTrue(report['timestamp_refreshed'])
        self.assertNotEqual(report['before']['archived_at'], report['after']['archived_at'])

    def test_no_live_ls_is_a_clear_error(self):
        (self.gemini / 'daemon' / 'hub_1.json').write_text(
            json.dumps({'pid': 2 ** 22 + 12345, 'httpPort': 1, 'csrfToken': SECRET}), encoding='utf-8')
        code, _, err = self.run_cli('--mail', 'c2e317')
        self.assertEqual(code, 11)
        self.assertIn('no running Antigravity language server', err)
        self.assertNotIn(SECRET, err)

    def test_environment_address_and_token_win_over_the_daemon_files(self):
        ls = FakeLS(self.gemini)
        self.addCleanup(ls.close)
        with mock.patch.dict(os.environ, {'ANTIGRAVITY_LS_ADDRESS': f'127.0.0.1:{ls.port}',
                                          'ANTIGRAVITY_CSRF_TOKEN': 'env-token'}):
            found = arch.discover_ls(self.gemini)
            self.assertEqual(found[0], (f'http://127.0.0.1:{ls.port}', 'env-token'))
            code, _, err = self.run_cli('--mail', 'c2e317')
        self.assertEqual(code, 0, err)
        self.assertEqual(ls.requests[0]['token'], 'env-token')

    def test_failing_first_candidate_falls_through_to_the_next(self):
        ls = self.fake()
        with mock.patch.dict(os.environ, {'ANTIGRAVITY_LS_ADDRESS': '127.0.0.1:1',
                                          'ANTIGRAVITY_CSRF_TOKEN': 'env-token'}):
            code, _, err = self.run_cli('--mail', 'c2e317')
        self.assertEqual(code, 0, err)
        self.assertEqual(ls.requests[0]['token'], SECRET)


class ReadAnnotationTests(unittest.TestCase):
    def test_parses_archived_flag_and_timestamp_like_the_real_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'annotations').mkdir()
            (Path(tmp) / 'annotations' / f'{CONV}.pbtxt').write_text(
                'title:"[Relay] 20261107T192645949Z-c2e317" archived:true archival_status_timestamp:'
                '{seconds:1794120329 nanos:926815900} last_user_view_time:{seconds:1794120120 nanos:48000000}',
                encoding='utf-8')
            self.assertEqual(arch.read_annotation(CONV, Path(tmp)),
                             {'archived': True, 'archived_at': 1794120329, 'title': '[Relay] 20261107T192645949Z-c2e317'})
            self.assertEqual(arch.read_annotation(OTHER, Path(tmp)), {})


if __name__ == '__main__':
    unittest.main()
