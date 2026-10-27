#!/usr/bin/env python3
"""GPU spectator and OBS broadcaster for agent-wow.

python3 viewer.py --launch --stream CHANNEL   Launch and broadcast to Twitch.
python3 viewer.py --launch --level-target orc warrior 10 --stream CHANNEL
                                            Run, follow, broadcast, and link the benchmark.
python3 viewer.py --resume                    Continue the saved launch without a duplicate job.
python3 viewer.py JOB_ID --debug              Debug a spectator through DCV.
python3 viewer.py --record-test               Verify OBS/NVENC without broadcasting.
python3 viewer.py                Prepare the Windows desktop without a game.
python3 viewer.py --stop         Terminate the Windows viewer.
python3 viewer.py --build-client Build/reuse the preinstalled Windows client AMI.
"""
import argparse
from datetime import datetime
import getpass
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parent
REGION = 'us-west-2'
GPU_INSTANCE_TYPE = 'g4dn.xlarge'
NVIDIA_DRIVER = '596.86__grid_win10_win11_server2022_server2025_dch_64bit_international_aws_swl.exe'
agent = None


def load_agent(directory):
    directory = Path(directory).resolve()
    if not (directory / 'launch.py').is_file():
        raise RuntimeError(f'agent-wow checkout not found at {directory}; use --agent-dir PATH.')
    os.environ['PATH'] = os.pathsep.join([str(directory / 'bin'), '/opt/homebrew/bin', os.environ.get('PATH', '')])
    sys.path.insert(0, str(directory))
    import launch
    if Path(launch.__file__).resolve().parent != directory:
        raise RuntimeError('A different agent-wow checkout is already loaded.')
    return launch


def outputs(directory):
    tofu = shutil.which(os.environ.get('WOW_TOFU', 'tofu'))
    if not tofu:
        raise RuntimeError('OpenTofu not found; install tofu or set WOW_TOFU.')
    return {key: item['value'] for key, item in json.loads(subprocess.check_output(
        [tofu, 'output', '-json'], cwd=directory)).items()}


def upload(path, bucket, key):
    subprocess.run(['aws', 's3', 'cp', str(path), f's3://{bucket}/{key}',
                    '--region', REGION, '--only-show-errors'], check=True)


def instances(name):
    return agent.aws(REGION, 'ec2', 'describe-instances', '--filters',
               f'Name=tag:Name,Values={name}', 'Name=instance-state-name,Values=pending,running',
               '--query', 'Reservations[].Instances[].InstanceId')


def start(template, subnet, user_data, image=None, extra=()):
    (ROOT / 'tmp').mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', prefix='viewer-user-data-', suffix='.txt', dir=ROOT / 'tmp', delete=False) as file:
        file.write(user_data)
        path = Path(file.name)
    args = ['ec2', 'run-instances', '--launch-template', f'LaunchTemplateId={template},Version=$Latest',
            '--subnet-id', subnet, '--user-data', f'file://{path}', '--count', '1',
            '--query', 'Instances[0].InstanceId']
    if image:
        args += ['--image-id', image]
    args += list(extra)
    # A launch is a mutation: never automatically repeat an uncertain outcome.
    try:
        return agent.aws_request(REGION, *args)
    finally:
        path.unlink(missing_ok=True)


def start_gpu(config, user_data, image, extra=()):
    subnets = list(dict.fromkeys(config.get('viewer_subnets') or [config['viewer_subnet']]))
    for index, subnet in enumerate(subnets):
        try:
            return start(config['viewer_launch_template'], subnet, user_data, image,
                         extra=['--instance-type', GPU_INSTANCE_TYPE, *extra])
        except agent.AwsError as error:
            # Only an explicit capacity rejection proves that no VM was launched.
            if 'InsufficientInstanceCapacity' not in str(error) or index == len(subnets) - 1:
                raise
            print('No T4 capacity in this zone; trying the next configured project subnet.', flush=True)


def build(config):
    existing = instances('agent-wow-world-build')
    if existing:
        raise RuntimeError(f'A worldserver builder already exists: {existing[0]}.')
    prefix = 'viewer/build/' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    script = (ROOT / 'viewer/build-worldserver.sh').read_text()
    env = dict(REGION=REGION, BUCKET=config['results_bucket'], BUILD_PREFIX=prefix,
               WORLD_IMAGE=config['world_watch_image'])
    import shlex
    lines = '\n'.join(f'export {key}={shlex.quote(value)}' for key, value in env.items())
    script = script.replace('set -Eeuo pipefail', 'set -Eeuo pipefail\n' + lines, 1)
    instance = start(config['world_build_template'], config['viewer_subnet'], script)
    state = dict(instance_id=instance, prefix=prefix, world_image=env['WORLD_IMAGE'])
    (ROOT / 'tmp/viewer-build.json').write_text(json.dumps(state, indent=2) + '\n')
    print(f'Builder {instance}; logs: s3://{env["BUCKET"]}/{prefix}/build.log', flush=True)


