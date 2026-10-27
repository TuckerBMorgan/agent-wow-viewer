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
                    patch.object(viewer, 'ensure_available'), \
                    patch.object(viewer, 'discover_character', return_value='Gorvash'), \
                    patch.object(viewer, 'watch_request'), \
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

    def test_legacy_codex_viewer_definition_is_supported_without_using_it_for_claude(self):
        config = {'viewer_job_definition': 'legacy-codex-viewer'}
        with patch.object(viewer, 'agent', None):
            self.assertEqual(viewer.job_definition(config, 'codex'), 'legacy-codex-viewer')
            with self.assertRaisesRegex(RuntimeError, 'No claude'):
                viewer.job_definition(config, 'claude')
        config['job_definitions'] = {'codex': 'current-codex'}
        self.assertEqual(viewer.job_definition(config, 'codex'), 'current-codex')

    def test_old_state_uses_current_account_launcher_before_legacy_api_key_job(self):
        config = {'viewer_job_definition': 'legacy-api-key-viewer'}
        launcher = viewer.load_agent(AGENT_DIR)
        with patch.object(viewer, 'agent', launcher):
            for harness in ('codex', 'claude'):
                self.assertEqual(viewer.job_definition(config, harness), launcher.JOB_DEFINITIONS[harness])

    def test_windows_uses_assets_from_the_viewer_checkout(self):
        config = {'results_bucket': 'bucket', 'viewer_launch_template': 'template', 'viewer_subnet': 'subnet'}
        with patch.object(viewer, 'agent', MagicMock(), create=True), \
                patch.object(viewer, 'instances', return_value=[]), \
                patch.object(viewer, 'client_images', return_value=[
                    {'id': 'ami-ready', 'state': 'available', 'created': '2026-10-25'},
                    {'id': 'ami-pending', 'state': 'pending', 'created': '2026-10-26'}]), \
                patch.object(viewer, 'upload') as upload, \
                patch.object(viewer.subprocess, 'check_output', return_value='https://example.test/bootstrap') as presign, \
                patch.object(viewer, 'start', return_value='instance') as start:
            self.assertEqual(viewer.windows(config), 'instance')
        self.assertEqual([call.args[0] for call in upload.call_args_list],
                         [ROOT / 'viewer/windows.ps1', ROOT / 'viewer/obs.ps1', ROOT / 'viewer/watch.ps1'])
        self.assertEqual(start.call_args.args[3], 'ami-ready')
        self.assertIn(viewer.client_recipe(), start.call_args.args[2])
        self.assertIn("s3://bucket/viewer/bootstrap/", start.call_args.args[2])
        self.assertNotIn('https://', start.call_args.args[2])
        presign.assert_not_called()

    def test_missing_image_does_not_silently_install_on_a_fresh_viewer(self):
        with patch.object(viewer, 'instances', return_value=[]), \
                patch.object(viewer, 'client_images', return_value=[]), patch.object(viewer, 'start') as start:
            with self.assertRaisesRegex(RuntimeError, '--build-client'):
                viewer.windows({})
            start.assert_not_called()

    def test_existing_viewer_is_reused(self):
        with patch.object(viewer, 'instances', return_value=['i-existing']), patch.object(viewer, 'start') as start:
            self.assertEqual(viewer.windows({}), 'i-existing')
            start.assert_not_called()

    def test_multiple_viewers_are_rejected(self):
        with patch.object(viewer, 'instances', return_value=['i-one', 'i-two']):
            with self.assertRaisesRegex(RuntimeError, 'Multiple Windows'):
                viewer.windows({})

    def test_gpu_capacity_falls_back_without_changing_instance_size(self):
        agent = viewer.load_agent(AGENT_DIR)
        config = {'viewer_launch_template': 'template', 'viewer_subnet': 'subnet-b',
                  'viewer_subnets': ['subnet-b', 'subnet-a']}
        with patch.object(viewer, 'agent', agent), patch.object(viewer, 'start', side_effect=[
                agent.AwsError('InsufficientInstanceCapacity', 255), 'instance']) as start:
            self.assertEqual(viewer.start_gpu(config, 'script', 'image'), 'instance')
        self.assertEqual([call.args[1] for call in start.call_args_list], ['subnet-b', 'subnet-a'])
        for call in start.call_args_list:
            self.assertEqual(call.kwargs['extra'], ['--instance-type', 'g4dn.xlarge'])

    def test_gpu_launch_does_not_retry_uncertain_or_permission_failures(self):
        agent = viewer.load_agent(AGENT_DIR)
        config = {'viewer_launch_template': 'template', 'viewer_subnet': 'subnet-b',
                  'viewer_subnets': ['subnet-b', 'subnet-a']}
        for message in ('Request timed out', 'UnauthorizedOperation'):
            with self.subTest(message=message), patch.object(viewer, 'agent', agent), \
                    patch.object(viewer, 'start', side_effect=agent.AwsError(message, 255)) as start:
                with self.assertRaises(agent.AwsError):
                    viewer.start_gpu(config, 'script', 'image')
                start.assert_called_once()


