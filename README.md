# agent-wow-viewer

Watch an agent-wow character in a full Windows WoW client streamed to your browser.
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
```

`--build` starts a temporary Linux instance that builds the pinned AzerothCore + mod-watch image, pushes it to ECR, saves build status/logs to S3, and shuts down. Wait for its `status.json` to report exit code 0 before launching a viewer run. The existing image can be reused.

The agent Dockerfile downloads this repository's spectator account helper at a pinned commit. That helper reuses `prepare_wow_account.py` already in the agent image. Rebuild the agent image when either runtime helper changes.

## Watch

```sh
# Launch a fresh run and open its private browser connection:
python3 viewer.py --launch --harness codex --prompt 'Create an orc warrior and complete the starting zone.'

# Or connect to an existing spectator-enabled job:
python3 viewer.py JOB_ID

# Shut down the Windows viewer:
python3 viewer.py --stop
```

Open `https://127.0.0.1:8443/#console` while the command is connected. First boot installs the Windows client and restarts Windows; reconnect afterward if necessary. Ctrl+C disconnects your browser tunnel without stopping the game. The Windows viewer shuts down after the attached game ends. `--stop` stops only the viewer.

The viewer account is separate from the agent's regular player account. Game ports and the browser stream use private Session Manager tunnels. No public inbound ports are opened.

Terminal job status and agent logs remain in `agent-wow`: use `watch_jobs.py` or `launch.py --watch JOB_ID` there.

## Tests

```sh
python3 -B -m unittest discover -s tests
```

Tests use mocked services and the sibling `agent-wow` checkout. Set `WOW_AGENT_DIR` to use another checkout. They do not launch jobs or Windows instances.