def client_recipe():
    digest = hashlib.sha256()
    for name in ('prepare-image.ps1', 'client.torrent'):
        digest.update(name.encode() + b'\0' + (ROOT / 'viewer' / name).read_bytes())
    return digest.hexdigest()


def client_images(recipe):
    return agent.aws(REGION, 'ec2', 'describe-images', '--owners', 'self', '--filters',
                     'Name=tag:Project,Values=agent-wow', f'Name=tag:ViewerImageRecipe,Values={recipe}',
                     'Name=state,Values=pending,available', '--query',
                     'Images[].{id:ImageId,state:State,created:CreationDate}')


def bootstrap(config, name, recipe):
    # Immutable keys prevent another launch from replacing an in-flight build's input.
    path = ROOT / 'viewer' / name
    key = f'viewer/bootstrap/{hashlib.sha256(path.read_bytes()).hexdigest()}/{name}'
    upload(path, config['results_bucket'], key)
    if name == 'windows.ps1':
        obs = ROOT / 'viewer/obs.ps1'
        obs_key = f'viewer/bootstrap/{hashlib.sha256(obs.read_bytes()).hexdigest()}/obs.ps1'
        upload(obs, config['results_bucket'], obs_key)
        watch = ROOT / 'viewer/watch.ps1'
        watch_key = f'viewer/bootstrap/{hashlib.sha256(watch.read_bytes()).hexdigest()}/watch.ps1'
        upload(watch, config['results_bucket'], watch_key)
        # The saved image already has AWS CLI. Its instance role avoids signed URLs
        # expiring with the operator's temporary credentials during Windows setup.
        return ('<powershell>\n$ErrorActionPreference="Stop"\n'
                "$aws='C:\\Program Files\\Amazon\\AWSCLIV2\\aws.exe'\n"
                f"& $aws s3 cp 's3://{config['results_bucket']}/{obs_key}' C:\\viewer\\obs.ps1 --region {REGION} --only-show-errors\n"
                "if ($LASTEXITCODE) { throw 'Could not download OBS runtime.' }\n"
                f"& $aws s3 cp 's3://{config['results_bucket']}/{watch_key}' C:\\viewer\\watch.ps1 --region {REGION} --only-show-errors\n"
                "if ($LASTEXITCODE) { throw 'Could not download camera runtime.' }\n"
                f"& $aws s3 cp 's3://{config['results_bucket']}/{key}' C:\\viewer\\windows.ps1 --region {REGION} --only-show-errors\n"
                "if ($LASTEXITCODE) { throw 'Could not download viewer runtime.' }\n"
                f"& C:\\viewer\\windows.ps1 -Bucket '{config['results_bucket']}' -Region '{REGION}' -Recipe '{recipe}'\n"
                '</powershell>\n<detach>true</detach>')
    url = subprocess.check_output(['aws', 's3', 'presign', f's3://{config["results_bucket"]}/{key}',
                                   '--region', REGION, '--expires-in', '3600'], text=True).strip()
    return ('<powershell>\n$ErrorActionPreference="Stop"\n'
            '[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12\n'
            'New-Item -ItemType Directory -Force C:\\viewer | Out-Null\n'
            f"Invoke-WebRequest -UseBasicParsing '{url}' -OutFile C:\\viewer\\{name}\n"
            f"& C:\\viewer\\{name} -Bucket '{config['results_bucket']}' -Region '{REGION}' -Recipe '{recipe}'\n"
            '</powershell>\n<detach>true</detach>')


def read_status(bucket, key):
    try:
        return agent.aws(REGION, 's3', 'cp', f's3://{bucket}/{key}', '-', '--only-show-errors')
    except agent.AwsError as error:
        if not any(code in str(error) for code in ('404', 'Not Found', 'NoSuchKey')):
            raise
        return None


def wait_image(image):
    for _ in range(240):
        try:
            state = agent.aws(REGION, 'ec2', 'describe-images', '--image-ids', image,
                              '--query', 'Images[0].State')
        except agent.AwsError as error:
            if 'InvalidAMIID.NotFound' not in str(error):
                raise
            state = None  # EC2 image registration is eventually consistent.
        if state == 'available':
            return
        if state not in (None, 'pending'):
            raise RuntimeError(f'Client AMI {image} is {state}; inspect it in EC2.')
        time.sleep(15)
    raise RuntimeError(f'Client AMI {image} is still pending; rerun --build-client to resume.')


