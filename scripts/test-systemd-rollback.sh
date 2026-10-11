#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
systemd_dir=$tmp/systemd
checkpoint_dir=$tmp/checkpoint
expected_dir=$tmp/expected
unit_names=(ci-fleet-health.service ci-fleet-health.timer ci-fleet-cleanup.service ci-fleet-cleanup.timer ci-fleet-drift.service ci-fleet-drift.timer)
timer_names=(ci-fleet-health.timer ci-fleet-cleanup.timer ci-fleet-drift.timer)
optional_unit_names=(ci-fleet-reconcile.service ci-fleet-reconcile.timer)
staging_paths=()
reconcile_unit=ci-fleet-reconcile.service
calls=$tmp/systemctl.log

fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }

# Exercise the production functions without running the installer's CLI.
source_functions=$tmp/functions.sh
sed -n '/^remove_systemd_units() {$/,/^}$/p; /^restore_systemd_snapshot() {$/,/^}$/p' "$repo_root/scripts/install-worker-controller.sh" >"$source_functions"
# shellcheck disable=SC1090
source "$source_functions"

install() {
  [[ -z ${fail_install_unit:-} || ${*: -1} != */"$fail_install_unit" ]] || return 42
  command install "$@"
}
mv() {
  [[ -z ${fail_move_unit:-} || ${*: -1} != "$systemd_dir/$fail_move_unit" ]] || return 42
  command mv "$@"
}

systemctl() {
  printf '%s\n' "$*" >>"$calls"
  local action=$1 unit directory expected actual start_budget=90 stop_budget=90
  shift
  case "$action" in
    show)
      [[ ${fail_show:-false} != true ]] || return 1
      printf '%s\n' "$modeled_service_state"
      ;;
    daemon-reload)
      # This is a timeout model, not a real systemd timing test. Recovery takes
      # 128 seconds, beyond the 90-second manager default and within 2h/15min.
      for unit in "${unit_names[@]}" "${optional_unit_names[@]}"; do
        expected=$expected_dir/$unit
        actual=$systemd_dir/$unit
        if [[ -f "$expected" ]]; then
          cmp -s "$expected" "$actual" || { printf 'invalid unit at reload: %s\n' "$unit" >&2; return 1; }
        else
          [[ ! -e "$actual" ]] || { printf 'unexpected unit at reload: %s\n' "$unit" >&2; return 1; }
        fi
      done
      if [[ "$model_running_service" == true ]]; then
        if grep -Fxq 'TimeoutStartSec=2h' "$systemd_dir/$reconcile_unit"; then start_budget=7200; fi
        if grep -Fxq 'TimeoutStopSec=15min' "$systemd_dir/$reconcile_unit"; then stop_budget=900; fi
        ((128 < start_budget && 128 < stop_budget)) || return 1
      fi
      [[ ${fail_reload:-false} != true ]] || return 1
      ;;
    enable|disable|start|stop)
      directory=$tmp/enabled
      [[ "$action" != start && "$action" != stop ]] || directory=$tmp/active
      for unit in "$@"; do
        [[ "$unit" != --now ]] || continue
        [[ "$unit" != "$reconcile_unit" ]] || fail 'rollback tried to stop or restart its parent service'
        [[ "$unit" != "${fail_timer:-}" ]] || return 1
        case "$action" in
          enable|start) : >"$directory/$unit" ;;
          disable|stop) rm -f "$directory/$unit" ;;
        esac
        [[ " $* " != *' --now '* || "$action" != disable ]] || rm -f "$tmp/active/$unit"
      done
      ;;
    *) fail "unexpected systemctl call: $action $*" ;;
  esac
}

