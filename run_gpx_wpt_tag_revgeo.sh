#!/usr/bin/env bash

set -euo pipefail

# -----------------------------------------------------------------------------
# GPX Trail Waypoint Generator container wrapper
# Priority 3: accuracy and GPX compatibility validation, while preserving the
# Priority 1 file-safety model and Priority 2 wrapper interface.
# -----------------------------------------------------------------------------

readonly PROGRAM_NAME="${0##*/}"
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly DOCKERFILE_PATH="${SCRIPT_DIR}/Dockerfile"
readonly REQUIREMENTS_PATH="${SCRIPT_DIR}/requirements.txt"
readonly APPLICATION_PATH="${SCRIPT_DIR}/gpx_wpt_tag_revgeo.py"

IMAGE_NAME="${IMAGE_NAME:-trailone/gpx-wpt-tag-revgeo}"
DOCKER_BIN="${DOCKER_BIN:-docker}"
CONTAINER_NAME="${CONTAINER_NAME:-gpx-runner-$$-${RANDOM}}"

DISTANCE_METHOD="auto"
REBUILD_IMAGE=false
OVERWRITE=false

GPX_FILE=""
TRAIL_PREFIX=""
STEP_KM=""
OUTPUT_FILE=""

INPUT_PATH=""
INPUT_DIRECTORY=""
INPUT_FILENAME=""
OUTPUT_DIRECTORY=""
OUTPUT_FILENAME=""
OUTPUT_PATH=""
NORMALIZED_STEP=""

STAGING_DIRECTORY=""
STAGING_DIRECTORY_NAME=""
STAGING_INPUT_PATH=""
STAGED_OUTPUT_PATH=""

DOCKER_RUN_PID=""
CONTAINER_ACTIVE=false
DOCKER_RUNTIME_ARGS=()

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  readonly GREEN=$'\033[0;32m'
  readonly YELLOW=$'\033[1;33m'
  readonly NC=$'\033[0m'
else
  readonly GREEN=""
  readonly YELLOW=""
  readonly NC=""
fi

if [[ -t 2 && -z "${NO_COLOR:-}" ]]; then
  readonly RED=$'\033[0;31m'
  readonly ERR_NC=$'\033[0m'
else
  readonly RED=""
  readonly ERR_NC=""
fi

usage() {
  local status="${1:-0}"
  local stream=1

  if (( status != 0 )); then
    stream=2
  fi

  {
    printf '%bGPX Trail Waypoint Generator Wrapper%b\n\n' "$YELLOW" "$NC"
    printf 'Usage:\n'
    printf '  %s --file <GPX_FILE> --prefix <PREFIX> --step <KM> [options]\n\n' "$PROGRAM_NAME"
    printf 'Required:\n'
    printf '  --file FILE              Input GPX track file\n'
    printf '  --prefix PREFIX          3-6 uppercase waypoint-name letters\n'
    printf '  --step KM                Marker interval in km; range (0, 100]\n\n'
    printf 'Options:\n'
    printf '  --distance-method METHOD auto|geodesic|haversine (default: auto)\n'
    printf '  --output FILE            Wrapper-managed host output GPX path\n'
    printf '  --overwrite              Replace an existing host output file\n'
    printf '  --rebuild                Force an uncached Docker image rebuild\n'
    printf '  -h, --help               Show this help\n\n'
    printf 'Environment:\n'
    printf '  IMAGE_NAME               Docker image tag (default: gpx-trail-wpt)\n'
    printf '  DOCKER_BIN               Docker executable (default: docker)\n'
    printf '  CONTAINER_NAME           Processing container name override\n'
    printf '  NO_COLOR                 Disable ANSI color output\n\n'
    printf 'Notes:\n'
    printf '  --output and --overwrite are wrapper interfaces. The Python CLI\n'
    printf '  receives the three positional arguments and --distance-method only.\n'
    printf '  Application failures are propagated with their nonzero exit status.\n'
    printf '  Staged output must use the GPX 1.1 root namespace and version.\n\n'
    printf 'Example:\n'
    printf '  %s --file trail.gpx --prefix HIK --step 1.5 \\\n' "$PROGRAM_NAME"
    printf '    --distance-method haversine --output ./output/trail_wpt.gpx\n'
  } >&"$stream"

  return "$status"
}

error() {
  printf '%bError:%b %s\n' "$RED" "$ERR_NC" "$*" >&2
}

status_message() {
  local color="$1"
  shift
  printf '%b%s%b\n' "$color" "$*" "$NC"
}

require_option_value() {
  local option="$1"
  local remaining="$2"

  if (( remaining < 2 )); then
    error "Option '${option}' requires a value."
    usage 2
    exit 2
  fi
}

