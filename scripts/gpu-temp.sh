#!/usr/bin/env bash
# Live GPU temperature dashboard for a Linux host with a discrete GPU.
# Reads the nouveau hwmon sensor and plots recent samples in the terminal.
#
#   sudo bash scripts/gpu-temp.sh [interval_seconds]
#
# History is appended to /var/tmp/gpu-temp.csv (override with GPU_TEMP_LOG).
set -euo pipefail

INTERVAL="${1:-2}"
LOG="${GPU_TEMP_LOG:-/var/tmp/gpu-temp.csv}"
YMIN=35
YMAX=115
SENSOR=""
DEV=""
FAN=""
LOG_OK=1

if [[ ! "$INTERVAL" =~ ^[1-9][0-9]*$ ]]; then
  echo "Interval must be a positive number of seconds." >&2
  exit 1
fi

find_sensor() {
  local h name
  for h in /sys/class/hwmon/hwmon*; do
    [[ -r "$h/name" && -r "$h/temp1_input" ]] || continue
    name=$(<"$h/name")
    if [[ "$name" == "nouveau" || "$name" == "nvidia" || "$name" == "amdgpu" ]]; then
      printf '%s\n' "$h"
      return 0
    fi
  done
  return 1
}

if ! SENSOR=$(find_sensor); then
  echo "No nouveau, nvidia, or amdgpu hwmon temperature sensor found." >&2
  echo "Expected /sys/class/hwmon/hwmon*/name to be one of those, with temp1_input." >&2
  exit 1
fi

if [[ -L "$SENSOR/device" ]]; then
  DEV=$(basename "$(readlink -f "$SENSOR/device")")
fi
if [[ -r "$SENSOR/fan1_input" ]]; then
  FAN="$SENSOR/fan1_input"
fi

declare -a TEMPS=()

