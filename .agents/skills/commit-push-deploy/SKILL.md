---
name: commit-push-deploy
description: Commit, push, and deploy Hermes to agent-sachi. Use when asked to ship this repo, sync the server, or restart its gateway after changes.
compatibility: Requires Git, Bash, SSH access to agent-sachi, and its systemd user gateway service.
---

# Commit, push, and deploy

Commit the intended changes in the current repository worktree, push its branch, sync the exact commit to `agent-sachi`, and restart the Hermes gateway. Run this workflow when the user requests deployment; creating or editing this skill alone does not request a deployment.

## Deployment target

- SSH host: `agent-sachi`
- Checkout: `/home/sinkerine/hermes-agent`
- Service: `systemctl --user … hermes-gateway.service`
- Service executable: `/home/sinkerine/hermes-agent/.hermes/bin/hermes gateway run`

These paths were verified on the host. Check them again if the host setup changes. The server's `origin` may differ from the local repository's; fetch from the local push destination explicitly.

## Procedure

1. Read the repository and affected area instructions. Inspect `git status --short`, the staged and unstaged diffs, the current branch, and its remotes. Work in the current feature worktree. Do not merge into `main` as part of deploying.
2. Review the intended changes and run the relevant checks required by the repository. Python tests use `scripts/run_tests.sh`; run `python scripts/check` when required by the change. Do not bypass a failing check without an established explanation and user authorization.
3. Check the server before committing or pushing:

   ```bash
   ssh -o BatchMode=yes -o ConnectTimeout=10 agent-sachi 'git -C /home/sinkerine/hermes-agent status --porcelain --untracked-files=all; systemctl --user show hermes-gateway.service -p WorkingDirectory -p ExecStart -p ActiveState -p SubState'
   ```

   If the checkout has changes, stop and report the paths. Preserve server edits and state; never automatically stash, clean, or reset them. Confirm the service still uses the target checkout. Check any local-only server commits before switching away from them, and report commits at risk of becoming unreachable.
4. Stage only changes belonging to the user's task, inspect `git diff --cached`, generate a useful commit message, and commit. Do not ask the user to supply a message. If the task is already committed, deploy that commit without creating an empty commit. Leave unrelated local changes alone; explain that they are excluded from the deployment. Resolve an ambiguous change selection with the user.
5. Select the intended push remote (normally `origin`) and obtain the branch and SHA. Push explicitly, without force. Stop on a rejected push; reconcile it through the normal repository workflow before continuing.

   ```bash
   deploy_branch=$(git symbolic-ref --quiet --short HEAD)
   deploy_sha=$(git rev-parse HEAD)
   deploy_url=$(git remote get-url --push origin)
   git push origin "HEAD:refs/heads/$deploy_branch"
   ```

   Run each step only after the preceding one succeeds. A detached local HEAD needs an explicit destination branch from existing task context or the user. If the remote has multiple push destinations, resolve which one the server should fetch before deploying.
6. Sync and restart with the following Bash block, using the values from the successful push. It fetches the pushed branch from that destination, checks its SHA before changing the checkout, then checks out the exact commit detached. Keeping the deployment detached avoids moving the server's local branches.

   ```bash
   printf -v deploy_command 'bash -s -- %q %q %q' "$deploy_sha" "$deploy_branch" "$deploy_url"
   ssh -o BatchMode=yes -o ConnectTimeout=10 agent-sachi "$deploy_command" <<'REMOTE'
   set -euo pipefail
   expected_sha=$1
   branch=$2
   source_url=$3
   cd /home/sinkerine/hermes-agent
   if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
       echo 'Server checkout has changes; deployment stopped.' >&2
       exit 1
   fi
   previous_sha=$(git rev-parse HEAD)
   printf 'Previous deployment: %s\n' "$previous_sha"
   git fetch --no-tags "$source_url" "refs/heads/$branch"
   fetched_sha=$(git rev-parse FETCH_HEAD)
   if [[ "$fetched_sha" != "$expected_sha" ]]; then
       echo 'Pushed branch changed; deployment stopped before checkout.' >&2
       exit 1
   fi
   git checkout --detach "$expected_sha"
   test "$(git rev-parse HEAD)" = "$expected_sha"
   systemctl --user restart hermes-gateway.service
   systemctl --user is-active --quiet hermes-gateway.service
   systemctl --user show hermes-gateway.service -p ActiveState -p SubState -p MainPID -p ExecMainStatus
   printf 'Deployed commit: %s\n' "$(git rev-parse HEAD)"
   REMOTE
   ```

   Dependency changes require the repository's supported Hermes package-management workflow in the server checkout before restarting. Inspect `pm/AGENTS.md` and the changed dependency files; do not use raw pip/uv or replace the existing runtime/configuration. The block above handles source-only deployments.
7. Verify again in a separate SSH call after restart: confirm `git rev-parse HEAD`, service activity, and a live `MainPID`. Inspect recent service logs with `journalctl --user -u hermes-gateway.service --since '<restart time>' --no-pager` for startup failures. Avoid exposing credentials or private message content from logs. Service activity alone does not prove a platform connection is healthy; check startup evidence for the configured adapters and report any unverified readiness.
8. Report the branch, pushed SHA, deployment host/path, previous SHA, checks performed, and gateway status. If anything fails, state the exact last successful stage and whether the server checkout or running gateway changed. Never claim success after a failed command.

## Failure handling

Stop after a failed commit, push, fetch, checkout, or restart and inspect the cause. Do not force-push, discard server edits, overwrite configuration, delete runtime data, or repeatedly restart a failing service. Preserve the previous deployment SHA from the output. If rollback is needed, explain the failure and use that SHA only within the user's authorized recovery scope; verify the service again after rollback.