def build_client(config, base_image=None):
    recipe = client_recipe()
    images = client_images(recipe)
    builders = agent.aws(REGION, 'ec2', 'describe-instances', '--filters',
                         'Name=tag:Name,Values=agent-wow-viewer-build',
                         f'Name=tag:ViewerImageRecipe,Values={recipe}',
                         'Name=instance-state-name,Values=pending,running,stopping,stopped',
                         '--query', 'Reservations[].Instances[].InstanceId')
    if len(builders) > 1:
        raise RuntimeError(f'Multiple client builders exist: {", ".join(builders)}. Terminate the extras first.')
    if images:
        image = max(images, key=lambda item: item['created'])['id']
    else:
        if builders:
            instance = builders[0]
        else:
            if base_image:
                base = agent.aws(REGION, 'ec2', 'describe-images', '--owners', 'self',
                                 '--image-ids', base_image, '--filters',
                                 'Name=tag:Project,Values=agent-wow', 'Name=tag:ClientBuild,Values=12340',
                                 'Name=state,Values=available', '--query', 'Images[].ImageId')
                if base != [base_image]:
                    raise RuntimeError('The base client image must be an available, owned agent-wow client AMI.')
                base = base_image
            else:
                base = agent.aws(REGION, 'ssm', 'get-parameter', '--name',
                                 '/aws/service/ami-windows-latest/Windows_Server-2022-English-Full-Base',
                                 '--query', 'Parameter.Value')
            # Stage the pinned, signed GRID installer where the restricted viewer role can read it.
            subprocess.run(['aws', 's3', 'cp', f's3://ec2-windows-nvidia-drivers/latest/{NVIDIA_DRIVER}',
                            f's3://{config["results_bucket"]}/viewer/bootstrap/{NVIDIA_DRIVER}',
                            '--region', REGION, '--copy-props', 'none', '--only-show-errors'], check=True)
            upload(ROOT / 'viewer/client.torrent', config['results_bucket'],
                   f'viewer/bootstrap/{recipe}/client.torrent')
            script = bootstrap(config, 'prepare-image.ps1', recipe)
            tags = [{'Key': 'Name', 'Value': 'agent-wow-viewer-build'},
                    {'Key': 'Project', 'Value': 'agent-wow'}, {'Key': 'ViewerImageRecipe', 'Value': recipe}]
            instance = start_gpu(config, script, base,
                             extra=['--instance-initiated-shutdown-behavior', 'stop',
                                    '--client-token', uuid.uuid4().hex, '--tag-specifications',
                                    json.dumps([{'ResourceType': 'instance', 'Tags': tags}])])
            builders = [instance]
        prefix = f'viewer/image-build/{instance}'
        print(f'Client builder {instance}; logs: s3://{config["results_bucket"]}/{prefix}/setup.log', flush=True)
        print('Installing once, rebooting, and preparing Windows for reuse. --build-client can resume this build.', flush=True)
        for _ in range(720):
            status = read_status(config['results_bucket'], f'{prefix}/status.json')
            state = agent.aws(REGION, 'ec2', 'describe-instances', '--instance-ids', instance,
                              '--query', 'Reservations[0].Instances[0].State.Name')
            if status and status.get('status') == 'failed':
                raise RuntimeError(f'Client build {instance} failed: {status.get("error")}. Logs: s3://{config["results_bucket"]}/{prefix}/setup.log')
            if state == 'stopped':
                if not status or status.get('status') != 'prepared' or status.get('recipe') != recipe:
                    raise RuntimeError(f'Builder {instance} stopped before completing setup; inspect its setup log. No image was created.')
                break
            if state in ('shutting-down', 'terminated'):
                raise RuntimeError(f'Client builder {instance} was terminated before imaging.')
            time.sleep(15)
        else:
            raise RuntimeError(f'Client builder {instance} is not ready after three hours; rerun --build-client to resume.')
        tags = [{'Key': 'Name', 'Value': 'agent-wow-viewer-client'},
                {'Key': 'Project', 'Value': 'agent-wow'}, {'Key': 'ViewerImageRecipe', 'Value': recipe},
                {'Key': 'ClientBuild', 'Value': '12340'}]
        image = agent.aws_request(REGION, 'ec2', 'create-image', '--instance-id', instance,
                                  # EC2's unique AMI name also prevents two resumed controllers
                                  # from capturing the same builder twice.
                                  '--name', f'agent-wow-viewer-{recipe[:12]}-{instance}',
                                  '--description', 'Windows viewer with WoW 3.3.5a build 12340 and graphics/streaming dependencies',
                                  '--no-reboot', '--tag-specifications', json.dumps([
                                      {'ResourceType': resource, 'Tags': tags} for resource in ('image', 'snapshot')]),
                                  '--query', 'ImageId')
        print(f'Saving client AMI {image}...', flush=True)
    wait_image(image)
    if builders:
        agent.aws_request(REGION, 'ec2', 'terminate-instances', '--instance-ids', *builders,
                          '--query', 'TerminatingInstances[].InstanceId')
    print(f'Client AMI ready: {image}. Future viewers reuse this image.', flush=True)
    return image


