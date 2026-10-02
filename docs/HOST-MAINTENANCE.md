# Host maintenance standard

Fleet hosts are generic Docker infrastructure. They must receive operating-system security fixes automatically, report health, and clean only fleet-owned expired resources.

## Policy

- Enable the distribution's unattended **security** upgrades.
- Do not automatically replace controller, runner, Docker major-version, kernel, or project images without repository validation.
- Schedule reboots only after draining the controller.
- Run health checks every five minutes.
- Run scoped cleanup daily.
- Compare installed state with its pinned Git configuration every fifteen minutes.
- Never schedule `docker system prune`.
- Alert before Docker storage reaches 80%; treat 90% as critical.

## Debian host setup

Install and enable Debian's supported security update mechanism:

```bash
sudo apt-get update
sudo apt-get install -y unattended-upgrades apt-listchanges
sudo dpkg-reconfigure -plow unattended-upgrades
sudo unattended-upgrade --dry-run --debug
```

Review `/etc/apt/apt.conf.d/50unattended-upgrades` and confirm only the intended Debian security origins are enabled. Keep automatic reboot disabled; a generic CI host may still be executing a job when a package requests reboot.

## Fleet timers

`scripts/install-worker-controller.sh` installs and enables all three timer pairs:

- `ci-fleet-health.timer` runs the complete [fleet health contract](HEALTH-MONITORING.md);
- `ci-fleet-cleanup.timer` removes only expired inactive fleet-owned resources;
- `ci-fleet-drift.timer` compares the installation with the exact pinned configuration commit without applying changes;
- `ci-fleet-reconcile.timer` fetches the latest reviewed desired-state commit from the private repository over authenticated HTTPS and applies it automatically.

Run each service manually once before relying on its timer:

```bash
sudo systemctl start ci-fleet-health.service
sudo systemctl start ci-fleet-drift.service
sudo journalctl -u ci-fleet-health.service --since today
sudo journalctl -u ci-fleet-drift.service --since today
sudo /opt/ci-fleet/current/scripts/cleanup.sh
sudo systemctl start ci-fleet-cleanup.service
```

The manual cleanup command is intentionally a dry-run. Enable the applying service only after its candidates are understood.

## Reboot procedure

1. Merge and apply a reviewed desired-state change setting the controller to `drained`, or otherwise pause the controller and wait for zero managed runners.
2. Confirm no managed runner container is active.
3. Apply updates and reboot.
4. Confirm Docker, disk, time synchronization, DNS, and outbound GitHub connectivity.
5. Run `scripts/install-worker-controller.sh --check` against the installed configuration commit.
6. Apply a reviewed `active` desired-state commit and confirm `MIN=0` produces no idle container.

## Dependency maintenance

Dependabot proposes updates for GitHub Actions, Go modules, and both Dockerfiles. Those pull requests must pass inert validation and be reviewed before merge. This preserves unattended host security patching without silently changing the runner control plane.

The `Update GitHub Actions runner release` workflow checks the official `actions/runner` releases each Monday. It ignores drafts and prereleases, validates the stable tag and exact Linux x64 and arm64 assets, downloads both archives, and calculates their SHA-256 checksums. When an update exists, it updates the three pins in `runner/Dockerfile` and the `CI_FLEET_RUNNER_VERSION` default in `deploy/compose.yaml` on the machine-managed `automation/update-actions-runner` branch. It creates or refreshes one pull request for normal review and validation. If all four pins are current, it makes no branch or pull-request change.

Run the same check on demand from **Actions → Update GitHub Actions runner release → Run workflow**. The workflow only proposes a repository change. It does not build or deploy a host update.

Controller and runner engine updates are also pinned. A merged private configuration change is applied with `install-worker-controller.sh --upgrade`; the host never follows a moving engine or configuration branch automatically. See [Git-authored controller desired state](DESIRED-STATE.md).