path_entry_exists() {
  [[ -e "$1" || -L "$1" ]]
}

parse_arguments() {
  while (( $# > 0 )); do
    case "$1" in
      --file)
        require_option_value "$1" "$#"
        GPX_FILE="$2"
        shift 2
        ;;
      --file=*)
        GPX_FILE="${1#*=}"
        shift
        ;;
      --prefix)
        require_option_value "$1" "$#"
        TRAIL_PREFIX="$2"
        shift 2
        ;;
      --prefix=*)
        TRAIL_PREFIX="${1#*=}"
        shift
        ;;
      --step)
        require_option_value "$1" "$#"
        STEP_KM="$2"
        shift 2
        ;;
      --step=*)
        STEP_KM="${1#*=}"
        shift
        ;;
      --distance-method)
        require_option_value "$1" "$#"
        DISTANCE_METHOD="$2"
        shift 2
        ;;
      --distance-method=*)
        DISTANCE_METHOD="${1#*=}"
        shift
        ;;
      --output)
        require_option_value "$1" "$#"
        OUTPUT_FILE="$2"
        shift 2
        ;;
      --output=*)
        OUTPUT_FILE="${1#*=}"
        shift
        ;;
      --overwrite)
        OVERWRITE=true
        shift
        ;;
      --rebuild)
        REBUILD_IMAGE=true
        shift
        ;;
      -h|--help)
        usage 0
        exit 0
        ;;
      --)
        shift
        if (( $# > 0 )); then
          error "Unexpected positional arguments: $*"
          usage 2
          exit 2
        fi
        ;;
      -*)
        error "Unknown option: $1"
        usage 2
        exit 2
        ;;
      *)
        error "Unexpected positional argument: $1"
        usage 2
        exit 2
        ;;
    esac
  done
}

validate_wrapper_arguments() {
  if [[ -z "$GPX_FILE" || -z "$TRAIL_PREFIX" || -z "$STEP_KM" ]]; then
    error "Missing required arguments."
    usage 2
    exit 2
  fi

  if [[ -L "$GPX_FILE" && ! -e "$GPX_FILE" ]]; then
    error "Input is a broken symbolic link: ${GPX_FILE}"
    exit 2
  fi

  if [[ ! -e "$GPX_FILE" ]]; then
    error "File '${GPX_FILE}' does not exist."
    exit 2
  fi

  if [[ ! -f "$GPX_FILE" ]]; then
    error "'${GPX_FILE}' is not a regular file."
    exit 2
  fi

  if [[ ! -r "$GPX_FILE" ]]; then
    error "File '${GPX_FILE}' is not readable."
    exit 2
  fi

  case "$GPX_FILE" in
    *.[gG][pP][xX]) ;;
    *)
      error "Input file must use the '.gpx' extension."
      exit 2
      ;;
  esac

  if [[ ! "$TRAIL_PREFIX" =~ ^[A-Z]{3,6}$ ]]; then
    error "Prefix must contain 3-6 uppercase letters."
    exit 3
  fi

  if [[ ! "$STEP_KM" =~ ^[+-]?(([0-9]+([.][0-9]*)?)|([.][0-9]+))([eE][+-]?[0-9]+)?$ ]]; then
    error "Step size must be a finite numeric value."
    exit 4
  fi

  case "$DISTANCE_METHOD" in
    auto|geodesic|haversine) ;;
    *)
      error "Invalid distance method '${DISTANCE_METHOD}'."
      printf 'Allowed values: auto | geodesic | haversine\n' >&2
      exit 5
      ;;
  esac
}

validate_build_environment() {
  if ! command -v "$DOCKER_BIN" >/dev/null 2>&1; then
    error "Docker executable '${DOCKER_BIN}' was not found in PATH."
    exit 127
  fi

  local required_file
  for required_file in \
    "$DOCKERFILE_PATH" \
    "$REQUIREMENTS_PATH" \
    "$APPLICATION_PATH"; do
    if [[ ! -f "$required_file" || ! -r "$required_file" ]]; then
      error "Required build file is missing or unreadable: ${required_file}"
      exit 6
    fi
  done

  if ! "$DOCKER_BIN" info >/dev/null 2>&1; then
    error "Docker daemon is unavailable or inaccessible."
    exit 6
  fi
}

canonicalize_existing_directory() {
  local directory="$1"
  (
    cd -- "$directory"
    pwd -P
  )
}

validate_mount_path() {
  local path="$1"

  if [[ "$path" == *,* ]]; then
    error "Docker --mount cannot safely represent a host path containing a comma: ${path}"
    exit 7
  fi
}