def windows(config):
    found = instances('agent-wow-viewer')
    if len(found) > 1:
        raise RuntimeError('Multiple Windows viewers exist; stop the extras before continuing.')
    if found:
        return found[0]
    recipe = client_recipe()
    available = [item for item in client_images(recipe) if item['state'] == 'available']
    if not available:
        raise RuntimeError('No matching preinstalled client image. Run python3 viewer.py --build-client first.')
    image = max(available, key=lambda item: item['created'])['id']
    script = bootstrap(config, 'windows.ps1', recipe)
    print(f'Starting viewer from preinstalled client AMI {image}.', flush=True)
    return start_gpu(config, script, image, extra=['--client-token', uuid.uuid4().hex])


def attach(job_id, instance, config):
    for _ in range(180):
        job, = agent.aws(REGION, 'batch', 'describe-jobs', '--jobs', job_id, '--query',
                   'jobs[].{status:status,host:container.containerInstanceArn,task:container.taskArn}')
        if job['status'] in ('FAILED', 'SUCCEEDED'):
            raise RuntimeError(f'The game job is already {job["status"]}.')
        if job.get('host'):
            cluster = job['task'].split('/')[1]
            host = agent.aws(REGION, 'ecs', 'describe-container-instances', '--cluster', cluster,
                       '--container-instances', job['host'], '--query', 'containerInstances[0].ec2InstanceId')
            path = ROOT / 'tmp/viewer-job.json'
            path.write_text(json.dumps(dict(job_id=job_id, game_instance=host)))
            upload(path, config['results_bucket'], f'viewer/{instance}/job.json')
            return
        time.sleep(5)
    raise RuntimeError(f'Job {job_id} is still queued. Run viewer.py {job_id} when it starts.')


def wait_ready(instance, config):
    print('Waiting for the preinstalled client desktop...', flush=True)
    for _ in range(120):
        failure = read_status(config['results_bucket'], f'viewer/{instance}/failed.json')
        if failure:
            raise RuntimeError(f'Viewer {instance} failed: {failure.get("error")}')
        if read_status(config['results_bucket'], f'viewer/{instance}/ready.json'):
            return
        time.sleep(10)
    raise RuntimeError('Viewer desktop did not start within 20 minutes; inspect setup.log and play.log through SSM.')


def connect(instance):
    if not shutil.which('session-manager-plugin'):
        raise RuntimeError('Install the AWS Session Manager plugin before opening the viewer.')
    print(f'Windows viewer {instance}. Waiting for remote access...', flush=True)
    for _ in range(180):
        ready = agent.aws(REGION, 'ssm', 'describe-instance-information', '--filters',
                    f'Key=InstanceIds,Values={instance}', '--query', 'InstanceInformationList[].PingStatus')
        if ready == ['Online']:
            break
        time.sleep(5)
    else:
        raise RuntimeError('Windows has not registered with Session Manager yet.')
    print('Browser address: https://127.0.0.1:8443/#console', flush=True)
    return subprocess.call(['aws', 'ssm', 'start-session', '--region', REGION, '--target', instance,
                            '--document-name', 'AWS-StartPortForwardingSession',
                            '--parameters', 'portNumber=8443,localPortNumber=8443'])


def job_definition(config, harness):
    definition = config.get('job_definitions', {}).get(harness)
    if definition:
        return definition
    # Older state predates job_definitions. Follow the shared launcher's current
    # account-backed definition names instead of reviving a legacy API-key job.
    definition = getattr(agent, 'JOB_DEFINITIONS', {}).get(harness)
    if definition:
        return definition
    # Older agent-wow state has a dedicated Codex viewer definition.
    if harness == 'codex' and config.get('viewer_job_definition'):
        return config['viewer_job_definition']
    raise RuntimeError(f'No {harness} viewer job definition in OpenTofu outputs; update the agent-wow infrastructure first.')


