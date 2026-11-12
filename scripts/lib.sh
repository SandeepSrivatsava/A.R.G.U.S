# shellcheck shell=bash
# Shared helpers for Argus's setup scripts (sourced, not run): output, questions, the secrets file and
# config.toml. The scripts set ARGUS_HOME, SECRETS, DRY_RUN and ASSUME_YES before calling them.
: "${DRY_RUN:=0}" "${ASSUME_YES:=0}"

if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'; else B=; G=; Y=; R=; N=; fi
step() { printf '\n%s%s%s\n' "$B" "$*" "$N"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*" >&2; }
die()  { printf '%sError:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }
run()  { if [ "$DRY_RUN" = 1 ]; then printf '  [dry-run] %s\n' "$*"; else "$@"; fi; }
is_tty() { [ -t 0 ]; }

ask() {  # ask VAR "Question" "default": uses $VAR if already set (tests and re-runs skip prompts)
  local var="$1" question="$2" default="${3:-}" answer
  eval "answer=\${$var:-}"
  if [ -z "$answer" ]; then
    is_tty || die "$var is not set and there is no terminal to ask. Set $var and run again."
    if [ -n "$default" ]; then read -r -p "  $question [$default]: " answer; else read -r -p "  $question: " answer; fi
    answer="${answer:-$default}"
  fi
  eval "$var=\$answer"
}

ask_secret() {  # like ask, but typing is hidden; empty allowed when $3 = optional
  local var="$1" question="$2" optional="${3:-}" answer
  eval "answer=\${$var:-}"
  if [ -z "$answer" ]; then
    if is_tty; then read -r -s -p "  $question: " answer; echo
    elif [ -z "$optional" ]; then die "$var is not set and there is no terminal to ask. Set $var and run again."; fi
  fi
  eval "$var=\$answer"
}

confirm() {  # confirm "Question" y|n : returns 0 for yes
  local question="$1" default="$2" answer
  if [ "$ASSUME_YES" = 1 ] || ! is_tty; then [ "$default" = y ]; return; fi
  if [ "$default" = y ]; then read -r -p "  $question [Y/n]: " answer; else read -r -p "  $question [y/N]: " answer; fi
  answer="${answer:-$default}"
  case "$answer" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

sha256() { if command -v shasum > /dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1; else sha256sum "$1" | cut -d' ' -f1; fi; }
esc() { printf '%s' "$1" | sed -e 's/[\\|&]/\\&/g'; }
toml_str() { printf '"%s"' "$(printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"; }

# ---------- secrets go into ~/.argus/secrets.env through stdin, never argv ----------
secret_get() { if [ -f "$SECRETS" ]; then sed -n "s/^$1=//p" "$SECRETS" | tail -1; fi; }
secret_set() {  # usage: printf '%s' "$value" | secret_set KEY
  local key="$1" value tmp
  IFS= read -r value || true
  if [ "$DRY_RUN" = 1 ]; then printf '  [dry-run] save %s in %s\n' "$key" "$SECRETS"; return; fi
  mkdir -p "$ARGUS_HOME" && chmod 700 "$ARGUS_HOME"
  touch "$SECRETS" && chmod 600 "$SECRETS"
  tmp="$(mktemp "$ARGUS_HOME/.secrets.XXXXXX")"
  grep -v "^${key}=" "$SECRETS" > "$tmp" || true
  [ -z "$value" ] || printf '%s=%s\n' "$key" "$value" >> "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$SECRETS"
}

# toml_set FILE SECTION KEY VALUE: set one setting in config.toml, adding the section or key if it's missing.
# VALUE is written as TOML (true, 8765, "apple"). The rest of the file, comments included, is kept.
toml_set() {
  local file="$1" section="$2" key="$3" value="$4"
  if [ -z "$section" ]; then                       # a top-level setting, before the first [section]
    awk -v k="$key" -v v="$value" 'BEGIN{done=0; top=1}
      top && /^\[/ {if (!done) print k " = " v; done=1; top=0}
      top && $0 ~ "^"k"[[:space:]]*=" {if (!done) print k " = " v; done=1; next}
      {print} END{if (!done) print k " = " v}' "$file" > "$file.tmp"
    mv "$file.tmp" "$file"
  elif grep -q "^\[$section\]" "$file"; then
    if awk -v s="[$section]" -v k="$key" '$0==s{x=1;next} /^\[/{x=0} x && $0 ~ "^"k"[[:space:]]*=" {f=1} END{exit !f}' "$file"; then
      awk -v s="[$section]" -v k="$key" -v v="$value" \
        '$0==s{x=1;print;next} /^\[/{x=0} x && $0 ~ "^"k"[[:space:]]*=" {print k " = " v; next} {print}' "$file" > "$file.tmp"
    else
      awk -v s="[$section]" -v k="$key" -v v="$value" '{print} $0==s{print k " = " v}' "$file" > "$file.tmp"
    fi
    mv "$file.tmp" "$file"
  else
    printf '\n[%s]\n%s = %s\n' "$section" "$key" "$value" >> "$file"
  fi
  chmod 600 "$file"
}

# toml_get FILE SECTION KEY: a setting's value, without quotes or comment ("" if it isn't there).
# SECTION "" means the top of the file, before the first [section].
toml_get() {
  awk -v s="[$2]" -v k="$3" 'BEGIN{x = (s == "[]")} $0==s{x=1;next} /^\[/{x=0}
    x && $0 ~ "^"k"[[:space:]]*=" {sub(/^[^=]*=[[:space:]]*/, ""); sub(/[[:space:]]*#.*$/, ""); gsub(/"/, ""); print; exit}' "$1"
}

# The answers file remembers which questions were answered, including keys you chose to skip, so a later run
# can leave them alone. One "id=value" per line, owner-only. (Settings live in config.toml, keys in secrets.env.)
answer_get() { if [ -n "${ANSWERS:-}" ] && [ -f "$ANSWERS" ]; then sed -n "s/^$1=//p" "$ANSWERS" | tail -1; fi; }
answer_set() {  # answer_set ID VALUE
  [ -n "${ANSWERS:-}" ] && [ "$DRY_RUN" != 1 ] || return 0
  mkdir -p "$(dirname "$ANSWERS")"
  touch "$ANSWERS" && chmod 600 "$ANSWERS"
  { grep -v "^$1=" "$ANSWERS" || true; printf '%s=%s\n' "$1" "$2"; } > "$ANSWERS.tmp"
  mv "$ANSWERS.tmp" "$ANSWERS" && chmod 600 "$ANSWERS"
}

# optional_key VAR SECRET_NAME ID LABEL: an optional key (Google Maps, TomTom). Takes VAR as given, else the saved
# key. KEEP=1 (keep saved answers) also leaves a key alone that was skipped before; KEEP=0 (reviewing) offers to
# replace a saved key. Sets KEY_ASK=1 when the caller should ask for it (it explains what the key is for first).
optional_key() {
  local var="$1" secret="$2" id="$3" label="$4" value new
  eval "value=\${$var:-}"
  KEY_ASK=0
  if [ -z "$value" ] && [ "${CHANGE_KEYS:-0}" = 0 ]; then
    value="$(secret_get "$secret")"
    if [ -n "$value" ] && [ "${KEEP:-1}" = 0 ] && is_tty; then
      ok "$label: saved (ending ...$(printf '%s' "$value" | tail -c 4))"
      read -r -s -p "  New $label (Enter keeps the saved one): " new; echo
      [ -z "$new" ] || value="$new"
      eval "$var=\$value"
      return
    fi
  fi
  if [ -n "$value" ]; then
    eval "$var=\$value"
    ok "$label: found (ending ...$(printf '%s' "$value" | tail -c 4))"
  elif [ "${KEEP:-0}" = 1 ] && [ "$(answer_get "$id")" = skipped ]; then
    ok "$label: skipped before (to add one, run ./setup.sh and choose to change your answers)"
  elif is_tty; then
    KEY_ASK=1
  fi
}

# choose_nav_apps PHONE GIVEN [MAIN] [SAVED]: which navigation apps are on the phone. Sets MAPS_APP (the one that opens
# by default) and MAPS_APPS_TOML (the others, as a TOML list body like: "google", "waze").
# GIVEN, like "apple, google,waze" (from ARGUS_MAPS_APPS or the settings file), is used as it is: its first app
# opens by default. Without it, asks on a terminal; MAIN is then the app that opens (default: by phone).
app_name() { case "$1" in apple) printf 'Apple Maps' ;; google) printf 'Google Maps' ;; waze) printf 'Waze' ;; esac; }

choose_nav_apps() {
  local phone="$1" given="$2" main="${3:-}" saved=" ${4:-} " apps="" word yes
  case "$main" in apple|google|waze) ;; *) if [ "$phone" = android ]; then main=google; else main=apple; fi ;; esac
  for word in $(printf '%s' "$given" | tr '[:upper:]' '[:lower:]' | tr ',[]"' '     '); do
    case "$word" in
      apple|google|waze) case " $apps " in *" $word "*) ;; *) apps="$apps $word" ;; esac ;;
    esac
  done
  if [ -z "$apps" ]; then
    apps="$main"
    if is_tty && [ "$ASSUME_YES" != 1 ]; then
      echo "  Directions open in $(app_name "$main"). Argus can offer your other navigation apps too:"
      if [ "$phone" != android ] && [ "$main" != google ]; then
        yes=n; case "$saved" in *" google "*) yes=y ;; esac
        if confirm "Is Google Maps on your iPhone?" "$yes"; then apps="$apps google"; fi
      fi
      if [ "$main" != waze ]; then
        yes=n; case "$saved" in *" waze "*) yes=y ;; esac
        if confirm "Is Waze on your phone?" "$yes"; then apps="$apps waze"; fi
      fi
    fi
  fi
  apps="${apps# }"
  MAPS_APP="${apps%% *}"
  MAPS_APPS_TOML=""
  for word in $apps; do
    [ "$word" = "$MAPS_APP" ] || MAPS_APPS_TOML="${MAPS_APPS_TOML:+$MAPS_APPS_TOML, }\"$word\""
  done
}

# ask_tomtom_key: sets TOMTOM_KEY (from ARGUS_TOMTOM_KEY, the saved key, or a question; empty = skipped).
# shellcheck disable=SC2034  # TOMTOM_KEY and TOMTOM_ASKED are read by the setup scripts
ask_tomtom_key() {
  TOMTOM_KEY="${ARGUS_TOMTOM_KEY:-}"
  TOMTOM_ASKED=0
  optional_key TOMTOM_KEY TOMTOM_API_KEY tomtom_key "TomTom key"
  if [ "$KEY_ASK" = 1 ]; then
    echo "  Accidents, closures and road works on each route, and one more opinion on the traffic, come from"
    echo "  TomTom: a free key from developer.tomtom.com (2,500 requests a day, no card). Press Enter to skip."
    ask_secret TOMTOM_KEY "TomTom API key (Enter to skip)" optional
    TOMTOM_ASKED=1
  fi
}

# normalize_phone ANSWER: "iphone" or "android" (from 1/2, iPhone, ios, Android...), or "" if unclear.
normalize_phone() {
  case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -d ' ')" in
    1|iphone|ios|i|apple) printf 'iphone' ;;
    2|android|a|samsung|pixel) printf 'android' ;;
    *) printf '' ;;
  esac
}
