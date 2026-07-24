# GPX Waypoint Tag Reverse Geocoding Tool

This repository contains a GPX enrichment workflow that adds trail-aware waypoint markers to GPX tracks, computes elevation statistics, and optionally reverse-geocodes each marker with Nominatim. The implementation is split into three source components:

- [gpx_wpt_tag_revgeo.py](gpx_wpt_tag_revgeo.py) for the core Python CLI and processing logic
- [Dockerfile](Dockerfile) for a reproducible runtime image
- [run_gpx_wpt_tag_revgeo.sh](run_gpx_wpt_tag_revgeo.sh) for a safe Docker wrapper with validation and staging

## Source 1: gpx_wpt_tag_revgeo.py

### 1. Introduction

#### 1.1 Purpose
The Python entry point analyzes a GPX file, validates its structure, computes trail statistics, and generates enriched GPX output with semantic waypoints such as trail head, trail end, highest point, lowest point, halfway point, and kilometre markers.

#### 1.2 Scope
The script is responsible for:
- parsing GPX files with gpxpy
- validating track geometry and coordinate values
- computing elevation statistics
- calculating cumulative 3D distances
- generating waypoint names following the repository convention
- reverse-geocoding marker coordinates using Nominatim
- writing a GPX 1.1 document atomically to disk

#### 1.3 Intended Audience
- hikers and trail maintainers who want enriched GPX exports
- developers maintaining the waypoint generation workflow
- operators running the tool from a shell or container

#### 1.4 Terminology
- GPX: GPS Exchange Format, a standard XML schema for GPS data
- Waypoint: a named geographic point embedded in the output GPX
- Reverse geocoding: converting coordinates to a human-readable place name
- Segment: a contiguous track sequence inside the GPX structure

### 2. Requirements

#### 2.1 Runtime Requirements
- Python 3.13 or newer is recommended for compatibility with the current syntax and runtime assumptions
- Network access is required if reverse geocoding is used
- A readable GPX file with at least two track points

#### 2.2 Dependencies
Install the Python packages listed in [requirements.txt](requirements.txt):
- gpxpy==1.6.2
- geopy==2.5.0
- requests==2.34.2

#### 2.3 Compatibility
The script expects GPX input that contains at least one track and at least one track segment with two or more points. It writes GPX 1.1 output and preserves existing waypoints, routes, and tracks when possible.

### 3. Installation

#### 3.1 Standard Installation
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

