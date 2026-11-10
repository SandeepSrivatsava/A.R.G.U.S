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
  if grep -q "^\[$section\]" "$file"; then
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

# normalize_phone ANSWER: "iphone" or "android" (from 1/2, iPhone, ios, Android...), or "" if unclear.
normalize_phone() {
  case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -d ' ')" in
    1|iphone|ios|i|apple) printf 'iphone' ;;
    2|android|a|samsung|pixel) printf 'android' ;;
    *) printf '' ;;
  esac
}