def twitch_secret_name(channel):
    channel = channel.lower()
    if not re.fullmatch(r'[a-z0-9_]{3,25}', channel):
        raise ValueError('Use a Twitch channel login, such as tuckerbullmorgan, without a URL.')
    return f'agent-wow/viewer/twitch/{channel}'


def configure_twitch(channel):
    name = twitch_secret_name(channel)
    key = getpass.getpass(f'Twitch stream key for {channel} (hidden): ').strip()
    if not re.fullmatch(r'live_[A-Za-z0-9_]+', key):
        raise ValueError('Expected a Twitch primary stream key beginning with live_.')
    try:
        agent.aws(REGION, 'secretsmanager', 'describe-secret', '--secret-id', name)
        operation, data = 'put-secret-value', {'SecretId': name, 'SecretString': key}
    except agent.AwsError as error:
        if 'ResourceNotFoundException' not in str(error):
            raise
        operation, data = 'create-secret', {'Name': name, 'SecretString': key,
                                           'Tags': [{'Key': 'Project', 'Value': 'agent-wow'}]}
    # AWS CLI's parameter loader does not reliably support /dev/stdin on macOS.
    # NamedTemporaryFile creates mode 0600; the secret is never in argv or the checkout.
    with tempfile.NamedTemporaryFile(mode='w', prefix='viewer-twitch-', suffix='.json') as file:
        json.dump(data, file)
        file.flush()
        result = subprocess.run(['aws', 'secretsmanager', operation, '--region', REGION,
                                 '--cli-input-json', f'file://{file.name}', '--output', 'json', '--no-cli-pager'],
                                text=True, capture_output=True)
    if result.returncode:
        detail = (result.stderr or '').replace(key, '[redacted]').strip()
        raise RuntimeError(f'Could not save the Twitch key: {detail}')
    print(f'Twitch key saved in Secrets Manager: {name}.')


def stream_request(instance, config, mode, channel=None):
    request = {'id': uuid.uuid4().hex, 'mode': mode, 'expires_at': int(time.time()) + 300}
    if channel:
        request['channel'] = channel.lower()
        request['secret'] = agent.aws(REGION, 'secretsmanager', 'describe-secret',
                                      '--secret-id', twitch_secret_name(channel), '--query', 'ARN')
    path = ROOT / 'tmp/stream-request.json'
    path.write_text(json.dumps(request))
    upload(path, config['results_bucket'], f'viewer/{instance}/stream-request.json')
    expected = 'complete' if mode in ('record', 'test') else 'stopped' if mode == 'stop' else 'active'
    print(f'OBS {mode} requested on {instance}.', flush=True)
    for _ in range(60):
        status = read_status(config['results_bucket'], f'viewer/{instance}/stream-status.json')
        if status and status.get('state') == 'failed' and status.get('request_id') in (None, request['id']):
            raise RuntimeError(f'OBS failed: {status.get("error")}')
        if status and status.get('request_id') == request['id'] and status.get('state') == expected:
            if mode == 'live' and (status.get('output', {}).get('outputReconnecting') or
                                   not status.get('output', {}).get('outputBytes', 0)):
                time.sleep(5)
                continue
            print(json.dumps(status, indent=2), flush=True)
            return status
        time.sleep(5)
    raise RuntimeError('OBS did not confirm the request within five minutes; inspect stream-status.json via S3.')


def watch_request(instance, config, character):
    if not re.fullmatch(r'[A-Za-z]{2,12}', character):
        raise ValueError('Use a character name containing 2–12 letters.')
    request = {'id': uuid.uuid4().hex, 'character': character, 'expires_at': int(time.time()) + 300}
    path = ROOT / 'tmp/watch-request.json'
    path.write_text(json.dumps(request))
    upload(path, config['results_bucket'], f'viewer/{instance}/watch-request.json')
    for _ in range(60):
        status = read_status(config['results_bucket'], f'viewer/{instance}/watch-status.json')
        if status and status.get('request_id') == request['id']:
            if status.get('state') == 'failed':
                raise RuntimeError(f'Camera request failed: {status.get("error")}')
            if status.get('state') == 'submitted':
                print(f'Spectator camera command submitted for {character}.', flush=True)
                return status
        time.sleep(5)
    raise RuntimeError('Camera command submission was not acknowledged within five minutes.')


def ensure_available(instance, config, job_id=None):
    """Never retarget a viewer that belongs to another unfinished run."""
    attached = read_status(config['results_bucket'], f'viewer/{instance}/job.json')
    if attached and attached.get('job_id') != job_id:
        status = agent.aws(REGION, 'batch', 'describe-jobs', '--jobs', attached['job_id'],
                           '--query', 'jobs[0].status')
        if status not in ('FAILED', 'SUCCEEDED', None):
            raise RuntimeError(f"Viewer is busy with {attached['job_id']}; resume that run or wait for it to finish.")
        # Its desktop still has the old account/session and a pending shutdown.
        raise RuntimeError('The previous viewer is shutting down. Wait for it to terminate before launching another run.')


