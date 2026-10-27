# agent-wow-viewer

Watch an agent-wow character in a full Windows WoW client streamed to Twitch through OBS, with a private DCV connection for debugging.
This add-on was extracted from [TuckerBMorgan/agent-wow](https://github.com/TuckerBMorgan/agent-wow), starting from commit `931ac51e505cba363884e9dac4a33e9dad51c966`.

Keep the two checkouts next to each other:

```text
agent-wow/          # agent launcher, job history, Dockerfile, OpenTofu state
agent-wow-viewer/   # browser viewer, Windows setup, mod-watch image builder
```

The viewer reuses the agent checkout's launcher, model settings, and job database.
Use `--agent-dir /path/to/agent-wow` if it lives elsewhere. Both checkouts need the viewer extraction changes.

## Setup

Install Python 3, the AWS CLI, the AWS Session Manager plugin, and OpenTofu. Sign in to AWS.
The agent repository consumes `terraform/` as a pinned child module; **all OpenTofu commands and state stay in `agent-wow`**.

```sh
cd ../agent-wow
tofu init
tofu plan
# Review the plan before applying it. The viewer extraction should only move addresses.
tofu apply

cd ../agent-wow-viewer
python3 viewer.py --build
python3 viewer.py --build-client
```

`--build` starts a temporary Linux instance that builds the pinned AzerothCore + mod-watch image, pushes it to ECR, saves build status/logs to S3, and shuts down. Wait for its `status.json` to report exit code 0 before launching a viewer run. The existing image can be reused.

`--build-client` installs WoW 3.3.5a build 12340, NVIDIA graphics drivers, OBS, Amazon DCV, and the connection dependencies on a temporary Windows instance. It reboots, runs Sysprep, saves a private AMI and its EBS snapshot, and terminates the builder once the image is available. The AMI stays in your AWS account for future launches; its snapshot incurs storage charges. No game credentials or desktop user are saved in it.

The command waits for completion and can be rerun after a terminal interruption to resume the same build. A matching saved image is reused. Images are matched by a hash of the installation script and client torrent, so changing either requires another `--build-client`; changing the runtime script does not. Build status and logs are stored under `viewer/image-build/INSTANCE_ID/` in the results bucket. Failed builders remain available for diagnosis; terminate them before retrying the build.

The agent Dockerfile downloads this repository's spectator account helper at a pinned commit. That helper reuses `prepare_wow_account.py` already in the agent image. Rebuild the agent image when either runtime helper changes.

## Watch

```sh
# Save the Twitch key once using a hidden prompt:
python3 viewer.py --configure-twitch CHANNEL

# Launch, automatically follow the character, and broadcast:
python3 viewer.py --launch --stream CHANNEL

# Include a new level-target benchmark and its live-stream link:
python3 viewer.py --launch --level-target orc warrior 10 --stream CHANNEL

# Recover an interrupted launch without submitting another job:
python3 viewer.py --resume

# Launch a fresh run and open its private browser connection:
python3 viewer.py --launch --harness codex --debug --prompt 'Create an orc warrior and complete the starting zone.'

# Or connect to an existing spectator-enabled job:
python3 viewer.py JOB_ID --debug

# Shut down the Windows viewer:
python3 viewer.py --stop
```

With `--debug`, open `https://127.0.0.1:8443/#console` while the command is connected. Each new viewer starts from the matching preinstalled AMI, creates a fresh desktop user, and reboots into the client. The launcher waits for the desktop before opening the tunnel, so no setup reconnect is needed. If no matching image exists, it asks you to run `--build-client` first. Ctrl+C disconnects your browser tunnel without stopping the game. The Windows viewer shuts down after the attached game ends. `--stop` stops only the viewer; it retains the saved AMI.

The viewer account is separate from the agent's regular player account. Game ports and the browser stream use private Session Manager tunnels. No public inbound ports are opened.

Terminal job status and agent logs remain in `agent-wow`: use `watch_jobs.py` or `launch.py --watch JOB_ID` there.

## Tests

```sh
python3 -B -m unittest discover -s tests
```

Tests use mocked services and the sibling `agent-wow` checkout. Set `WOW_AGENT_DIR` to use another checkout. They do not launch jobs or Windows instances.
