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

- `ci-fleet-health.timer` checks Docker, disk, and the controller's desired runtime state;
- `ci-fleet-cleanup.timer` removes only expired inactive fleet-owned resources;
- `ci-fleet-drift.timer` compares the installation with the exact pinned configuration commit without applying changes.

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

Controller and runner engine updates are also pinned. A merged private configuration change is applied with `install-worker-controller.sh --upgrade`; the host never follows a moving engine or configuration branch automatically. See [Git-authored controller desired state](DESIRED-STATE.md).