def character_records(message):
    """Both CLI harnesses retain the client's exact creation response."""
    records = message.splitlines()
    try:
        blocks = json.loads(message)
        if isinstance(blocks, list):
            records += [line for block in blocks if isinstance(block, dict) and block.get('type') == 'text'
                        and isinstance(block.get('text'), str) for line in block['text'].splitlines()]
    except ValueError:
        pass
    return records


def discover_character(job_id, timeout=1800):
    """Consume complete client creation records, not names guessed from the prompt."""
    print('Waiting for the agent to create its character...', flush=True)
    deadline = time.monotonic() + timeout
    token = stream = None
    names = set()
    while time.monotonic() < deadline:
        job = agent.aws(REGION, 'batch', 'describe-jobs', '--jobs', job_id, '--query',
                        'jobs[0].{status:status,log:container.logStreamName}')
        if not job or job['status'] in ('FAILED', 'SUCCEEDED'):
            raise RuntimeError('The agent run ended before its spectator camera was ready.')
        if job.get('log'):
            if stream != job['log']:
                stream, token = job['log'], None
            caught_up = False
            for _ in range(20):
                args = ['logs', 'get-log-events', '--log-group-name', '/aws/batch/agent-wow',
                        '--log-stream-name', stream, '--start-from-head']
                if token:
                    args += ['--next-token', token]
                try:
                    page = agent.aws(REGION, *args)
                except agent.AwsError as error:
                    if 'ResourceNotFoundException' in str(error):
                        break
                    raise
                for event in page.get('events', []):
                    for line in character_records(event['message']):
                        match = re.fullmatch(r'Created character: ([A-Za-z]{2,12})', line)
                        if match and match[1].lower() != 'observer':
                            names.add(match[1])
                new_token = page['nextForwardToken']
                caught_up = new_token == token
                token = new_token
                if caught_up:
                    break
            if len(names) > 1:
                raise RuntimeError('The run created multiple characters; resume with --watch-character NAME.')
            if names and caught_up:
                character = next(iter(names))
                print(f'Agent character: {character}', flush=True)
                return character
        time.sleep(5)
    raise RuntimeError('No character creation was confirmed within 30 minutes. Resume when the agent is ready.')


def wait_obs(instance, config):
    print('Waiting for OBS game capture...', flush=True)
    for _ in range(120):
        status = read_status(config['results_bucket'], f'viewer/{instance}/stream-status.json')
        if status and status.get('state') in ('ready', 'active', 'complete', 'stopped'):
            return status
        if status and status.get('state') == 'failed':
            raise RuntimeError(f'OBS startup failed: {status.get("error")}')
        time.sleep(5)
    raise RuntimeError('OBS did not become ready within ten minutes.')


def live_output(status, job_id, channel):
    try:
        # Windows emits seven fractional digits; macOS Python 3.9 accepts six.
        stamp = re.sub(r'(\.\d{6})\d+', r'\1', status['updated_at']).replace('Z', '+00:00')
        age = time.time() - datetime.fromisoformat(stamp).timestamp()
        output = status.get('output', {})
        return (0 <= age <= 90 and status.get('mode') == 'live' and status.get('state') == 'active'
                and status.get('job_id') == job_id and status.get('channel') == channel.lower()
                and output.get('outputActive') and not output.get('outputReconnecting') and bool(output.get('outputBytes')))
    except (KeyError, TypeError, ValueError):
        return False


def link_benchmark(path, state):
    from benchmark_results import post
    benchmark = json.loads(Path(path).read_text())
    status = post(benchmark['url'], benchmark['key'], dict(
        kind='stream', channel=state['channel'].lower(), job_id=state['job_id'],
        instance_id=state['instance_id'], character=state['character'],
        harness=state['harness'], model=state['model'], effort=state['effort']))
    if status != 'stream linked':
        raise RuntimeError('The benchmark API did not confirm the Twitch link.')
    print('Twitch stream linked to benchmark run ' + hashlib.sha256(benchmark['key'].encode()).hexdigest(), flush=True)