load_history() {
  [[ -r "$LOG" ]] || return 0
  local line c
  while IFS= read -r line; do
    c=${line#*,}
    [[ "$c" =~ ^[0-9]+$ ]] || continue
    TEMPS+=("$c")
  done < <(tail -n 600 "$LOG" 2>/dev/null || true)
}

read_temp() {
  local raw
  raw=$(<"$SENSOR/temp1_input")
  printf '%s\n' $((raw / 1000))
}

read_fan() {
  [[ -n "$FAN" && -r "$FAN" ]] || return 0
  printf '%s\n' "$(<"$FAN")"
}

read_clock() {
  local p
  for p in /sys/kernel/debug/dri/0/pstate /sys/kernel/debug/dri/"$DEV"/pstate; do
    [[ -r "$p" ]] || continue
    awk -F: '/^AC:/ { gsub(/^ +/, "", $2); print $2; exit }' "$p" 2>/dev/null || true
    return 0
  done
}

append_log() {
  printf '%s,%s\n' "$(date +%s)" "$1" >>"$LOG" 2>/dev/null || LOG_OK=0
}

color_for() {
  local t=$1
  if ((t >= 105)); then
    printf '\033[1;31m'
  elif ((t >= 95)); then
    printf '\033[31m'
  elif ((t >= 90)); then
    printf '\033[33m'
  elif ((t >= 75)); then
    printf '\033[33m'
  else
    printf '\033[32m'
  fi
}

trend() {
  local n=${#TEMPS[@]} step=$((60 / INTERVAL))
  if ((step < 2)); then step=2; fi
  if ((n <= step)); then
    printf 'warming up'
    return
  fi
  local prev=${TEMPS[$((n - 1 - step))]}
  local now=${TEMPS[$((n - 1))]}
  local d=$((now - prev))
  if ((d >= 2)); then
    printf 'rising %+d°C/min' "$d"
  elif ((d <= -2)); then
    printf 'falling %+d°C/min' "$d"
  else
    printf 'steady %+d°C/min' "$d"
  fi
}

minmax() {
  local t min=999 max=0
  for t in "${TEMPS[@]}"; do
    ((t < min)) && min=$t
    ((t > max)) && max=$t
  done
  printf '%s %s\n' "$min" "$max"
}

restore() {
  printf '\033[?25h\033[0m'
  tput cnorm 2>/dev/null || true
}
trap restore EXIT INT TERM

load_history
printf '\033[?25l'

while true; do
  temp=$(read_temp)
  TEMPS+=("$temp")
  if ((${#TEMPS[@]} > 2000)); then
    TEMPS=("${TEMPS[@]: -1500}")
  fi
  append_log "$temp"

  cols=$(tput cols 2>/dev/null || echo 80)
  rows=$(tput lines 2>/dev/null || echo 24)
  chart_h=$((rows - 9))
  chart_w=$((cols - 8))
  if ((chart_h < 8)); then chart_h=8; fi
  if ((chart_w < 20)); then chart_w=20; fi
  if ((chart_w > cols - 8)); then chart_w=$((cols - 8)); fi

  read -r min max <<<"$(minmax)"
  fan_rpm=$(read_fan || true)
  clock=$(read_clock || true)
  move=$(trend)
  c=$(color_for "$temp")

  buf=$'\033[H\033[J'
  buf+=$'GPU temperature\n'
  buf+=$(printf '  %s' "$(<"$SENSOR/name") $(basename "$SENSOR")")
  if [[ -n "$DEV" ]]; then buf+="   $DEV"; fi
  buf+=$(printf '   every %ss\n\n' "$INTERVAL")
  buf+=$(printf '  %b%s°C\033[0m    min %s    max %s    %s' "$c" "$temp" "$min" "$max" "$move")
  if [[ -n "$fan_rpm" ]]; then buf+=$(printf '    fan %s rpm' "$fan_rpm"); fi
  buf+=$'\n'
  if [[ -n "$clock" ]]; then buf+=$(printf '  clocks %s\n' "$clock"); fi
  buf+=$'\n'

  n=${#TEMPS[@]}
  start=0
  if ((n > chart_w)); then start=$((n - chart_w)); fi
  span=$((YMAX - YMIN))

  declare -a label_at=()
  for ((r = 0; r < chart_h; r++)); do label_at[r]=""; done
  for mark in "$YMAX" 105 95 90 "$YMIN"; do
    best=0
    best_d=9999
    for ((r = 0; r < chart_h; r++)); do
      rt=$((YMAX - r * span / (chart_h - 1)))
      d=$((rt - mark))
      if ((d < 0)); then d=$((-d)); fi
      if ((d < best_d)); then best_d=$d; best=$r; fi
    done
    label_at[best]=$(printf '%4s' "$mark")
  done

  for ((r = 0; r < chart_h; r++)); do
    rt=$((YMAX - r * span / (chart_h - 1)))
    lab=${label_at[r]:-    }
    line=$(printf '%s ┤' "$lab")
    for ((x = 0; x < chart_w; x++)); do
      idx=$((start + x))
      if ((idx >= n)); then
        line+=' '
        continue
      fi
      sample=${TEMPS[$idx]}
      if ((sample >= rt)); then
        line+=$(color_for "$sample")
        line+=$'█\033[0m'
      else
        near=0
        step=$((span / (chart_h - 1)))
        if ((step < 1)); then step=1; fi
        for mark in 90 95 105; do
          d=$((rt - mark))
          if ((d < 0)); then d=$((-d)); fi
          if ((d <= step / 2)); then near=1; fi
        done
        if ((near)); then
          line+=$'\033[2m─\033[0m'
        else
          line+=' '
        fi
      fi
    done
    buf+="$line"$'\n'
  done

  axis=$(printf '     └')
  for ((x = 0; x < chart_w; x++)); do axis+='─'; done
  buf+="$axis"$'\n'
  buf+=$(printf '       older %*s now\n' $((chart_w - 10)) "")
  if ((LOG_OK)); then
    buf+=$(printf '  %s samples in view, log %s\n' "$n" "$LOG")
  else
    buf+=$(printf '  %s samples in view, log not writable (%s)\n' "$n" "$LOG")
  fi
  buf+=$'  90 fanboost   95 downclock   105 critical\n'

  printf '%s' "$buf"
  sleep "$INTERVAL"
done
