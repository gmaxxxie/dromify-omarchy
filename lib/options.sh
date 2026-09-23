# dromify-options.sh - load Dromify's panel-set options.
#
# Sourced by bin/dromify-api, bin/dromify-output and bin/dromify-dlna. The
# panel cannot set the shell process's environment, and passing an option on a
# command line would put it in world-readable /proc/<pid>/cmdline, so options
# live in an owner-only state file and the helpers read it themselves.
#
# Only assignments are honoured: the file is validated by content before being
# sourced, so a corrupted or hand-edited file cannot turn into code execution.
# The environment wins over the file, so an explicit invocation (a test, a
# one-off shell command) is never silently overridden by the saved setting.

dromify_load_options() {
  local file="${XDG_STATE_HOME:-$HOME/.local/state}/dromify/options.env"
  [[ -f "$file" && ! -L "$file" && -O "$file" ]] || return 0
  if grep -qvE '^[[:space:]]*(#|$|[A-Za-z_][A-Za-z0-9_]*=[A-Za-z0-9_./-]*$)' -- "$file"; then
    echo "dromify: ignoring $file (unexpected content)" >&2
    return 0
  fi
  local saved_insecure="${DROMIFY_ALLOW_INSECURE_LAN:-}"
  # shellcheck disable=SC1090
  . "$file"
  # An explicitly exported value is a deliberate per-invocation choice.
  if [[ -n "$saved_insecure" ]]; then
    export DROMIFY_ALLOW_INSECURE_LAN="$saved_insecure"
  fi
  export DROMIFY_OPTIONS_FILE="$file"
}