resolve_input_path() {
  local input_directory
  local input_name

  input_directory="$(dirname -- "$GPX_FILE")"
  input_name="$(basename -- "$GPX_FILE")"
  input_directory="$(canonicalize_existing_directory "$input_directory")"

  INPUT_DIRECTORY="$input_directory"
  INPUT_FILENAME="$input_name"
  INPUT_PATH="${INPUT_DIRECTORY}/${INPUT_FILENAME}"
  validate_mount_path "$INPUT_PATH"
}

resolve_output_directory() {
  local output_directory
  local output_name

  if [[ -n "$OUTPUT_FILE" ]]; then
    output_directory="$(dirname -- "$OUTPUT_FILE")"
    output_name="$(basename -- "$OUTPUT_FILE")"

    if [[ ! -d "$output_directory" ]]; then
      error "Output directory does not exist: ${output_directory}"
      exit 7
    fi

    output_directory="$(canonicalize_existing_directory "$output_directory")"

    case "$output_name" in
      *.[gG][pP][xX]) ;;
      *)
        error "Output file must use the '.gpx' extension."
        exit 7
        ;;
    esac

    OUTPUT_DIRECTORY="$output_directory"
    OUTPUT_FILENAME="$output_name"
    OUTPUT_PATH="${OUTPUT_DIRECTORY}/${OUTPUT_FILENAME}"
  else
    OUTPUT_DIRECTORY="$INPUT_DIRECTORY"
    OUTPUT_FILENAME=""
    OUTPUT_PATH=""
  fi

  validate_mount_path "$OUTPUT_DIRECTORY"
}

validate_output_target() {
  if [[ -z "$OUTPUT_PATH" ]]; then
    error "Internal error: output path has not been resolved."
    exit 70
  fi

  if [[ "$INPUT_PATH" == "$OUTPUT_PATH" ]] \
    || { path_entry_exists "$OUTPUT_PATH" && [[ "$INPUT_PATH" -ef "$OUTPUT_PATH" ]]; }; then
    error "Output file must not replace or alias the input GPX file."
    exit 7
  fi

  if path_entry_exists "$OUTPUT_PATH"; then
    if [[ -d "$OUTPUT_PATH" ]]; then
      error "Output path is a directory: ${OUTPUT_PATH}"
      exit 7
    fi

    if [[ ! -f "$OUTPUT_PATH" && ! -L "$OUTPUT_PATH" ]]; then
      error "Output path is not a regular file or symbolic link: ${OUTPUT_PATH}"
      exit 7
    fi

    if [[ "$OVERWRITE" != true ]]; then
      error "Output file already exists: ${OUTPUT_PATH}. Use --overwrite to replace it."
      exit 7
    fi
  fi
}

build_image() {
  local -a build_command=(
    "$DOCKER_BIN" build
    --file "$DOCKERFILE_PATH"
    --tag "$IMAGE_NAME"
  )

  if [[ "$REBUILD_IMAGE" == true ]]; then
    build_command+=(--no-cache)
    status_message "$YELLOW" "Rebuilding Docker image '${IMAGE_NAME}' without cache..."
  else
    status_message "$GREEN" "Validating Docker image '${IMAGE_NAME}' against the current build context..."
  fi

  build_command+=("$SCRIPT_DIR")
  "${build_command[@]}"
}

create_staging_directory() {
  if ! STAGING_DIRECTORY="$(mktemp -d "${OUTPUT_DIRECTORY}/.gpx-wrapper.XXXXXX")"; then
    error "Cannot create a staging directory in '${OUTPUT_DIRECTORY}'. Check ownership and write access."
    exit 7
  fi

  STAGING_DIRECTORY_NAME="$(basename -- "$STAGING_DIRECTORY")"
  STAGING_INPUT_PATH="${STAGING_DIRECTORY}/input.gpx"
  : > "$STAGING_INPUT_PATH"
}

docker_runtime_base_args() {
  DOCKER_RUNTIME_ARGS=(
    --user "$(id -u):$(id -g)"
    --read-only
    --cap-drop ALL
    --security-opt no-new-privileges
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m
  )
}

