#!/usr/bin/env bash

set -euo pipefail

print_failed_bazel_test_logs=0
print_failed_bazel_action_summary=0
remote_download_toplevel=0
windows_msvc_host_platform=0
windows_cross_compile=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --print-failed-test-logs)
      print_failed_bazel_test_logs=1
      shift
      ;;
    --print-failed-action-summary)
      print_failed_bazel_action_summary=1
      shift
      ;;
    --remote-download-toplevel)
      remote_download_toplevel=1
      shift
      ;;
    --windows-msvc-host-platform)
      windows_msvc_host_platform=1
      shift
      ;;
    --windows-cross-compile)
      windows_cross_compile=1
      shift
      ;;
    --)
      shift
      break
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

if [[ $# -eq 0 ]]; then
  echo "Usage: $0 [--print-failed-test-logs] [--print-failed-action-summary] [--remote-download-toplevel] [--windows-msvc-host-platform] [--windows-cross-compile] -- <bazel args> -- <targets>" >&2
  exit 1
fi

bazel_startup_args=()
if [[ -n "${BAZEL_OUTPUT_USER_ROOT:-}" ]]; then
  bazel_startup_args+=("--output_user_root=${BAZEL_OUTPUT_USER_ROOT}")
fi

run_bazel() {
  if [[ "${RUNNER_OS:-}" == "Windows" ]]; then
    MSYS2_ARG_CONV_EXCL='*' "$(dirname "${BASH_SOURCE[0]}")/run_bazel_with_buildbuddy.py" "$@"
    return
  fi

  "$(dirname "${BASH_SOURCE[0]}")/run_bazel_with_buildbuddy.py" "$@"
}

run_bazel_with_startup_args() {
  if (( ${#bazel_startup_args[@]} > 0 )); then
    run_bazel "${bazel_startup_args[@]}" "$@"
    return
  fi

  run_bazel "$@"
}

ci_config=ci-linux
case "${RUNNER_OS:-}" in
  macOS)
    ci_config=ci-macos
    ;;
  Windows)
    if [[ $windows_cross_compile -eq 1 ]]; then
      ci_config=ci-windows-cross
    else
      ci_config=ci-windows
    fi
    ;;
esac

print_bazel_test_log_tails() {
  local console_log="$1"
  local testlogs_dir

  local -a bazel_info_args=(info)
  if [[ -n "${BUILDBUDDY_API_KEY:-}" ]]; then
    # `bazel info` needs the same CI config as the failed test invocation so
    # platform-specific output roots match. On Windows, omitting `ci-windows`
    # would point at `local_windows-fastbuild` even when the test ran with the
    # MSVC host platform under `local_windows_msvc-fastbuild`.
    bazel_info_args+=("--config=${ci_config}")
  fi

  # Only pass flags that affect Bazel's output-root selection or repository
  # lookup. Test/build-only flags such as execution logs or remote download
  # mode can make `bazel info` fail, which would hide the real test log path.
  for arg in "${post_config_bazel_args[@]}"; do
    case "$arg" in
      --host_platform=* | --repo_contents_cache=* | --repository_cache=*)
        bazel_info_args+=("$arg")
        ;;
    esac
  done

  testlogs_dir="$(run_bazel_with_startup_args \
    --noexperimental_remote_repo_contents_cache \
    "${bazel_info_args[@]}" \
    bazel-testlogs 2>/dev/null || echo bazel-testlogs)"

  local failed_targets=()
  while IFS= read -r target; do
    failed_targets+=("$target")
  done < <(
    grep -E '^(FAIL: //|ERROR: .* Testing //)' "$console_log" \
      | sed -E 's#^FAIL: (//[^ ]+).*#\1#; s#^ERROR: .* Testing (//[^ ]+) failed:.*#\1#' \
      | sort -u
  )

  if [[ ${#failed_targets[@]} -eq 0 ]]; then
    echo "No failed Bazel test targets were found in console output."
    return
  fi

  for target in "${failed_targets[@]}"; do
    local rel_path="${target#//}"
    rel_path="${rel_path/://}"
    local test_log="${testlogs_dir}/${rel_path}/test.log"
    local reported_test_log
    reported_test_log="$(grep -F "FAIL: ${target} " "$console_log" | sed -nE 's#.* \(see (.*[\\/]test\.log)\).*#\1#p' | head -n 1 || true)"
    if [[ -n "$reported_test_log" ]]; then
      reported_test_log="${reported_test_log//\\//}"
      test_log="$reported_test_log"
    fi

    echo "::group::Bazel test log tail for ${target}"
    if [[ -f "$test_log" ]]; then
      tail -n 200 "$test_log"
    else
      echo "Missing test log: $test_log"
    fi
    echo "::endgroup::"
  done
}

print_bazel_action_failure_summary() {
  local console_log="$1"
  local escaped_summary
  local summary

  summary="$(
    awk '
      function clean(line) {
        gsub(sprintf("%c", 27) "\\[[0-9;]*m", "", line)
        sub(/^.*\t[^\t]*\t[0-9TZ:._-]+ /, "", line)
        return line
      }

      function is_diagnostic(line) {
        return line ~ /^(error(\[[^]]+\])?:|warning:|note:|help:)/ ||
          line ~ /^[[:space:]]+-->/ ||
          line ~ /^[[:space:]]*[0-9]+[[:space:]]+\|/ ||
          line ~ /^[[:space:]]*\|/ ||
          line ~ /^[[:space:]]+= (note|help):/ ||
          line ~ /^[[:space:]]*\^[[:space:]^~-]*$/ ||
          line ~ /^For more information/ ||
          line ~ /^error: aborting/
      }

      {
        line = clean($0)
      }

      line ~ /^ERROR: .* failed:/ {
        if (printed) {
          print ""
        }
        print line
        in_failure = 1
        seen_diagnostic = 0
        printed = 1
        next
      }

      in_failure && is_diagnostic(line) {
        print line
        seen_diagnostic = 1
        next
      }

      in_failure && seen_diagnostic && line == "" {
        print ""
        next
      }

      in_failure && seen_diagnostic {
        in_failure = 0
        seen_diagnostic = 0
        next
      }
    ' "$console_log"
  )"

  if [[ -z "$summary" ]]; then
    summary="$(grep -E '^ERROR: |^FAILED: ' "$console_log" | tail -n 50 || true)"
  fi

  if [[ -z "$summary" ]]; then
    echo "No Bazel action failures were found in the captured console output."
    return
  fi

  if [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
    escaped_summary="$(
      printf '%s' "$summary" \
        | awk 'BEGIN { ORS = "" } {
            gsub(/%/, "%25")
            gsub(/\r/, "%0D")
            print sep $0
            sep = "%0A"
          }'
    )"
    echo "::error title=Bazel failed action diagnostics::${escaped_summary}"
  fi

  echo
  echo "Bazel failed action diagnostics:"
  echo "--------------------------------"
  printf '%s\n' "$summary"
  echo "--------------------------------"
}

print_windows_bazel_server_log_tail() {
  if [[ "${RUNNER_OS:-}" != "Windows" || -z "${BAZEL_OUTPUT_BASE:-}" ]]; then
    return
  fi

  local server_log="${BAZEL_OUTPUT_BASE}/server/jvm.out"
  echo "::group::Bazel Windows server log tail"
  if [[ -f "$server_log" ]]; then
    tail -n 200 "$server_log"
  else
    echo "Missing Bazel server log: $server_log"
  fi
  echo "::endgroup::"
}

bazel_args=()
bazel_targets=()
found_target_separator=0
for arg in "$@"; do
  if [[ "$arg" == "--" && $found_target_separator -eq 0 ]]; then
    found_target_separator=1
    continue
  fi

  if [[ $found_target_separator -eq 0 ]]; then
    bazel_args+=("$arg")
  else
    bazel_targets+=("$arg")
  fi
done

if [[ ${#bazel_args[@]} -eq 0 || ${#bazel_targets[@]} -eq 0 ]]; then
  echo "Expected Bazel args and targets separated by --" >&2
  exit 1
fi

if [[ "${RUNNER_OS:-}" == "Windows" && $windows_cross_compile -eq 1 && -z "${BUILDBUDDY_API_KEY:-}" ]]; then
  # Public forks cannot use the upstream Linux RBE pool. Keep the gnullvm
  # target ABI while retaining the hosted MSVC execution platform for helper
  # binaries and Rust proc-macros.
  ci_config=ci-windows
fi

post_config_bazel_args=()
if [[ "${RUNNER_OS:-}" == "Windows" && $windows_cross_compile -eq 1 && -z "${BUILDBUDDY_API_KEY:-}" ]]; then
  has_target_platform_override=0
  for arg in "${bazel_args[@]}"; do
    if [[ "$arg" == --platforms=* ]]; then
      has_target_platform_override=1
      break
    fi
  done

  if [[ $has_target_platform_override -eq 0 ]]; then
    post_config_bazel_args+=("--platforms=//:windows_x86_64_gnullvm")
  fi
  # Resolve test targets for the gnullvm ABI, but execute their helpers on
  # the hosted MSVC platform. Rust proc-macro DLLs must match that execution
  # platform, while ABI-scoped C++ toolchains leave gnullvm target actions on
  # hermetic LLVM. Do not hard-code link.exe for exec Rustc here: gnullvm
  # helpers receive GNU-style linker arguments, which must be routed through
  # the selected toolchain's compatible compiler driver.
  post_config_bazel_args+=(
    "--extra_execution_platforms=//:win"
    "--extra_toolchains=//:windows_gnullvm_tests_on_msvc_host_toolchain"
  )
fi
if [[ "${RUNNER_OS:-}" == "Windows" && $windows_msvc_host_platform -eq 1 ]]; then
  has_host_platform_override=0
  for arg in "${bazel_args[@]}"; do
    if [[ "$arg" == --host_platform=* ]]; then
      has_host_platform_override=1
      break
    fi
  done

  if [[ $has_host_platform_override -eq 0 ]]; then
    # Use the short MSVC Windows platform label for jobs that need helper
    # binaries like Rust test wrappers and V8 generators to resolve a compatible
    # toolchain. This keeps rules_rust bootstrap sysroot paths under the legacy
    # 260-character limit enforced by the hosted MSVC linker. Callers that need
    # a different Windows target platform should pass an explicit
    # `--platforms=...` flag.
    post_config_bazel_args+=("--host_platform=//:win")
  fi

  # Native MSVC targets use the local C++ toolchain. The public gnullvm
  # fallback above owns its exec-Rustc linker override because it executes
  # helpers on MSVC while targeting gnullvm. `--repo_env==NAME` uses Bazel's
  # explicit-unset syntax to override the repository-wide `=1` setting and let
  # the local repository detect MSVC.
  post_config_bazel_args+=(
    "--repo_env==BAZEL_DO_NOT_DETECT_CPP_TOOLCHAIN"
    "--extra_toolchains=//:windows_msvc_local_cc_toolchain"
  )
fi

if [[ $remote_download_toplevel -eq 1 ]]; then
  # Override the CI config's remote_download_minimal setting when callers need
  # the built artifact to exist on disk after the command completes.
  post_config_bazel_args+=(--remote_download_toplevel)
fi

if [[ "${RUNNER_OS:-}" == "Windows" && $windows_cross_compile -eq 1 && -n "${BUILDBUDDY_API_KEY:-}" ]]; then
  # `--enable_platform_specific_config` expands `common:windows` on Windows
  # hosts after ordinary rc configs, which can override `ci-windows-cross`'s
  # RBE host platform. Repeat the host platform on the command line so V8 and
  # other genrules execute on Linux RBE workers instead of Git Bash locally.
  #
  # Bazel also derives the default genrule shell from the client host. Without
  # an explicit shell executable, remote Linux actions can be asked to run
  # `C:\Program Files\Git\usr\bin\bash.exe`.
  post_config_bazel_args+=(--host_platform=//:rbe --shell_executable=/bin/bash)
fi

if [[ "${RUNNER_OS:-}" == "Windows" && $windows_cross_compile -eq 1 && -z "${BUILDBUDDY_API_KEY:-}" ]]; then
  # The Windows cross-compile config depends on authenticated remote
  # execution. When credentials are unavailable, keep the local build shape
  # and its lower concurrency cap.
  post_config_bazel_args+=(--jobs=8)
fi

if [[ -n "${BAZEL_REPO_CONTENTS_CACHE:-}" ]]; then
  # Windows self-hosted runners can run multiple Bazel jobs concurrently. Give
  # each job its own repo contents cache so they do not fight over the shared
  # path configured in `ci-windows`.
  post_config_bazel_args+=("--repo_contents_cache=${BAZEL_REPO_CONTENTS_CACHE}")
fi

if [[ -n "${BAZEL_REPOSITORY_CACHE:-}" ]]; then
  post_config_bazel_args+=("--repository_cache=${BAZEL_REPOSITORY_CACHE}")
fi

if [[ -n "${CODEX_BAZEL_EXECUTION_LOG_COMPACT_DIR:-}" ]]; then
  post_config_bazel_args+=(
    "--execution_log_compact_file=${CODEX_BAZEL_EXECUTION_LOG_COMPACT_DIR}/execution-log-${bazel_args[0]}-${GITHUB_JOB:-local}-$$.zst"
  )
fi

if [[ "${RUNNER_OS:-}" == "Windows" ]]; then
  # Windows module extensions can add platform-specific state to the committed
  # lockfile during a build. CI validates the existing dependency graph and
  # must not leave that generated state in the checkout.
  post_config_bazel_args+=(--lockfile_mode=off)

  pass_windows_build_env=1
  if [[ $windows_cross_compile -eq 1 && -n "${BUILDBUDDY_API_KEY:-}" ]]; then
    # Remote build actions execute on Linux RBE workers. Passing the Windows
    # runner's build environment there makes Bazel genrules try to execute
    # C:\Program Files\Git\usr\bin\bash.exe on Linux.
    pass_windows_build_env=0
  fi

  if [[ $pass_windows_build_env -eq 1 ]]; then
    windows_action_env_vars=(
      INCLUDE
      LIB
      LIBPATH
      UCRTVersion
      UniversalCRTSdkDir
      VCINSTALLDIR
      VCToolsInstallDir
      WindowsLibPath
      WindowsSdkBinPath
      WindowsSdkDir
      WindowsSDKLibVersion
      WindowsSDKVersion
    )

    for env_var in "${windows_action_env_vars[@]}"; do
      if [[ -n "${!env_var:-}" ]]; then
        # Refer to the value inherited by the Bazel client rather than embed
        # the often very long MSVC SDK value in every command invocation. The
        # Windows batch-mode client has a 32,768-character command-line cap.
        post_config_bazel_args+=("--action_env=${env_var}" "--host_action_env=${env_var}")
      fi
    done
  fi

  if [[ -z "${CODEX_BAZEL_WINDOWS_PATH:-}" ]]; then
    echo "CODEX_BAZEL_WINDOWS_PATH must be set for Windows Bazel CI." >&2
    exit 1
  fi

  if [[ $pass_windows_build_env -eq 1 ]]; then
    post_config_bazel_args+=(
      # The Python launcher gives its Bazel subprocess this cache-stable PATH.
      # Passing the variable name keeps it out of the Windows batch command.
      "--action_env=PATH"
      "--host_action_env=PATH"
    )
  elif [[ $windows_cross_compile -eq 1 ]]; then
    # Remote build actions run on Linux RBE workers. Give their shell snippets
    # a Linux PATH while preserving CODEX_BAZEL_WINDOWS_PATH below for local
    # Windows test execution.
    post_config_bazel_args+=(
      "--action_env=PATH=/usr/bin:/bin"
      "--host_action_env=PATH=/usr/bin:/bin"
    )
  fi
  post_config_bazel_args+=("--test_env=PATH")
fi

bazel_target_pattern_file=""
bazel_target_pattern_arg=""
if [[ "${RUNNER_OS:-}" == "Windows" ]]; then
  # Bazel 9 accepts newline-delimited target patterns from a file. Windows test
  # shards and clippy invocations can exceed the 32,768-character process
  # command-line limit solely because they enumerate hundreds of labels.
  bazel_target_pattern_file="$(mktemp "${PWD}/.bazel-target-patterns.XXXXXX")"
  printf '%s\n' "${bazel_targets[@]}" > "$bazel_target_pattern_file"
  # Git Bash represents the workspace as `/d/...`, but the native Bazel client
  # requires a drive-qualified path to read this file in batch mode.
  bazel_target_pattern_arg="$(cygpath -w "$bazel_target_pattern_file")"
  bazel_targets=()
  post_config_bazel_args+=("--target_pattern_file=$bazel_target_pattern_arg")
fi

bazel_console_log="$(mktemp)"
cleanup_bazel_ci_files() {
  rm -f "$bazel_console_log"
  if [[ -n "$bazel_target_pattern_file" ]]; then
    rm -f "$bazel_target_pattern_file"
  fi
}
trap cleanup_bazel_ci_files EXIT

bazel_run_args=(
  "${bazel_args[@]}"
)
if [[ -n "${BUILDBUDDY_API_KEY:-}" ]]; then
  echo "BuildBuddy API key is available; using remote Bazel configuration."
  bazel_run_args+=("--config=${ci_config}")
else
  echo "BuildBuddy API key is not available; using local Bazel configuration."
fi
if (( ${#post_config_bazel_args[@]} > 0 )); then
  bazel_run_args+=("${post_config_bazel_args[@]}")
fi
set +e
# Work around Bazel 9 remote repo contents cache / overlay materialization
# failures seen in CI (for example "is not a symlink" or permission errors
# while materializing external repos such as rules_perl). This only disables
# the startup-level repo contents cache; keyed runs still use BuildBuddy.
bazel_invocation_args=(
  --noexperimental_remote_repo_contents_cache
  "${bazel_run_args[@]}"
)
if (( ${#bazel_targets[@]} > 0 )); then
  bazel_invocation_args+=(-- "${bazel_targets[@]}")
fi
run_bazel_with_startup_args \
  "${bazel_invocation_args[@]}" \
  2>&1 | tee "$bazel_console_log"
bazel_status=${PIPESTATUS[0]}
set -e

if [[ ${bazel_status:-0} -ne 0 ]]; then
  if [[ "${RUNNER_OS:-}" == "Windows" ]] && grep -q "Server terminated abruptly" "$bazel_console_log"; then
    print_windows_bazel_server_log_tail
  fi
  if [[ $print_failed_bazel_action_summary -eq 1 ]]; then
    print_bazel_action_failure_summary "$bazel_console_log"
  fi
  if [[ $print_failed_bazel_test_logs -eq 1 ]]; then
    print_bazel_test_log_tails "$bazel_console_log"
  fi
  exit "$bazel_status"
fi