def main():
    global agent, REGION
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job_id', nargs='?')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--build', action='store_true')
    mode.add_argument('--build-client', action='store_true', help='Build or reuse the matching Windows client AMI.')
    parser.add_argument('--base-client-image', help='Reuse the client files from an owned client AMI when building.')
    mode.add_argument('--launch', action='store_true')
    mode.add_argument('--resume', action='store_true', help='Resume the saved launch without submitting another job.')
    mode.add_argument('--stop', action='store_true')
    mode.add_argument('--configure-twitch', metavar='CHANNEL', help='Save a Twitch key using a hidden terminal prompt.')
    mode.add_argument('--stream-stop', action='store_true', help='Stop OBS output on the existing viewer.')
    output = parser.add_mutually_exclusive_group()
    output.add_argument('--record-test', action='store_true', help='Record 60 seconds with NVENC; upload private test artifacts.')
    output.add_argument('--twitch-test', metavar='CHANNEL', help='Send a 60-second Twitch Inspector test; not publicly viewable.')
    output.add_argument('--stream', metavar='CHANNEL', help='Start a public Twitch broadcast from OBS on the viewer.')
    parser.add_argument('--debug', action='store_true', help='Open the private DCV debugging tunnel.')
    parser.add_argument('--watch-character', metavar='NAME', help='Point the spectator camera at this character.')
    benchmark = parser.add_mutually_exclusive_group()
    benchmark.add_argument('--benchmark', type=Path, help='Private benchmark config; its prompt and run ID are preserved.')
    benchmark.add_argument('--level-target', nargs=3, metavar=('RACE', 'CLASS', 'LEVEL'),
                           help='Create a new level-target benchmark as part of this launch.')
    parser.add_argument('--harness', choices=('codex', 'claude'), default='codex')
    parser.add_argument('--agent-dir', type=Path, default=ROOT.parent / 'agent-wow',
                        help='agent-wow checkout owning the launcher and OpenTofu state.')
    parser.add_argument('--prompt', help='Task override; benchmark configs already specify their task.')
    args = parser.parse_args()
    if args.base_client_image and not args.build_client:
        parser.error('--base-client-image requires --build-client')
    if args.job_id and (args.launch or args.resume):
        parser.error('Use an existing job ID or --launch/--resume, not both.')
    if args.level_target and not args.launch:
        parser.error('--level-target requires --launch')
    if args.benchmark and not (args.launch or args.resume):
        parser.error('--benchmark requires --launch or --resume so its run cannot be attached to an unrelated job.')
    if args.watch_character and not re.fullmatch(r'[A-Za-z]{2,12}', args.watch_character):
        parser.error('Character names must contain 2–12 letters.')
    streaming = args.record_test or args.twitch_test or args.stream
    if streaming and (args.stop or args.build or args.build_client or args.configure_twitch or args.stream_stop):
        parser.error('Output options cannot be combined with build, configure or stop modes.')
    if args.stream or args.twitch_test or args.configure_twitch:
        twitch_secret_name(args.stream or args.twitch_test or args.configure_twitch)
    args.agent_dir = args.agent_dir.resolve()
    agent = load_agent(args.agent_dir)
    (ROOT / 'tmp').mkdir(exist_ok=True)
    config = outputs(args.agent_dir)
    REGION = config['region']
    if args.configure_twitch:
        configure_twitch(args.configure_twitch)
        return 0
    if args.stream_stop:
        found = instances('agent-wow-viewer')
        if len(found) != 1:
            raise RuntimeError('Expected exactly one running viewer to stop OBS output.')
        stream_request(found[0], config, 'stop')
        return 0
    if args.build:
        build(config)
        return 0
    if args.build_client:
        build_client(config, args.base_client_image)
        return 0
    if args.stop:
        found = instances('agent-wow-viewer')
        if found:
            agent.aws(REGION, 'ec2', 'terminate-instances', '--instance-ids', *found,
                '--query', 'TerminatingInstances[].InstanceId')
            print('Windows viewer terminated.')
        return 0
    state_path = ROOT / 'tmp/viewer-state.json'
    if args.resume:
        state = json.loads(state_path.read_text())
        instance, job_id = state['instance_id'], state['job_id']
        if not job_id:
            raise RuntimeError('No job was recorded; inspect the saved viewer before submitting a new run.')
        if instance not in instances('agent-wow-viewer'):
            raise RuntimeError('The saved viewer is no longer running.')
        args.harness = state.get('harness', args.harness)
        state['harness'] = args.harness
        if args.benchmark and state.get('benchmark') and args.benchmark.resolve() != Path(state['benchmark']).resolve():
            parser.error('The saved launch already belongs to a different benchmark config.')
        args.benchmark = Path(state['benchmark']) if state.get('benchmark') else args.benchmark
        if not (args.record_test or args.twitch_test):
            args.stream = args.stream or state.get('channel')
        streaming = args.record_test or args.twitch_test or args.stream
    else:
        # Validate task/benchmark input and credentials before creating compute.
        if args.stream or args.twitch_test:
            agent.aws(REGION, 'secretsmanager', 'describe-secret', '--secret-id',
                      twitch_secret_name(args.stream or args.twitch_test), '--query', 'ARN')
        if args.level_target:
            from race_classes import RACE_CLASSES
            race, character_class, level = args.level_target
            if character_class not in RACE_CLASSES.get(race, ()) or not level.isdigit() or not 1 <= int(level) <= 80:
                parser.error('Choose a supported race/class and a level from 1 to 80.')
            if args.prompt:
                parser.error('--level-target fixes the benchmark prompt.')
        prompt = args.prompt if args.prompt is not None else (None if args.benchmark or args.level_target else
                  'Create one orc warrior and complete the starting area.')
        overrides = agent.container_overrides(prompt, args.harness, args.benchmark) if args.launch else None
        definition = job_definition(config, args.harness) if args.launch else None
        instance = windows(config)
        ensure_available(instance, config, args.job_id)
        job_id = args.job_id
        state = dict(instance_id=instance, job_id=job_id, harness=args.harness,
                     channel=args.stream, benchmark=str(args.benchmark.resolve()) if args.benchmark else None)
        from agent_common import model_settings
        state['model'], state['effort'] = model_settings(args.harness)
    if args.launch:
        if args.level_target:
            from benchmark_results import create_run
            args.benchmark = args.agent_dir / 'config' / ('viewer-' + uuid.uuid4().hex + '.json')
            create_run(race, character_class, int(level), args.benchmark)
            state['benchmark'] = str(args.benchmark)
            overrides = agent.container_overrides(None, args.harness, args.benchmark)
        overrides['environment'] += [
            {'name': 'AGENT_WOW_VIEWER', 'value': '1'},
            {'name': 'AGENT_WOW_WORLD_IMAGE', 'value': config['world_watch_image']},
        ]
        state_path.write_text(json.dumps(state, indent=2)+'\n')
        job_id = agent.submit_job(REGION, 'agent-wow-viewer', definition,
                            overrides, config['viewer_job_queue'])['jobId']
        state['job_id'] = job_id
        print(f'Game job: {job_id}', flush=True)
    state_path.write_text(json.dumps(state, indent=2)+'\n')
    print('Launch saved. If interrupted, rerun with --resume.', flush=True)
    if job_id:
        if 'model' not in state or 'effort' not in state:
            from agent_common import harness_spec
            spec = harness_spec(state['harness'])
            env = agent.aws(REGION, 'batch', 'describe-jobs', '--jobs', job_id, '--query',
                f"jobs[0].container.environment[?name=='{spec['model_env']}' || name=='{spec['effort_env']}']")
            settings = {item['name']: item['value'] for item in env}
            state['model'], state['effort'] = settings[spec['model_env']], settings[spec['effort_env']]
        if args.benchmark:
            # In particular, a resumed older launch may only link an exact task.
            benchmark_data = json.loads(args.benchmark.read_text())
            command = agent.aws(REGION, 'batch', 'describe-jobs', '--jobs', job_id, '--query', 'jobs[0].container.command')
            if command != ['run-wow-agent', benchmark_data['prompt']]:
                raise RuntimeError('The benchmark prompt does not match this game job; no link was published.')
            state['benchmark'] = str(args.benchmark.resolve())
        attach(job_id, instance, config)
    wait_ready(instance, config)
    if job_id or args.watch_character:
        character = args.watch_character or state.get('character') or discover_character(job_id)
        watch_request(instance, config, character)
        state['character'] = character
        state_path.write_text(json.dumps(state, indent=2)+'\n')
    if streaming:
        status = wait_obs(instance, config)
        if not (args.stream and live_output(status, job_id, args.stream)):
            stream_request(instance, config, 'record' if args.record_test else 'test' if args.twitch_test else 'live',
                           args.stream or args.twitch_test)
        if args.stream:
            state['channel'] = args.stream.lower()
            state_path.write_text(json.dumps(state, indent=2)+'\n')
            if args.benchmark:
                link_benchmark(args.benchmark, state)
            print('Live: https://www.twitch.tv/' + state['channel'], flush=True)
    if args.debug:
        return connect(instance)
    print(f'Viewer {instance} is running. Add --debug to connect through DCV.', flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        if '--build-client' in sys.argv:
            print('\nClient builder/image retained in AWS. Rerun --build-client to finish and clean up the builder.')
        else:
            print('\nDisconnected. The viewer and game continue; the viewer terminates after the game ends.')
    except Exception as error:
        raise SystemExit(str(error))
