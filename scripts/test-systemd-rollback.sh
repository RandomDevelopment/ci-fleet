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
lock_file=$tmp/installer.lock
deferred_command=()

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
systemd-run() {
  [[ " $* " == *' --property=After=ci-fleet-reconcile.service '* ]] || fail 'deferred restore is not ordered after reconciliation'
  [[ ${fail_dispatch:-false} != true ]] || return 1
  while [[ "$1" != /bin/bash ]]; do shift; done
  deferred_command=("$@")
}

complete_deferred() {
  [[ ${#deferred_command[@]} != 0 ]] || fail 'no deferred restoration was queued'
  modeled_service_state=inactive model_running_service=false
  if [[ -f "$checkpoint_dir/systemd/$reconcile_unit" ]]; then
    cp "$checkpoint_dir/systemd/$reconcile_unit" "$expected_dir/$reconcile_unit"
  else
    rm -f "$expected_dir/$reconcile_unit"
  fi
  local environment=$tmp/deferred-env
  declare -p tmp systemd_dir expected_dir calls unit_names optional_unit_names reconcile_unit modeled_service_state modeled_state_file model_running_service fail_reload fail_show fail_timer >"$environment"
  declare -f systemctl fail >>"$environment"
  BASH_ENV="$environment" "${deferred_command[@]}"
}

systemctl() {
  printf '%s\n' "$*" >>"$calls"
  local action=$1 unit directory expected actual start_budget=90 stop_budget=90
  shift
  case "$action" in
    show)
      [[ ${fail_show:-false} != true ]] || return 1
      if [[ -n ${modeled_state_file:-} ]]; then
        local queried_state
        queried_state=$(cat "$modeled_state_file")
        printf '%s\n' "$queried_state"
        if [[ "$queried_state" == activating ]]; then : >"$tmp/deferred-state-query"; fi
      else
        printf '%s\n' "$modeled_service_state"
      fi
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
  rm -f "$tmp/deferred-env" "$tmp/deferred-state-query"
  rm -rf "$systemd_dir" "$checkpoint_dir" "$expected_dir" "$tmp/enabled" "$tmp/active"
  mkdir -p "$systemd_dir" "$checkpoint_dir/systemd" "$expected_dir" "$tmp/enabled" "$tmp/active"
  : >"$calls"
  fail_install_unit='' fail_move_unit='' fail_timer='' fail_reload=false fail_show=false fail_dispatch=false
  staging_paths=()
  deferred_command=()
  modeled_service_state=activating model_running_service=true
  modeled_state_file=''
  local unit
  for unit in "${unit_names[@]}" "${optional_unit_names[@]}"; do
    cp "$repo_root/host/systemd/$unit" "$checkpoint_dir/systemd/$unit"
    cp "$checkpoint_dir/systemd/$unit" "$expected_dir/$unit"
    cp "$checkpoint_dir/systemd/$unit" "$systemd_dir/$unit"
    printf '\n# candidate definition\n' >>"$systemd_dir/$unit"
  done
  cp "$systemd_dir/$reconcile_unit" "$expected_dir/$reconcile_unit"
  printf '%s\n' ci-fleet-cleanup.timer ci-fleet-reconcile.timer >"$checkpoint_dir/enabled-timers"
  printf '%s\n' ci-fleet-drift.timer ci-fleet-reconcile.timer >"$checkpoint_dir/active-timers"
  : >"$tmp/enabled/ci-fleet-health.timer"
  : >"$tmp/active/ci-fleet-health.timer"
}

reset_fixture
restore_systemd_snapshot || fail 'checkpoint restoration failed'
[[ ${#staging_paths[@]} == 0 && -d ${deferred_command[2]} ]] || fail 'deferred restore did not retain its staged units'
[[ ${deferred_command[-1]} == "$lock_file" ]] || fail 'deferred restore bypasses the installer lock'
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
complete_deferred || fail 'checkpointed unit was not restored after execution'
[[ ! -d ${deferred_command[2]} ]] || fail 'completed deferred restore retained its staged units'

reset_fixture
sed -i '/^TimeoutStartSec=/d; /^TimeoutStopSec=/d' "$checkpoint_dir/systemd/$reconcile_unit"
restore_systemd_snapshot || fail 'older checkpoint dropped the live timeout budgets'
complete_deferred || fail 'older unit was not restored after execution'

reset_fixture
restore_systemd_snapshot || fail 'could not queue execution-wait fixture'
modeled_state_file=$tmp/service-state
printf 'activating\n' >"$modeled_state_file"
complete_deferred >"$tmp/deferred-wait.log" 2>&1 &
deferred_pid=$!
for ((attempt = 0; attempt < 200; attempt++)); do
  [[ ! -f "$tmp/deferred-state-query" ]] || break
  sleep 0.05
done
[[ -f "$tmp/deferred-state-query" ]] || fail 'deferred helper did not wait for the executing service'
cmp -s "$systemd_dir/$reconcile_unit" "${deferred_command[2]}/current.service" || fail 'deferred helper changed a running service'
printf 'inactive\n' >"$modeled_state_file"
wait "$deferred_pid" || fail 'deferred helper did not finish after the service exited'
cmp -s "$systemd_dir/$reconcile_unit" "$checkpoint_dir/systemd/$reconcile_unit" || fail 'deferred helper did not restore the unit'

for state in activating active reloading deactivating; do
  reset_fixture
  modeled_service_state=$state
  rm "$checkpoint_dir/systemd/$reconcile_unit"
  cp "$systemd_dir/$reconcile_unit" "$expected_dir/$reconcile_unit"
  restore_systemd_snapshot || fail "older checkpoint removed the executing unit: $state"
  complete_deferred || fail "unit absent from the checkpoint was not removed after execution: $state"
done

for state in inactive failed; do
  reset_fixture
  modeled_service_state=$state model_running_service=false
  rm "$checkpoint_dir/systemd/$reconcile_unit" "$expected_dir/$reconcile_unit"
  restore_systemd_snapshot || fail "absent stopped unit was not removed: $state"
done

for failure in install move show reload timer dispatch; do
  reset_fixture
  case "$failure" in
    install) fail_install_unit=$reconcile_unit ;;
    move) fail_move_unit=ci-fleet-health.service ;;
    show) fail_show=true; rm "$checkpoint_dir/systemd/$reconcile_unit" ;;
    reload) fail_reload=true ;;
    timer) fail_timer=ci-fleet-cleanup.timer ;;
    dispatch) fail_dispatch=true ;;
  esac
  if restore_systemd_snapshot; then fail "restoration hid $failure failure"; fi
  [[ -f "$systemd_dir/$reconcile_unit" ]] || fail "$failure failure removed the executing unit"
  [[ -f "$checkpoint_dir/enabled-timers" ]] || fail "$failure failure removed recovery evidence"
  if [[ "$failure" != reload && "$failure" != timer && "$failure" != dispatch ]]; then
    ! grep -Fxq daemon-reload "$calls" || fail "$failure failure reloaded an incomplete unit set"
  fi
done

for failure in reload changed; do
  reset_fixture
  restore_systemd_snapshot || fail 'could not queue deferred failure fixture'
  case "$failure" in
    reload) fail_reload=true ;;
    changed) printf '\n# later installer\n' >>"$systemd_dir/$reconcile_unit" ;;
  esac
  if complete_deferred; then fail "deferred restoration hid $failure failure"; fi
  [[ -d ${deferred_command[2]} ]] || fail 'failed deferred restoration discarded its evidence'
done

reset_fixture
rm -f "$expected_dir"/*
model_running_service=false
remove_systemd_units || fail 'explicit uninstall failed'
for unit in "${unit_names[@]}" "${optional_unit_names[@]}"; do
  [[ ! -e "$systemd_dir/$unit" ]] || fail "uninstall retained $unit"
done
printf 'SYSTEMD_ROLLBACK_REGRESSION_OK timing=model\n'