class ClientImageTests(unittest.TestCase):
    def setUp(self):
        self.agent = MagicMock()
        self.agent.AwsError = viewer.load_agent(AGENT_DIR).AwsError
        patcher = patch.object(viewer, 'agent', self.agent)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = {'results_bucket': 'bucket', 'viewer_launch_template': 'template', 'viewer_subnet': 'subnet'}

    def test_recipe_tracks_installation_and_client_but_not_runtime_changes(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(viewer, 'ROOT', Path(directory)):
            assets = Path(directory) / 'viewer'
            assets.mkdir()
            for name in ('prepare-image.ps1', 'client.torrent', 'windows.ps1'):
                (assets / name).write_text(name)
            original = viewer.client_recipe()
            (assets / 'windows.ps1').write_text('new runtime')
            self.assertEqual(viewer.client_recipe(), original)
            (assets / 'client.torrent').write_text('new client')
            updated = viewer.client_recipe()
            self.assertNotEqual(updated, original)
            (assets / 'prepare-image.ps1').write_text('new dependencies')
            self.assertNotEqual(viewer.client_recipe(), updated)

    def test_image_discovery_is_owned_and_recipe_specific(self):
        viewer.client_images('recipe')
        arguments = self.agent.aws.call_args.args
        self.assertIn('--owners', arguments)
        self.assertIn('self', arguments)
        self.assertIn('Name=tag:ViewerImageRecipe,Values=recipe', arguments)

    def test_completed_image_is_reused_without_starting_or_creating(self):
        self.agent.aws.return_value = []
        with patch.object(viewer, 'client_images', return_value=[{'id': 'ami-saved', 'created': 'now'}]), \
                patch.object(viewer, 'wait_image') as wait, patch.object(viewer, 'start') as start:
            self.assertEqual(viewer.build_client(self.config), 'ami-saved')
        wait.assert_called_once_with('ami-saved')
        start.assert_not_called()
        self.agent.aws_request.assert_not_called()

    def test_builder_stops_before_capture_and_is_terminated_only_after_image_is_available(self):
        events = []
        self.agent.aws.side_effect = [[], 'ami-base', 'running', 'stopped']
        def mutate(region, service, operation, *args):
            events.append(operation)
            return 'ami-new'
        self.agent.aws_request.side_effect = mutate
        with patch.object(viewer, 'client_images', return_value=[]), \
                patch.object(viewer, 'upload'), patch.object(viewer.subprocess, 'run'), \
                patch.object(viewer, 'bootstrap', return_value='script'), \
                patch.object(viewer, 'start', return_value='i-builder') as start, \
                patch.object(viewer, 'read_status', side_effect=[None, {'status': 'prepared', 'recipe': viewer.client_recipe()}]), \
                patch.object(viewer.time, 'sleep'), \
                patch.object(viewer, 'wait_image', side_effect=lambda image: events.append('available')):
            self.assertEqual(viewer.build_client(self.config), 'ami-new')
        self.assertEqual(events, ['create-image', 'available', 'terminate-instances'])
        self.assertEqual(start.call_args.args, ('template', 'subnet', 'script', 'ami-base'))
        self.assertIn('stop', start.call_args.kwargs['extra'])
        tags = json.loads(start.call_args.kwargs['extra'][-1])[0]['Tags']
        self.assertIn({'Key': 'Name', 'Value': 'agent-wow-viewer-build'}, tags)

    def test_interrupted_build_resumes_existing_builder(self):
        self.agent.aws.side_effect = [['i-builder'], 'stopped']
        self.agent.aws_request.return_value = 'ami-new'
        with patch.object(viewer, 'client_images', return_value=[]), \
                patch.object(viewer, 'start') as start, patch.object(viewer, 'wait_image'), \
                patch.object(viewer, 'read_status', return_value={'status': 'prepared', 'recipe': viewer.client_recipe()}):
            self.assertEqual(viewer.build_client(self.config), 'ami-new')
        start.assert_not_called()

    def test_failed_or_unverified_builder_is_never_captured(self):
        for status in (None, {'status': 'installed'}, {'status': 'failed', 'error': 'bad download'},
                       {'status': 'prepared', 'recipe': 'wrong-recipe'}):
            with self.subTest(status=status), patch.object(viewer, 'client_images', return_value=[]), \
                    patch.object(viewer, 'read_status', return_value=status):
                self.agent.aws.side_effect = [['i-builder'], 'stopped']
                with self.assertRaises(RuntimeError):
                    viewer.build_client(self.config)
                self.agent.aws_request.assert_not_called()

    def test_pending_capture_resumes_and_preserves_builder_on_wait_failure(self):
        self.agent.aws.return_value = ['i-builder']
        with patch.object(viewer, 'client_images', return_value=[{'id': 'ami-pending', 'created': 'now'}]), \
                patch.object(viewer, 'wait_image', side_effect=RuntimeError('pending')):
            with self.assertRaisesRegex(RuntimeError, 'pending'):
                viewer.build_client(self.config)
        self.agent.aws_request.assert_not_called()

    def test_status_distinguishes_missing_objects_from_auth_failure(self):
        self.agent.aws.side_effect = self.agent.AwsError('404 Not Found')
        self.assertIsNone(viewer.read_status('bucket', 'key'))
        self.agent.aws.side_effect = self.agent.AwsError('AccessDenied')
        with self.assertRaisesRegex(self.agent.AwsError, 'AccessDenied'):
            viewer.read_status('bucket', 'key')

    def test_image_wait_handles_registration_delay_but_not_failed_images(self):
        self.agent.aws.side_effect = [self.agent.AwsError('InvalidAMIID.NotFound'), 'pending', 'available']
        with patch.object(viewer.time, 'sleep') as sleep:
            viewer.wait_image('ami-new')
        self.assertEqual(sleep.call_count, 2)
        self.agent.aws.side_effect = ['failed']
        with self.assertRaisesRegex(RuntimeError, 'failed'):
            viewer.wait_image('ami-bad')

    def test_desktop_failure_prevents_waiting_or_submitting_a_game(self):
        with patch.object(viewer, 'read_status', return_value={'error': 'renderer failed'}), \
                patch.object(viewer.time, 'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, 'renderer failed'):
                viewer.wait_ready('instance', self.config)
        sleep.assert_not_called()


class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'tmp').mkdir()
        self.agent = MagicMock()
        self.agent.AwsError = viewer.load_agent(AGENT_DIR).AwsError
        for name, value in [('ROOT', self.root), ('agent', self.agent)]:
            patcher = patch.object(viewer, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_key_setup_uses_a_private_temporary_file_and_never_prints_the_key(self):
        key = 'live_test_sensitive_example'
        output = io.StringIO()
        paths = []
        def save(args, **kwargs):
            path = Path(args[args.index('--cli-input-json') + 1].removeprefix('file://'))
            paths.append(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())['SecretString'], key)
            return subprocess.CompletedProcess(args, 0)
        with patch.object(viewer.getpass, 'getpass', return_value=key), \
                patch.object(viewer.subprocess, 'run', side_effect=save) as run, \
                contextlib.redirect_stdout(output):
            viewer.configure_twitch('TuckerBullMorgan')
        self.assertNotIn(key, repr(run.call_args.args))
        self.assertNotIn(key, output.getvalue())
        self.assertFalse(paths[0].exists())
        self.assertEqual(list((self.root / 'tmp').iterdir()), [])

    def test_secret_lookup_failure_does_not_create_a_different_secret(self):
        self.agent.aws.side_effect = self.agent.AwsError('AccessDenied')
        with patch.object(viewer.getpass, 'getpass', return_value='live_test_example'), \
                patch.object(viewer.subprocess, 'run') as run:
            with self.assertRaises(self.agent.AwsError):
                viewer.configure_twitch('channel')
        run.assert_not_called()

    def test_local_recording_needs_no_secret_and_ignores_old_request_failures(self):
        def status(bucket, key):
            request = json.loads((self.root / 'tmp/stream-request.json').read_text())
            return {'request_id': request['id'], 'state': 'complete'}
        responses = iter([{'request_id': 'old', 'state': 'failed'}])
        with patch.object(viewer, 'upload'), patch.object(viewer.time, 'sleep'), \
                patch.object(viewer, 'read_status', side_effect=lambda *args: next(responses, None) or status(*args)):
            viewer.stream_request('instance', {'results_bucket': 'bucket'}, 'record')
        self.agent.aws.assert_not_called()
        request = json.loads((self.root / 'tmp/stream-request.json').read_text())
        self.assertNotIn('secret', request)
        self.assertGreater(request['expires_at'], viewer.time.time())

    def test_twitch_request_contains_only_the_secret_reference(self):
        arn = 'arn:aws:secretsmanager:us-west-2:123456789012:secret:agent-wow/viewer/twitch/channel-AbCd12'
        self.agent.aws.return_value = arn
        def status(*args):
            request = json.loads((self.root / 'tmp/stream-request.json').read_text())
            return {'request_id': request['id'], 'state': 'complete'}
        with patch.object(viewer, 'upload'), patch.object(viewer, 'read_status', side_effect=status):
            viewer.stream_request('instance', {'results_bucket': 'bucket'}, 'test', 'channel')
        request = json.loads((self.root / 'tmp/stream-request.json').read_text())
        self.assertEqual(request['secret'], arn)
        self.assertEqual(request['mode'], 'test')
        self.assertIn('describe-secret', self.agent.aws.call_args.args)
        self.assertNotIn('get-secret-value', self.agent.aws.call_args.args)

    def test_channel_cannot_inject_a_path_or_remote_command(self):
        for channel in ('https://twitch.tv/user', "user'; exit", '../user', 'a/b'):
            with self.subTest(channel=channel), self.assertRaises(ValueError):
                viewer.twitch_secret_name(channel)

    def test_camera_rejects_chat_and_keystroke_injection_before_upload(self):
        with patch.object(viewer, 'upload') as upload:
            for name in ('Gorvash{ENTER}', '.unwatch', 'a/b', 'x', 'two names'):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    viewer.watch_request('instance', {'results_bucket': 'bucket'}, name)
            upload.assert_not_called()

    def test_camera_waits_for_its_own_request_and_reports_only_submission(self):
        def status(*args):
            request = json.loads((self.root / 'tmp/watch-request.json').read_text())
            self.assertEqual(request['character'], 'Gorvash')
            self.assertGreater(request['expires_at'], viewer.time.time())
            return {'request_id': request['id'], 'state': 'submitted'}
        with patch.object(viewer, 'upload'), patch.object(viewer, 'read_status', side_effect=status):
            self.assertEqual(viewer.watch_request('instance', {'results_bucket': 'bucket'}, 'Gorvash')['state'], 'submitted')

    def test_discovery_drains_empty_pages_and_ignores_names_in_prose(self):
        self.agent.aws.side_effect = [
            {'status': 'RUNNING', 'log': 'stream'},
            {'events': [], 'nextForwardToken': 'one'},
            {'events': [{'message': 'I will create Gorvash\nCreated character: Observer\nCreated character: Gorvash'}], 'nextForwardToken': 'two'},
            {'events': [], 'nextForwardToken': 'two'},
        ]
        self.assertEqual(viewer.discover_character('job'), 'Gorvash')

    def test_discovery_does_not_guess_between_characters_or_follow_ended_jobs(self):
        self.agent.aws.side_effect = [
            {'status': 'RUNNING', 'log': 'stream'},
            {'events': [{'message': 'Created character: Gorvash\nCreated character: Throgar'}], 'nextForwardToken': 'one'},
            {'events': [], 'nextForwardToken': 'one'},
        ]
        with self.assertRaisesRegex(RuntimeError, 'multiple characters'):
            viewer.discover_character('job')
        self.agent.aws.side_effect = [{'status': 'FAILED'}]
        with self.assertRaisesRegex(RuntimeError, 'ended'):
            viewer.discover_character('job')

    def test_busy_viewer_is_not_retargeted_to_another_run(self):
        self.agent.aws.return_value = 'RUNNING'
        with patch.object(viewer, 'read_status', return_value={'job_id': 'existing'}):
            with self.assertRaisesRegex(RuntimeError, 'busy'):
                viewer.ensure_available('instance', {'results_bucket': 'bucket'}, 'new')
            viewer.ensure_available('instance', {'results_bucket': 'bucket'}, 'existing')

    def test_claude_tool_results_preserve_client_character_records(self):
        text = json.dumps([{'type': 'text', 'text': 'Created character: Gorvash\nRace: Orc'}])
        self.assertIn('Created character: Gorvash', viewer.character_records(text))

    def test_single_command_launch_orders_boot_submit_follow_stream_and_benchmark_link(self):
        launcher = viewer.load_agent(AGENT_DIR)
        benchmark = self.root / 'benchmark.json'
        benchmark.write_text(json.dumps({'url': 'https://example.test', 'key': 'a'*43, 'prompt': 'Reach level 2.'}))
        config = {'region': 'us-west-2', 'results_bucket': 'bucket', 'viewer_job_queue': 'queue',
                  'world_watch_image': 'world', 'job_definitions': {'codex': 'definition'}}
        order = []
        def step(name, value=None):
            return lambda *args, **kwargs: order.append(name) or value
        with contextlib.ExitStack() as stack:
            for name, value in [('outputs', config), ('load_agent', launcher)]:
                stack.enter_context(patch.object(viewer, name, return_value=value))
            stack.enter_context(patch.object(launcher, 'aws', side_effect=lambda *args:
                ['run-wow-agent', 'Reach level 2.'] if args[1] == 'batch' else 'arn:secret'))
            stack.enter_context(patch.object(launcher, 'submit_job', side_effect=step('submit', {'jobId': 'new-job'})))
            for name, value in [('windows', 'instance'), ('ensure_available', None), ('attach', None),
                                ('wait_ready', None), ('discover_character', 'Gorvash'), ('watch_request', None),
                                ('wait_obs', {'state': 'ready'}), ('stream_request', None), ('link_benchmark', None)]:
                stack.enter_context(patch.object(viewer, name, side_effect=step(name, value)))
            stack.enter_context(patch.object(sys, 'argv', ['viewer.py', '--launch', '--stream', 'channel',
                                                         '--benchmark', str(benchmark), '--agent-dir', str(AGENT_DIR)]))
            self.assertEqual(viewer.main(), 0)
        self.assertLess(order.index('submit'), order.index('wait_ready'))
        self.assertLess(order.index('watch_request'), order.index('stream_request'))
        self.assertLess(order.index('stream_request'), order.index('link_benchmark'))
        state = json.loads((self.root / 'tmp/viewer-state.json').read_text())
        self.assertEqual((state['job_id'], state['character'], state['benchmark']), ('new-job', 'Gorvash', str(benchmark.resolve())))

    def test_resume_uses_saved_job_and_never_submits_another(self):
        launcher = viewer.load_agent(AGENT_DIR)
        state = dict(instance_id='instance', job_id='saved-job', harness='codex', model='gpt-6.1-sol', effort='medium',
                     character='Gorvash', channel='channel', benchmark=None)
        (self.root / 'tmp/viewer-state.json').write_text(json.dumps(state))
        with contextlib.ExitStack() as stack:
            for name, value in [('load_agent', launcher), ('outputs', {'region': 'us-west-2'}),
                                ('instances', ['instance']), ('attach', None), ('wait_ready', None),
                                ('watch_request', None), ('wait_obs', {'state': 'active', 'mode': 'live',
                                 'job_id': 'saved-job', 'channel': 'channel', 'updated_at': viewer.datetime.now().astimezone().isoformat(),
                                 'output': {'outputActive': True, 'outputBytes': 20}})]:
                stack.enter_context(patch.object(viewer, name, return_value=value))
            submit = stack.enter_context(patch.object(launcher, 'submit_job'))
            output = stack.enter_context(patch.object(viewer, 'stream_request'))
            stack.enter_context(patch.object(sys, 'argv', ['viewer.py', '--resume', '--agent-dir', str(AGENT_DIR)]))
            self.assertEqual(viewer.main(), 0)
            submit.assert_not_called()
            output.assert_not_called()

    def test_level_target_creates_config_and_saves_job_before_attach_failure(self):
        launcher = viewer.load_agent(AGENT_DIR)
        import benchmark_results
        prompt = 'create a orc warrior and adventure until they are level 10'
        def create(race, character_class, level, path):
            self.assertEqual((race, character_class, level), ('orc', 'warrior', 10))
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps({'url': 'https://example.test', 'key': 'a'*43, 'prompt': prompt}))
        with contextlib.ExitStack() as stack:
            for name, value in [('load_agent', launcher), ('outputs', {'region': 'us-west-2',
                                 'viewer_job_queue': 'queue', 'world_watch_image': 'world',
                                 'job_definitions': {'codex': 'definition'}}),
                                ('windows', 'instance'), ('ensure_available', None)]:
                stack.enter_context(patch.object(viewer, name, return_value=value))
            stack.enter_context(patch.object(benchmark_results, 'create_run', side_effect=create))
            stack.enter_context(patch.object(launcher, 'aws', side_effect=lambda *args:
                ['run-wow-agent', prompt] if args[1] == 'batch' else 'arn:secret'))
            submit = stack.enter_context(patch.object(launcher, 'submit_job', return_value={'jobId': 'saved-job'}))
            stack.enter_context(patch.object(viewer, 'attach', side_effect=RuntimeError('temporary failure')))
            stack.enter_context(patch.object(sys, 'argv', ['viewer.py', '--launch', '--level-target', 'orc', 'warrior', '10',
                                                         '--stream', 'channel', '--agent-dir', str(self.root)]))
            with self.assertRaisesRegex(RuntimeError, 'temporary failure'):
                viewer.main()
        state = json.loads((self.root / 'tmp/viewer-state.json').read_text())
        self.assertEqual(state['job_id'], 'saved-job')
        self.assertTrue(Path(state['benchmark']).is_file())
        overrides = submit.call_args.args[3]
        self.assertEqual(overrides['command'], ['run-wow-agent', prompt])
        self.assertIn({'name': 'BENCHMARK_KEY', 'value': 'a'*43}, overrides['environment'])

    def test_old_or_different_stream_telemetry_cannot_skip_start(self):
        status = dict(mode='live', state='active', job_id='job', channel='channel',
                      updated_at=viewer.datetime.now().astimezone().isoformat(),
                      output={'outputActive': True, 'outputBytes': 20})
        self.assertTrue(viewer.live_output(status, 'job', 'channel'))
        status['updated_at'] = viewer.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.%f') + '0Z'
        self.assertTrue(viewer.live_output(status, 'job', 'channel'))
        self.assertFalse(viewer.live_output(status, 'another-job', 'channel'))
        self.assertFalse(viewer.live_output(status, 'job', 'another-channel'))
        status['updated_at'] = '2000-01-01T00:00:00Z'
        self.assertFalse(viewer.live_output(status, 'job', 'channel'))


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