runtime_preflight() {
  local container_input="/output/${STAGING_DIRECTORY_NAME}/input.gpx"
  local container_stage="/output/${STAGING_DIRECTORY_NAME}"

  local -a preflight_command=(
    "$DOCKER_BIN" run --rm
    "${DOCKER_RUNTIME_ARGS[@]}"
    --entrypoint /usr/local/bin/python
    --mount "type=bind,source=${OUTPUT_DIRECTORY},target=/output"
    --mount "type=bind,source=${INPUT_PATH},target=${container_input},readonly"
    "$IMAGE_NAME"
    -c
    $'import math\nimport os\nimport sys\nimport tempfile\n\nraw_step, input_path, stage_dir = sys.argv[1:4]\ntry:\n    step = float(raw_step)\nexcept (TypeError, ValueError):\n    print("Preflight error: step size is not numeric.", file=sys.stderr)\n    raise SystemExit(4)\nif not math.isfinite(step) or step <= 0 or step > 100:\n    print("Preflight error: step size must be finite and within (0, 100].", file=sys.stderr)\n    raise SystemExit(4)\ntry:\n    with open(input_path, "rb") as handle:\n        handle.read(1)\nexcept OSError as exc:\n    print(f"Preflight error: input is not readable: {exc}", file=sys.stderr)\n    raise SystemExit(2)\nprobe = link = None\ntry:\n    descriptor, probe = tempfile.mkstemp(prefix=".gpx-wrapper-probe-", dir=stage_dir)\n    with os.fdopen(descriptor, "wb") as handle:\n        handle.write(b"probe")\n        handle.flush()\n        os.fsync(handle.fileno())\n    link = probe + ".link"\n    os.link(probe, link)\nexcept OSError as exc:\n    print(f"Preflight error: output filesystem lacks required write/hard-link support: {exc}", file=sys.stderr)\n    raise SystemExit(7)\nfinally:\n    for path in (link, probe):\n        if path is not None:\n            try:\n                os.unlink(path)\n            except FileNotFoundError:\n                pass\nprint(str(step))'
    "$STEP_KM"
    "$container_input"
    "$container_stage"
  )

  local preflight_status
  set +e
  NORMALIZED_STEP="$("${preflight_command[@]}")"
  preflight_status=$?
  set -e

  if (( preflight_status != 0 )); then
    exit "$preflight_status"
  fi

  if [[ -z "$OUTPUT_FILENAME" ]]; then
    OUTPUT_FILENAME="$(basename -- "${INPUT_PATH%.*}_${NORMALIZED_STEP}_wpt.gpx")"
    OUTPUT_PATH="${OUTPUT_DIRECTORY}/${OUTPUT_FILENAME}"
  fi

  STAGED_OUTPUT_PATH="${STAGING_DIRECTORY}/input_${NORMALIZED_STEP}_wpt.gpx"
  validate_output_target
}

forward_signal() {
  local signal="$1"

  if [[ "$CONTAINER_ACTIVE" == true ]]; then
    if ! "$DOCKER_BIN" kill --signal "$signal" "$CONTAINER_NAME" >/dev/null 2>&1; then
      if [[ -n "$DOCKER_RUN_PID" ]]; then
        kill -s "$signal" "$DOCKER_RUN_PID" >/dev/null 2>&1 || true
      fi
    fi
  fi
}

cleanup_all() {
  if [[ "$CONTAINER_ACTIVE" == true ]]; then
    "$DOCKER_BIN" rm --force "$CONTAINER_NAME" >/dev/null 2>&1 || true
  fi

  if [[ -n "$STAGING_DIRECTORY" \
        && -d "$STAGING_DIRECTORY" \
        && "$STAGING_DIRECTORY" == "${OUTPUT_DIRECTORY}/.gpx-wrapper."* ]]; then
    rm -rf -- "$STAGING_DIRECTORY" || true
  fi
}

run_container() {
  local container_input="/output/${STAGING_DIRECTORY_NAME}/input.gpx"

  local -a run_command=(
    "$DOCKER_BIN" run --rm
    --name "$CONTAINER_NAME"
    "${DOCKER_RUNTIME_ARGS[@]}"
    --mount "type=bind,source=${OUTPUT_DIRECTORY},target=/output"
    --mount "type=bind,source=${INPUT_PATH},target=${container_input},readonly"
    "$IMAGE_NAME"
    "$container_input"
    "$TRAIL_PREFIX"
    "$STEP_KM"
    --distance-method "$DISTANCE_METHOD"
  )

  status_message "$GREEN" "Running GPX processing container..."

  trap 'forward_signal INT' INT
  trap 'forward_signal TERM' TERM

  set +e
  "${run_command[@]}" &
  DOCKER_RUN_PID=$!
  CONTAINER_ACTIVE=true

  local run_status
  while true; do
    wait "$DOCKER_RUN_PID"
    run_status=$?

    if ! kill -0 "$DOCKER_RUN_PID" >/dev/null 2>&1; then
      break
    fi
  done
  set -e

  CONTAINER_ACTIVE=false
  trap - INT TERM

  if (( run_status != 0 )); then
    exit "$run_status"
  fi
}

