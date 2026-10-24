#!/usr/bin/env python3
"""Browser spectator for agent-wow. See README.md for infrastructure setup.

python3 viewer.py --launch       Start a fresh orc warrior run and watch it.
python3 viewer.py JOB_ID         Watch an existing spectator-enabled run.
python3 viewer.py                Prepare the Windows desktop without a game.
python3 viewer.py --stop         Terminate the Windows viewer.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
REGION = 'us-west-2'
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


def start(template, subnet, user_data, image=None):
    with tempfile.NamedTemporaryFile(mode='w', prefix='viewer-user-data-', suffix='.txt', dir=ROOT / 'tmp', delete=False) as file:
        file.write(user_data)
        path = Path(file.name)
    args = ['ec2', 'run-instances', '--launch-template', f'LaunchTemplateId={template},Version=$Latest',
            '--subnet-id', subnet, '--user-data', f'file://{path}', '--count', '1',
            '--query', 'Instances[0].InstanceId']
    if image:
        args += ['--image-id', image]
    # A launch is a mutation: never automatically repeat an uncertain outcome.
    try:
        return agent.aws_request(REGION, *args)
    finally:
        path.unlink(missing_ok=True)


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
    instance = start(config['world_build_template'], config['world_build_subnet'], script)
    state = dict(instance_id=instance, prefix=prefix, world_image=env['WORLD_IMAGE'])
    (ROOT / 'tmp/viewer-build.json').write_text(json.dumps(state, indent=2) + '\n')
    print(f'Builder {instance}; logs: s3://{env["BUCKET"]}/{prefix}/build.log', flush=True)


def windows(config):
    found = instances('agent-wow-viewer')
    if len(found) > 1:
        raise RuntimeError('Multiple Windows viewers exist; stop the extras before continuing.')
    if found:
        return found[0]
    key = 'viewer/bootstrap/windows.ps1'
    upload(ROOT / 'viewer/windows.ps1', config['results_bucket'], key)
    upload(ROOT / 'viewer/client.torrent', config['results_bucket'], 'viewer/bootstrap/client.torrent')
    url = subprocess.check_output(['aws', 's3', 'presign', f's3://{config["results_bucket"]}/{key}',
                                   '--region', REGION, '--expires-in', '3600'], text=True).strip()
    script = ('<powershell>\n$ErrorActionPreference="Stop"\n'
              '[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12\n'
              f"Invoke-WebRequest -UseBasicParsing '{url}' -OutFile C:\\Windows\\Temp\\viewer.ps1\n"
              f"& C:\\Windows\\Temp\\viewer.ps1 -Bucket '{config['results_bucket']}' -Region '{REGION}'\n"
              '</powershell>\n<detach>true</detach>')
    image = agent.aws(REGION, 'ssm', 'get-parameter', '--name',
                '/aws/service/ami-windows-latest/Windows_Server-2022-English-Full-Base',
                '--query', 'Parameter.Value')
    return start(config['viewer_launch_template'], config['viewer_subnet'], script, image)


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
    print('Waiting for Windows to finish installing the client...', flush=True)
    for _ in range(360):
        try:
            agent.aws(REGION, 's3api', 'head-object', '--bucket', config['results_bucket'],
                '--key', f'viewer/{instance}/ready.json', '--query', 'ContentLength')
            return
        except agent.AwsError as error:
            if not any(code in str(error) for code in ('404', 'Not Found', 'NoSuchKey')):
                raise
        time.sleep(10)
    raise RuntimeError('Windows setup did not finish within one hour; inspect its setup.log through SSM.')


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
    print('Windows installs the client and software renderer on first boot. Reconnect after its setup reboot.', flush=True)
    return subprocess.call(['aws', 'ssm', 'start-session', '--region', REGION, '--target', instance,
                            '--document-name', 'AWS-StartPortForwardingSession',
                            '--parameters', 'portNumber=8443,localPortNumber=8443'])


def main():
    global agent, REGION
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job_id', nargs='?')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--build', action='store_true')
    mode.add_argument('--launch', action='store_true')
    mode.add_argument('--stop', action='store_true')
    parser.add_argument('--harness', choices=('codex', 'claude'), default='codex')
    parser.add_argument('--agent-dir', type=Path, default=ROOT.parent / 'agent-wow',
                        help='agent-wow checkout owning the launcher and OpenTofu state.')
    parser.add_argument('--prompt', default='Create one orc warrior and complete the starting area.')
    args = parser.parse_args()
    args.agent_dir = args.agent_dir.resolve()
    agent = load_agent(args.agent_dir)
    (ROOT / 'tmp').mkdir(exist_ok=True)
    config = outputs(args.agent_dir)
    REGION = config['region']
    if args.build:
        build(config)
        return 0
    if args.stop:
        found = instances('agent-wow-viewer')
        if found:
            agent.aws(REGION, 'ec2', 'terminate-instances', '--instance-ids', *found,
                '--query', 'TerminatingInstances[].InstanceId')
            print('Windows viewer terminated.')
        return 0
    instance = windows(config)
    job_id = args.job_id
    if args.launch:
        wait_ready(instance, config)
        overrides = agent.container_overrides(args.prompt, args.harness)
        overrides['environment'] += [
            {'name': 'AGENT_WOW_VIEWER', 'value': '1'},
            {'name': 'AGENT_WOW_WORLD_IMAGE', 'value': config['world_watch_image']},
        ]
        definition = config['job_definitions'][args.harness]
        job_id = agent.submit_job(REGION, 'agent-wow-viewer-test', definition,
                            overrides, config['viewer_job_queue'])['jobId']
        print(f'Game job: {job_id}', flush=True)
    (ROOT / 'tmp/viewer-state.json').write_text(json.dumps(dict(instance_id=instance, job_id=job_id), indent=2)+'\n')
    if job_id:
        attach(job_id, instance, config)
    return connect(instance)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('\nDisconnected. The viewer and game continue; the viewer terminates after the game ends.')
    except Exception as error:
        raise SystemExit(str(error))
