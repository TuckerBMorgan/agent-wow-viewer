import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import viewer

ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = Path(os.environ.get('WOW_AGENT_DIR', ROOT.parent / 'agent-wow')).resolve()


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ViewerTests(unittest.TestCase):
    def test_viewer_import_does_not_load_the_agent_launcher(self):
        subprocess.run([sys.executable, '-I', '-c',
                        f'import sys; sys.path.insert(0, {str(ROOT)!r}); '
                        "import viewer; assert 'launch' not in sys.modules"], check=True)

    def test_explicit_checkout_supplies_the_shared_launcher(self):
        agent = viewer.load_agent(AGENT_DIR)
        self.assertEqual(Path(agent.__file__).resolve(), AGENT_DIR / 'launch.py')
        self.assertEqual(set(agent.JOB_DEFINITIONS), {'codex', 'claude'})
        self.assertEqual(os.environ['PATH'].split(os.pathsep)[0], str(AGENT_DIR / 'bin'))

    def test_account_runs_enable_the_spectator_with_the_selected_harness(self):
        agent = viewer.load_agent(AGENT_DIR)
        config = {'region': 'us-west-2', 'job_definitions': {'codex': 'codex-definition', 'claude': 'claude-definition'},
                  'viewer_job_queue': 'viewer-queue', 'world_watch_image': 'world-image'}
        for harness in ('codex', 'claude'):
            with self.subTest(harness=harness), tempfile.TemporaryDirectory() as directory, \
                    patch.object(viewer, 'ROOT', Path(directory)), \
                    patch.object(viewer, 'outputs', return_value=config), \
                    patch.object(viewer, 'load_agent', return_value=agent), \
                    patch.object(viewer, 'agent', agent, create=True), \
                    patch.object(viewer, 'windows', return_value='viewer-instance'), \
                    patch.object(viewer, 'wait_ready'), patch.object(viewer, 'attach') as attach, \
                    patch.object(viewer, 'connect', return_value=0), \
                    patch.object(agent, 'submit_job', return_value={'jobId': 'account-job'}) as submit, \
                    patch.dict(os.environ, {}, clear=True), \
                    patch.object(sys, 'argv', ['viewer.py', '--launch', '--harness', harness,
                                             '--prompt', 'reach level 20']), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(viewer.main(), 0)
            args = submit.call_args.args
            self.assertEqual(args[2], f'{harness}-definition')
            self.assertEqual(args[3]['command'], ['run-wow-agent', 'reach level 20'])
            settings = {item['name']: item['value'] for item in args[3]['environment']}
            self.assertEqual(settings['AGENT_WOW_HARNESS'], harness)
            self.assertEqual(settings['AGENT_WOW_VIEWER'], '1')
            self.assertEqual(settings['AGENT_WOW_WORLD_IMAGE'], 'world-image')
            attach.assert_called_once_with('account-job', 'viewer-instance', config)

    def test_completed_job_is_not_attached(self):
        agent = MagicMock()
        agent.aws.return_value = [{'status': 'SUCCEEDED'}]
        with patch.object(viewer, 'agent', agent, create=True), patch.object(viewer, 'upload') as upload:
            with self.assertRaisesRegex(RuntimeError, 'already SUCCEEDED'):
                viewer.attach('job', 'viewer', {})
            upload.assert_not_called()

    def test_windows_uses_assets_from_the_viewer_checkout(self):
        config = {'results_bucket': 'bucket', 'viewer_launch_template': 'template', 'viewer_subnet': 'subnet'}
        with patch.object(viewer, 'agent', MagicMock(), create=True), \
                patch.object(viewer, 'instances', return_value=[]), \
                patch.object(viewer, 'upload') as upload, \
                patch.object(viewer.subprocess, 'check_output', return_value='https://example.test/bootstrap'), \
                patch.object(viewer, 'start', return_value='instance'):
            self.assertEqual(viewer.windows(config), 'instance')
        self.assertEqual([call.args[0] for call in upload.call_args_list],
                         [ROOT / 'viewer/windows.ps1', ROOT / 'viewer/client.torrent'])

    def test_existing_viewer_is_reused(self):
        with patch.object(viewer, 'instances', return_value=['i-existing']), patch.object(viewer, 'start') as start:
            self.assertEqual(viewer.windows({}), 'i-existing')
            start.assert_not_called()

    def test_multiple_viewers_are_rejected(self):
        with patch.object(viewer, 'instances', return_value=['i-one', 'i-two']):
            with self.assertRaisesRegex(RuntimeError, 'Multiple Windows'):
                viewer.windows({})


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.instructions = self.root / 'AGENTS.md'
        self.instructions.write_text('Play fairly.\n')
        self.aws = MagicMock()
        self.docker = MagicMock()
        self.world = MagicMock()
        self.world.attach_socket.return_value._sock.recv.return_value = b'You change security level of account WATCHER to 2.'
        self.docker.from_env.return_value.containers.list.return_value = [self.world]
        with patch.dict(sys.modules, {'boto3': self.aws, 'docker': self.docker}):
            self.accounts = load_module('prepare_wow_account', AGENT_DIR / 'docker/prepare_wow_account.py')
            with patch.dict(sys.modules, {'prepare_wow_account': self.accounts}):
                self.module = load_module('prepare_viewer', ROOT / 'docker/prepare_viewer.py')

    def test_observer_creation_uses_separate_account_and_no_agent_mutations(self):
        results = [subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0),
                   subprocess.CompletedProcess([], 0, '{"characters":[]}'), subprocess.CompletedProcess([], 0)]
        with patch.object(self.module.subprocess, 'run', side_effect=results) as run, \
                patch.object(self.module.time, 'sleep'), \
                patch.dict('os.environ', {'AWS_RESULTS_BUCKET': 'bucket', 'AWS_BATCH_JOB_ID': 'job'}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.module.prepare_account(self.root / 'observer', self.instructions)
        sent = ''.join(call.args[0].decode() for call in self.world.attach_socket.return_value._sock.sendall.call_args_list)
        self.assertIn('account create WATCHER ', sent)
        self.assertIn('account set gmlevel WATCHER 2 -1', sent)
        self.assertNotIn('AGENT', sent)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs['env']['AGENT_WOW_CONFIG_DIR'], str(self.root / 'observer'))
        exported = self.aws.client.return_value.put_object.call_args.kwargs
        self.assertEqual(exported['Key'], 'viewer/job/account.json')
        self.assertEqual(json.loads(exported['Body'])['character'], 'Observer')
        self.assertEqual(len(json.loads(exported['Body'])['password']), 16)

    def test_saved_observer_is_not_duplicated(self):
        results = [subprocess.CompletedProcess([], 0),
                   subprocess.CompletedProcess([], 0, '{"characters":[{"name":"Observer"}]}')]
        with patch.object(self.module.subprocess, 'run', side_effect=results) as run, \
                patch.dict('os.environ', {'AWS_RESULTS_BUCKET': 'bucket', 'AWS_BATCH_JOB_ID': 'job'}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.module.prepare_account(self.root / 'observer', self.instructions)
        self.assertEqual(run.call_count, 2)
        sent = self.world.attach_socket.return_value._sock.sendall.call_args.args[0].decode()
        self.assertEqual(sent, 'account set gmlevel WATCHER 2 -1\n')

    def test_unconfirmed_permissions_prevent_exporting_an_unusable_observer(self):
        self.world.attach_socket.return_value._sock.recv.return_value = b''
        with patch.object(self.module.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)), \
                patch.dict('os.environ', {'AWS_RESULTS_BUCKET': 'bucket', 'AWS_BATCH_JOB_ID': 'job'}):
            with self.assertRaisesRegex(RuntimeError, 'permissions were not confirmed'):
                self.module.prepare_account(self.root / 'observer', self.instructions)
        self.aws.client.return_value.put_object.assert_not_called()


if __name__ == '__main__':
    unittest.main()