install_output() {
  local source_relative="${STAGING_DIRECTORY_NAME}/$(basename -- "$STAGED_OUTPUT_PATH")"

  local -a install_command=(
    "$DOCKER_BIN" run --rm
    "${DOCKER_RUNTIME_ARGS[@]}"
    --entrypoint /usr/local/bin/python
    --mount "type=bind,source=${OUTPUT_DIRECTORY},target=/output"
    "$IMAGE_NAME"
    -c
    $'import os\nimport stat\nimport sys\nimport xml.etree.ElementTree as ET\n\nGPX_NAMESPACE = "http://www.topografix.com/GPX/1/1"\nsource = os.path.join("/output", sys.argv[1])\ndestination = os.path.join("/output", sys.argv[2])\noverwrite = sys.argv[3] == "true"\n\ntry:\n    source_stat = os.stat(source, follow_symlinks=False)\nexcept OSError as exc:\n    print(f"Install error: staged output is unavailable: {exc}", file=sys.stderr)\n    raise SystemExit(7)\nif not stat.S_ISREG(source_stat.st_mode) or source_stat.st_size == 0:\n    print("Install error: staged output is not a nonempty regular file.", file=sys.stderr)\n    raise SystemExit(7)\ntry:\n    root = ET.parse(source).getroot()\nexcept Exception as exc:\n    print(f"Install error: staged output is not well-formed XML: {exc}", file=sys.stderr)\n    raise SystemExit(7)\nif root.tag != f"{{{GPX_NAMESPACE}}}gpx" or root.get("version") != "1.1" or not root.get("creator", "").strip():\n    print("Install error: staged output does not satisfy the GPX 1.1 root contract.", file=sys.stderr)\n    raise SystemExit(7)\nif os.path.lexists(destination):\n    try:\n        if os.path.samefile(source, destination):\n            print("Install error: staged and destination paths alias the same file.", file=sys.stderr)\n            raise SystemExit(7)\n    except OSError:\n        pass\n    if os.path.isdir(destination):\n        print("Install error: destination is a directory.", file=sys.stderr)\n        raise SystemExit(7)\n    if not overwrite:\n        print("Install error: destination appeared before installation; no file was overwritten.", file=sys.stderr)\n        raise SystemExit(7)\ntry:\n    if overwrite:\n        os.replace(source, destination)\n    else:\n        os.link(source, destination)\n        try:\n            os.unlink(source)\n        except OSError:\n            pass\nexcept PermissionError as exc:\n    print(f"Install error: permission denied while committing output: {exc}", file=sys.stderr)\n    raise SystemExit(7)\nexcept FileExistsError:\n    print("Install error: destination appeared before installation; no file was overwritten.", file=sys.stderr)\n    raise SystemExit(7)\nexcept OSError as exc:\n    print(f"Install error: atomic output installation failed: {exc}", file=sys.stderr)\n    raise SystemExit(7)\ntry:\n    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)\n    descriptor = os.open("/output", flags)\n    try:\n        os.fsync(descriptor)\n    finally:\n        os.close(descriptor)\nexcept OSError:\n    pass'
    "$source_relative"
    "$OUTPUT_FILENAME"
    "$OVERWRITE"
  )

  "${install_command[@]}"
}

print_success() {
  if [[ ! -f "$OUTPUT_PATH" || ! -s "$OUTPUT_PATH" ]]; then
    error "Container reported success, but the expected output is missing or empty: ${OUTPUT_PATH}"
    exit 7
  fi

  printf '%bSuccess!%b\n' "$GREEN" "$NC"
  printf '  Output file: %b%s%b\n' "$YELLOW" "$OUTPUT_PATH" "$NC"
  printf '  Distance method: %s\n' "$DISTANCE_METHOD"
  printf '  Waypoints include:\n'
  printf '    Trail Head, Trail End, Highest, Lowest,\n'
  printf '    Halfway, and every %s km.\n' "$NORMALIZED_STEP"
}

main() {
  parse_arguments "$@"
  validate_wrapper_arguments
  validate_build_environment
  resolve_input_path
  resolve_output_directory

  # Reject explicit destructive or aliasing destinations before image build.
  # Default naming depends on Python float normalization and is validated after
  # the runtime preflight computes the same normalized step representation.
  if [[ -n "$OUTPUT_PATH" ]]; then
    validate_output_target
  fi

  build_image
  create_staging_directory
  trap cleanup_all EXIT
  docker_runtime_base_args
  runtime_preflight
  run_container
  install_output
  print_success
}

main "$@"