#### 3.2 Development Installation
For local development:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest black mypy
```

#### 3.3 Verification
Confirm that the CLI starts correctly:
```bash
python gpx_wpt_tag_revgeo.py --help
```

### 4. Configuration
The script supports the following environment variables:
- NOMINATIM_API_URL: override the reverse-geocoding endpoint
- NOMINATIM_USER_AGENT: set a custom User-Agent header for HTTP requests

The default behaviour is to call Nominatim at the public reverse endpoint with a minimum request interval and retry policy.

### 5. User Guide

#### 5.1 Basic Usage
```bash
python gpx_wpt_tag_revgeo.py data/cerro-mogoton-trail-mgtn.gpx HIK 1
```
This creates an output file named like:
```text
data/cerro-mogoton-trail-mgtn_1_wpt.gpx
```

#### 5.2 CLI/API Reference
CLI synopsis:
```bash
python gpx_wpt_tag_revgeo.py GPX_FILE TRAIL_PREFIX STEP_SIZE [--distance-method auto|geodesic|haversine]
```
Arguments:
- GPX_FILE: input GPX file path
- TRAIL_PREFIX: three-letter uppercase prefix such as HIK
- STEP_SIZE: positive whole-kilometre interval between cumulative markers
- --distance-method: chooses the horizontal distance algorithm

#### 5.3 Examples
```bash
python gpx_wpt_tag_revgeo.py trail.gpx HIK 1
python gpx_wpt_tag_revgeo.py trail.gpx HIK 2 --distance-method haversine
```

#### 5.4 Input Specification
The input file must:
- use the .gpx extension
- contain at least one track with at least one track segment
- contain at least two track points
- use finite latitude, longitude, and elevation values where provided

#### 5.5 Output Specification
The script writes:
- a new GPX 1.1 file with the original geometry preserved
- generated waypoints named using the prefix, including TH, TE, HE, LE, MP, and KM markers
- metadata and bounds derived from the input content

### 6. Technical Design

#### 6.1 Architecture
The script is a single-file processing pipeline with three main phases:
1. parse and validate GPX content
2. compute elevation and distance metrics
3. generate and serialize enriched GPX output

#### 6.2 Components
- validation helpers for coordinate, elevation, and prefix constraints
- parsing helpers for GPX structure and metadata
- distance calculation logic with geodesic or haversine support
- waypoint generation logic with semantic and cumulative markers
- GPX serialization and atomic write handling

#### 6.3 Data Flow
1. The GPX file is parsed into a gpxpy object.
2. Track points are validated and normalized.
3. Segment-aware distance calculations establish cumulative positions.
4. Semantic and kilometre waypoints are created.
5. The output GPX is written atomically to disk.

#### 6.4 Processing Workflow
- Compute elevation statistics first.
- Generate waypoints from the track geometry.
- Preserve existing GPX content while adding generated markers.
- Validate the output XML before replacing the destination file.

#### 6.5 Algorithms
- distance calculations use either geodesic distance from geopy or a haversine approximation
- waypoint placement interpolates latitude/longitude across a segment
- waypoint ordering is deterministic and uses a semantic tie-break order

### 7. Developer Reference

#### 7.1 Modules
The script uses the standard library plus:
- gpxpy for GPX parsing and model objects
- requests for Nominatim HTTP requests
- geopy.distance.geodesic for geodesic distances

#### 7.2 Classes
The implementation mostly uses functions and gpxpy model objects rather than custom classes.

#### 7.3 Functions
Key functions include:
- reverse_geocode(): performs reverse geocoding with retries and caching
- validate_inputs(): checks file, CLI, and GPX structure validity
- parse_gpx_file(): extracts track points and verifies point counts
- calculate_3d_distance(): computes point-to-point distances
- _generate_waypoints_from_segments(): builds waypoint markers
- save_gpx_file(): writes the final GPX document safely
- main(): wires the CLI workflow

#### 7.4 Data Structures
- lists of GPX track points
- dictionaries of elevation statistics
- tuples used internally to sort generated waypoints deterministically

#### 7.5 Public Interfaces
The main public operations are the CLI entry point and the helper functions that can be imported by other Python code.

### 8. Reliability

#### 8.1 Validation
The script validates:
- trail prefix format
- step size being a positive whole kilometre
- coordinate ranges and elevation values
- GPX structure and minimum point counts
- generated waypoint names for uniqueness and naming convention

#### 8.2 Error Handling
Failures are converted into descriptive error messages and terminate the CLI with a non-zero exit code.

#### 8.3 Exit Codes
- 1: general processing failure

#### 8.4 Logging
The script uses Python logging through the module logger for reverse-geocoding notices and warnings.

#### 8.5 Edge Cases
- no elevation data available for statistics
- missing or empty reverse-geocoding response
- very large GPX files
- invalid or extreme coordinates

### 9. Testing

#### 9.1 Test Strategy
Testing should target:
- valid GPX parsing
- invalid GPX structure rejection
- waypoint naming and ordering
- reverse-geocoding failures without stopping processing
- atomic file output behaviour

#### 9.2 Test Cases
Recommended cases:
- single-segment GPX with two points
- multi-segment GPX with mixed elevation values
- input with invalid coordinates
- input with no elevation values
- input that triggers Nominatim failure or rate limiting

#### 9.3 Regression Testing
Any change to waypoint naming, distance calculation, metadata behavior, or file-writing logic should be validated with sample GPX files from the repository.

### 10. Security
The script relies on outbound network access for reverse geocoding. Use a trustworthy Nominatim endpoint and avoid exposing sensitive credentials. The tool should not be run with elevated privileges unless strictly required.

### 11. Performance
- reverse geocoding is cached in-process for repeated coordinates
- requests are rate-limited to avoid hammering the service
- the auto distance method switches to haversine for larger datasets to reduce computational cost

### 12. Maintenance

#### 12.1 Repository Structure
Key files:
- gpx_wpt_tag_revgeo.py: implementation
- requirements.txt: runtime dependencies
- data/: sample GPX inputs for test and demo use

#### 12.2 Development Workflow
1. update the script
2. test locally with sample GPX files
3. verify the CLI and generated output
4. update documentation when behaviour changes

#### 12.3 Coding Standards
- keep functions small and focused
- preserve deterministic waypoint ordering
- validate inputs early and clearly
- prefer descriptive error messages over silent failure

#### 12.4 Versioning
The current implementation targets a stable CLI contract; changes to positional arguments or output names should be treated as breaking changes unless explicitly documented.

#### 12.5 Changelog
Track changes in the repository history and update the README when new features or output format changes are introduced.

#### 12.6 Deprecation
Any deprecated CLI flags or output conventions should remain supported for at least one release cycle and be documented clearly.

### 13. Troubleshooting
- If the CLI reports an input file error, confirm that the file exists, is readable, and ends in .gpx.
- If reverse geocoding fails, the run still continues but waypoints may have empty descriptions.
- If the output file cannot be written, verify directory permissions and free disk space.
- If the script exits early, inspect the stderr message for validation or network-related details.

### 14. Glossary
- Elevation statistics: ascent, descent, maximum, minimum, and range
- Trail prefix: three-letter uppercase prefix used for generated names
- Marker interval: whole-kilometre spacing between cumulative markers

### 15. References
- GPX 1.1 specification
- gpxpy documentation
- geopy documentation
- requests documentation
- Nominatim usage policy

---

## Source 2: Dockerfile

### 1. Introduction

#### 1.1 Purpose
The Dockerfile packages the Python application into a small container image with the required runtime dependencies and a non-root execution model.

#### 1.2 Scope
It defines:
- the base Python image
- dependency installation
- application copy step
- runtime user and working directories
- the default container entrypoint

#### 1.3 Intended Audience
- developers building the image locally
- operators deploying the tool in a containerized environment
- maintainers updating the runtime stack

#### 1.4 Terminology
- image: a packaged filesystem and runtime definition
- entrypoint: the command executed when the container starts
- layer: a cached build step used by Docker

### 2. Requirements

#### 2.1 Runtime Requirements
- Docker Engine with support for building images
- access to the repository content and network for package installation

#### 2.2 Dependencies
The image installs the packages declared in [requirements.txt](requirements.txt).

#### 2.3 Compatibility
The container uses Python 3.13-slim-bookworm and is designed to run the GPX processing script with the same interface expected by the wrapper.

### 3. Installation

#### 3.1 Standard Installation
```bash
docker build -t trailone/gpx-wpt-tag-revgeo .
```

#### 3.2 Development Installation
Use a rebuild with no cache while testing dependency changes:
```bash
docker build --no-cache -t trailone/gpx-wpt-tag-revgeo .
```

#### 3.3 Verification
```bash
docker run --rm trailone/gpx-wpt-tag-revgeo --help
```

### 4. Configuration
The image uses environment variables to keep Python output unbuffered and to disable bytecode writes. The container also exposes /data as a writable volume.

### 5. User Guide

#### 5.1 Basic Usage
Run the image with the same positional arguments expected by the Python script:
```bash
docker run --rm -v "$PWD:/data" trailone/gpx-wpt-tag-revgeo /data/track.gpx HIK 1
```

#### 5.2 CLI/API Reference
The container entrypoint runs:
```bash
python /app/gpx_wpt_tag_revgeo.py
```

#### 5.3 Examples
```bash
docker run --rm -v "$PWD:/data" trailone/gpx-wpt-tag-revgeo /data/track.gpx HIK 2
```

#### 5.4 Input Specification
The input file should be mounted into the container filesystem and available to the script at the provided path.

#### 5.5 Output Specification
The script writes the output beside the input file in the mounted workspace.

### 6. Technical Design

#### 6.1 Architecture
The Dockerfile is intentionally simple: it creates a runtime image around the Python application and standard library dependencies.

#### 6.2 Components
- Python runtime image
- requirements installation layer
- application copy layer
- non-root runtime user

#### 6.3 Data Flow
1. Docker builds the image layers.
2. The application and dependencies are copied into /app.
3. The container executes the Python script with the supplied file path arguments.

#### 6.4 Processing Workflow
The image does not add processing logic beyond launching the Python entry point.

#### 6.5 Algorithms
No algorithmic logic is defined here; the container simply provides the runtime environment.

### 7. Developer Reference

#### 7.1 Modules
No Python modules are defined in this file beyond the application image layout.

#### 7.2 Classes
None.

#### 7.3 Functions
None; the image relies on the Python entry point.

#### 7.4 Data Structures
None.

#### 7.5 Public Interfaces
The primary interface is the container command line.

### 8. Reliability

#### 8.1 Validation
The image build validates that the application file and requirements are available before runtime.

#### 8.2 Error Handling
Container errors are reported through the standard Docker and Python process exit handling.

#### 8.3 Exit Codes
The image inherits the exit code from the Python script or the wrapper.

#### 8.4 Logging
Container logs come from the Python process output.

#### 8.5 Edge Cases
- missing input file or permissions
- incompatible Python dependencies
- unreadable mounted directories

### 9. Testing
A simple smoke test is:
```bash
docker run --rm trailone/gpx-wpt-tag-revgeo --help
```

### 10. Security
The container runs as an unprivileged user to reduce the impact of a compromise. It also drops the default writable state to /data and uses a dedicated UID/GID.

### 11. Performance
The slim base image keeps startup overhead low while still providing the needed networking and Python support.

### 12. Maintenance
- update the base image deliberately and test the CLI after changes
- keep dependency versions consistent with [requirements.txt](requirements.txt)
- preserve the non-root runtime user configuration

### 13. Troubleshooting
- if the image cannot build, verify Docker has access to the repository and network
- if the container cannot write output, ensure the mounted host directory is writable

### 14. Glossary
- container image: a packaged runtime environment
- entrypoint: startup command for the container

### 15. References
- Docker documentation
- Python official images

---

## Source 3: run_gpx_wpt_tag_revgeo.sh

### 1. Introduction

#### 1.1 Purpose
The shell wrapper provides a safer and more ergonomic entry point for running the GPX processing workflow inside Docker. It validates arguments, creates a staging area, builds or reuses an image, and installs the output into the requested destination without overwriting the input file.

#### 1.2 Scope
It handles:
- CLI parsing
- argument validation
- Docker image build and reuse
- container execution
- staging and output installation
- exit-code translation and error reporting

#### 1.3 Intended Audience
- operators who prefer a shell wrapper over direct Python invocation
- maintainers running the tool in Docker-based environments
- users who want safe output handling and overwrite controls

#### 1.4 Terminology
- staging directory: a temporary working directory used to validate output before installation
- wrapper: a shell script around the Python entry point
- overwrite: a flag that allows an existing host output file to be replaced

### 2. Requirements

#### 2.1 Runtime Requirements
- Bash
- Docker Engine available on PATH
- the repository files present locally

#### 2.2 Dependencies
The wrapper depends on the Docker image built from the repository and the application files in the same directory.

#### 2.3 Compatibility
The wrapper is intended for Linux-like shells and uses Bash-specific constructs. It expects a regular GPX input file and a writable output directory.

### 3. Installation

#### 3.1 Standard Installation
No separate install step is required. Mark the script executable:
```bash
chmod +x run_gpx_wpt_tag_revgeo.sh
```

#### 3.2 Development Installation
For development, use it with the rebuild flag:
```bash
./run_gpx_wpt_tag_revgeo.sh --file data/track.gpx --prefix HIK --step 1 --rebuild
```

#### 3.3 Verification
```bash
./run_gpx_wpt_tag_revgeo.sh --help
```

### 4. Configuration
The wrapper supports:
- IMAGE_NAME for the Docker image tag
- DOCKER_BIN for an alternative Docker executable
- CONTAINER_NAME to override the temporary container name
- NO_COLOR to disable ANSI color output

### 5. User Guide

#### 5.1 Basic Usage
```bash
./run_gpx_wpt_tag_revgeo.sh --file data/track.gpx --prefix HIK --step 1
```

#### 5.2 CLI/API Reference
```bash
./run_gpx_wpt_tag_revgeo.sh --file FILE --prefix PREFIX --step KM [options]
```
Supported options:
- --distance-method auto|geodesic|haversine
- --output FILE: manage a host output path
- --overwrite: permit replacing an existing output file
- --rebuild: force an uncached image rebuild
- -h, --help: show usage

#### 5.3 Examples
```bash
./run_gpx_wpt_tag_revgeo.sh --file data/track.gpx --prefix HIK --step 2 --output ./out/track_wpt.gpx
./run_gpx_wpt_tag_revgeo.sh --file data/track.gpx --prefix HIK --step 1 --distance-method haversine --overwrite
```

#### 5.4 Input Specification
The wrapper requires an existing, readable GPX file and a valid prefix and step value.

#### 5.5 Output Specification
The wrapper writes the generated output to the host filesystem after validating that the staged output is a non-empty GPX 1.1 file.

### 6. Technical Design

#### 6.1 Architecture
The wrapper implements a small control loop around Docker:
1. validate local inputs
2. build or reuse the image
3. stage the input in a temporary directory
4. run the container
5. install the validated output into the target path

#### 6.2 Components
- argument parser and validation functions
- Docker image build logic
- preflight runtime checks inside the container
- staging and installation steps

#### 6.3 Data Flow
1. The host path is validated and resolved.
2. The input is mounted read-only into the container.
3. The container runs the Python script and writes its output to a staging directory.
4. The wrapper installs the validated file into the requested output location.

#### 6.4 Processing Workflow
The wrapper performs preflight checks before the main processing run so invalid paths, unsupported step values, and write issues are caught early.

#### 6.5 Algorithms
The wrapper uses simple shell-path and file validation logic rather than numeric algorithms; the actual processing is delegated to the Python application.

### 7. Developer Reference

#### 7.1 Modules
The wrapper is a standalone shell script; there are no Python modules to maintain here.

#### 7.2 Classes
None.

#### 7.3 Functions
Key shell functions include:
- usage(): prints usage information
- validate_wrapper_arguments(): checks CLI parameters
- validate_build_environment(): validates Docker availability and required files
- resolve_input_path() / resolve_output_directory(): normalize host paths
- runtime_preflight(): checks container mount and write support
- run_container(): executes the processing container
- install_output(): commits the validated staged output

#### 7.4 Data Structures
The script uses Bash variables and arrays to track file paths, image settings, and container state.

#### 7.5 Public Interfaces
The public interface is the CLI exposed by the wrapper.

### 8. Reliability

#### 8.1 Validation
The wrapper checks:
- required arguments
- file readability and extension
- output path safety and overwrite policy
- Docker presence and daemon access
- GPX 1.1 contract of the staged output before installation

#### 8.2 Error Handling
Errors are emitted to stderr and map to non-zero exit codes.

#### 8.3 Exit Codes
The wrapper uses explicit exit codes for validation and runtime problems, including:
- 2: CLI usage/argument errors
- 3: invalid trail prefix
- 4: invalid step size
- 5: invalid distance method
- 6: Docker/build environment failure
- 7: path, output, or install failure
- 127: Docker executable not found
- 70: internal wrapper error

#### 8.4 Logging
The wrapper prints status messages with colorized progress output when terminal support is available.

#### 8.5 Edge Cases
- broken symbolic links
- output paths that alias the input file
- output file already exists without overwrite
- container runtime lacking hard-link support or write access

### 9. Testing
Recommended smoke tests:
```bash
./run_gpx_wpt_tag_revgeo.sh --help
./run_gpx_wpt_tag_revgeo.sh --file data/track.gpx --prefix HIK --step 1 --rebuild
```

### 10. Security
The wrapper uses a read-only container mount for the input file, drops all Linux capabilities, disables new privileges, and uses a temporary filesystem for /tmp. These settings reduce the attack surface during processing.

### 11. Performance
The wrapper keeps the runtime lean by staging output locally, using a temporary staging directory, and keeping the processing logic inside the container.

### 12. Maintenance
- keep the wrapper aligned with the Python CLI contract
- update the documented exit codes if the script changes
- preserve path-safety validation when modifying output handling

### 13. Troubleshooting
- if Docker is unavailable, install Docker and confirm the daemon is reachable
- if the wrapper rejects the output path, choose a different destination or use --overwrite with care
- if the installed output is missing, inspect the container logs and the staging directory cleanup path

### 14. Glossary
- staging directory: temporary holding area for the generated GPX before it is committed to the final path
- read-only mount: a container mount that cannot be modified by the process

### 15. References
- Bash scripting best practices
- Docker run reference
- Linux capability and seccomp concepts
