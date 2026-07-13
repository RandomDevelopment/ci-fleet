# Host maintenance standard

Fleet hosts are generic Docker infrastructure. They must receive operating-system security fixes automatically, report health, and clean only fleet-owned expired resources.

## Policy

- Enable the distribution's unattended **security** upgrades.
- Do not automatically replace controller, runner, Docker major-version, kernel, or project images without repository validation.
- Schedule reboots only after draining the controller.
- Run health checks every five minutes.
- Run scoped cleanup daily.
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

## Install fleet timers

After the repository is installed at `/opt/ci-fleet` and `/etc/ci-fleet/ci-fleet.env` exists:

```bash
sudo install -m 0644 host/systemd/ci-fleet-health.service /etc/systemd/system/
sudo install -m 0644 host/systemd/ci-fleet-health.timer /etc/systemd/system/
sudo install -m 0644 host/systemd/ci-fleet-cleanup.service /etc/systemd/system/
sudo install -m 0644 host/systemd/ci-fleet-cleanup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ci-fleet-health.timer ci-fleet-cleanup.timer
```

Run each service manually once before relying on its timer:

```bash
sudo systemctl start ci-fleet-health.service
sudo journalctl -u ci-fleet-health.service --since today
sudo /opt/ci-fleet/scripts/cleanup.sh
sudo systemctl start ci-fleet-cleanup.service
```

The manual cleanup command is intentionally a dry-run. Enable the applying service only after its candidates are understood.

## Reboot procedure

1. Stop new capacity by stopping the controller.
2. Confirm no managed runner container is active.
3. Apply updates and reboot.
4. Confirm Docker, disk, time synchronization, DNS, and outbound GitHub connectivity.
5. Run `scripts/preflight.sh`.
6. Start the controller and confirm `MIN=0` produces no idle container.

## Dependency maintenance

Dependabot proposes updates for GitHub Actions, Go modules, and both Dockerfiles. Those pull requests must pass inert validation and be reviewed before merge. This preserves unattended host security patching without silently changing the runner control plane.