reset_fixture() {
  rm -rf "$systemd_dir" "$checkpoint_dir" "$expected_dir" "$tmp/enabled" "$tmp/active"
  mkdir -p "$systemd_dir" "$checkpoint_dir/systemd" "$expected_dir" "$tmp/enabled" "$tmp/active"
  : >"$calls"
  fail_install_unit='' fail_move_unit='' fail_timer='' fail_reload=false fail_show=false
  staging_paths=()
  modeled_service_state=activating model_running_service=true
  local unit
  for unit in "${unit_names[@]}" "${optional_unit_names[@]}"; do
    cp "$repo_root/host/systemd/$unit" "$checkpoint_dir/systemd/$unit"
    cp "$checkpoint_dir/systemd/$unit" "$expected_dir/$unit"
    cp "$checkpoint_dir/systemd/$unit" "$systemd_dir/$unit"
    printf '\n# candidate definition\n' >>"$systemd_dir/$unit"
  done
  printf '%s\n' ci-fleet-cleanup.timer ci-fleet-reconcile.timer >"$checkpoint_dir/enabled-timers"
  printf '%s\n' ci-fleet-drift.timer ci-fleet-reconcile.timer >"$checkpoint_dir/active-timers"
  : >"$tmp/enabled/ci-fleet-health.timer"
  : >"$tmp/active/ci-fleet-health.timer"
}

reset_fixture
restore_systemd_snapshot || fail 'checkpoint restoration failed'
[[ ${#staging_paths[@]} == 1 ]] || fail 'staged units were not registered for cleanup'
[[ $(grep -Fxc daemon-reload "$calls") == 1 ]] || fail 'rollback did not reload exactly once'
for timer in "${timer_names[@]}" ci-fleet-reconcile.timer; do
  if grep -Fxq "$timer" "$checkpoint_dir/enabled-timers"; then
    [[ -f "$tmp/enabled/$timer" ]] || fail "timer was not enabled: $timer"
  else
    [[ ! -f "$tmp/enabled/$timer" ]] || fail "timer remained enabled: $timer"
  fi
  if grep -Fxq "$timer" "$checkpoint_dir/active-timers"; then
    [[ -f "$tmp/active/$timer" ]] || fail "timer was not started: $timer"
  else
    [[ ! -f "$tmp/active/$timer" ]] || fail "timer remained active: $timer"
  fi
done

for state in activating active reloading deactivating; do
  reset_fixture
  modeled_service_state=$state
  rm "$checkpoint_dir/systemd/$reconcile_unit"
  cp "$systemd_dir/$reconcile_unit" "$expected_dir/$reconcile_unit"
  restore_systemd_snapshot || fail "older checkpoint removed the executing unit: $state"
done

for state in inactive failed; do
  reset_fixture
  modeled_service_state=$state model_running_service=false
  rm "$checkpoint_dir/systemd/$reconcile_unit" "$expected_dir/$reconcile_unit"
  restore_systemd_snapshot || fail "absent stopped unit was not removed: $state"
done

for failure in install move show reload timer; do
  reset_fixture
  case "$failure" in
    install) fail_install_unit=$reconcile_unit ;;
    move) fail_move_unit=$reconcile_unit ;;
    show) fail_show=true; rm "$checkpoint_dir/systemd/$reconcile_unit" ;;
    reload) fail_reload=true ;;
    timer) fail_timer=ci-fleet-cleanup.timer ;;
  esac
  if restore_systemd_snapshot; then fail "restoration hid $failure failure"; fi
  [[ -f "$systemd_dir/$reconcile_unit" ]] || fail "$failure failure removed the executing unit"
  [[ -f "$checkpoint_dir/enabled-timers" ]] || fail "$failure failure removed recovery evidence"
  if [[ "$failure" != reload && "$failure" != timer ]]; then
    ! grep -Fxq daemon-reload "$calls" || fail "$failure failure reloaded an incomplete unit set"
  fi
done

reset_fixture
rm -f "$expected_dir"/*
model_running_service=false
remove_systemd_units || fail 'explicit uninstall failed'
for unit in "${unit_names[@]}" "${optional_unit_names[@]}"; do
  [[ ! -e "$systemd_dir/$unit" ]] || fail "uninstall retained $unit"
done
printf 'SYSTEMD_ROLLBACK_REGRESSION_OK timing=model\n'
