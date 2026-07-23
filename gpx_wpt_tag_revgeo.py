from __future__ import annotations
import argparse
import copy
import logging
import math
import os
import re
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional, Sequence

import gpxpy
import gpxpy.gpx
import requests
from geopy.distance import geodesic

EARTH_RADIUS_M = 6371000
AUTO_METHOD_THRESHOLD = 50000
GPX_11_NAMESPACE = "http://www.topografix.com/GPX/1/1"
TRAIL_PREFIX_PATTERN = re.compile(r"^[A-Z]{3}$")
TERRAIN_WAYPOINT_NAME_PATTERN = re.compile(
    r"^[A-Z]{3}_(?:TH|TE|LE|HE|MP)$"
)
DISTANCE_WAYPOINT_NAME_PATTERN = re.compile(
    r"^[A-Z]{3}_KM(?:00[1-9]|0[1-9][0-9]|[1-9][0-9]{2})$"
)
MAX_KILOMETRE_MARKER = 999
WAYPOINT_TIE_BREAK_ORDER = {
    "TH": 0,
    "LE": 1,
    "HE": 2,
    "MP": 3,
    "KM": 4,
    "TE": 5,
}

NOMINATIM_API_URL = os.environ.get(
    "NOMINATIM_API_URL",
    "https://nominatim.openstreetmap.org/reverse",
)
NOMINATIM_USER_AGENT = os.environ.get(
    "NOMINATIM_USER_AGENT",
    "TrailOne-GPX-Waypoint-Generator/1.0",
)
NOMINATIM_TIMEOUT_S = 10.0
NOMINATIM_MIN_INTERVAL_S = 1.0
NOMINATIM_MAX_RETRIES = 3
NOMINATIM_BACKOFF_FACTOR = 2.0

_LOGGER = logging.getLogger(__name__)
_NOMINATIM_REQUEST_LOCK = threading.Lock()
_NOMINATIM_LAST_REQUEST_AT = 0.0


def _wait_for_nominatim_rate_limit() -> None:
    """Enforce a process-local minimum interval between Nominatim requests."""
    global _NOMINATIM_LAST_REQUEST_AT

    elapsed = time.monotonic() - _NOMINATIM_LAST_REQUEST_AT
    delay = NOMINATIM_MIN_INTERVAL_S - elapsed
    if delay > 0:
        time.sleep(delay)


def _request_nominatim_display_name(latitude: float, longitude: float) -> str:
    """Return one Nominatim ``display_name`` using a rate-limited request."""
    global _NOMINATIM_LAST_REQUEST_AT

    params = {
        "format": "jsonv2",
        "lat": latitude,
        "lon": longitude,
        "addressdetails": 0,
    }
    headers = {"User-Agent": NOMINATIM_USER_AGENT}

    with _NOMINATIM_REQUEST_LOCK:
        _wait_for_nominatim_rate_limit()
        try:
            response = requests.get(
                NOMINATIM_API_URL,
                params=params,
                headers=headers,
                timeout=NOMINATIM_TIMEOUT_S,
            )
        finally:
            _NOMINATIM_LAST_REQUEST_AT = time.monotonic()

    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Nominatim response must be a JSON object.")

    display_name = data.get("display_name")

    if not isinstance(display_name, str) or not display_name.strip():
        return ""

    return display_name.strip()


@lru_cache(maxsize=1000)
def reverse_geocode(latitude: float, longitude: float) -> str:
    """
    Reverse-geocode coordinates and return Nominatim's human-readable
    ``display_name`` value.

    Reverse-geocoding failure is non-fatal: an empty string is returned so
    waypoint generation can continue without a ``<desc>`` value. Results are
    cached in-process to avoid repeating identical coordinate lookups.
    """
    latitude = _require_finite_coordinate(
        latitude, "Reverse-geocode latitude", -90.0, 90.0
    )
    longitude = _require_finite_coordinate(
        longitude, "Reverse-geocode longitude", -180.0, 180.0
    )

    for attempt in range(1, NOMINATIM_MAX_RETRIES + 1):
        try:
            display_name = _request_nominatim_display_name(latitude, longitude)
            if not display_name:
                _LOGGER.warning(
                    "Nominatim returned no display_name for coordinates (%s, %s).",
                    latitude,
                    longitude,
                )
            return display_name
        except (requests.RequestException, ValueError) as exc:
            if attempt >= NOMINATIM_MAX_RETRIES:
                _LOGGER.warning(
                    "Reverse geocoding failed for coordinates (%s, %s) "
                    "after %d attempts: %s",
                    latitude,
                    longitude,
                    NOMINATIM_MAX_RETRIES,
                    exc,
                )
                return ""

            delay = NOMINATIM_BACKOFF_FACTOR ** (attempt - 1)
            _LOGGER.warning(
                "Reverse geocoding attempt %d/%d failed for (%s, %s): %s; "
                "retrying in %.1f s.",
                attempt,
                NOMINATIM_MAX_RETRIES,
                latitude,
                longitude,
                exc,
                delay,
            )
            time.sleep(delay)

    return ""


def _require_finite_coordinate(value, label: str, minimum: float, maximum: float):
    """Return a finite coordinate or raise a descriptive ValueError."""
    if value is None:
        raise ValueError(f"{label} is missing.")

    try:
        numeric_value = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not numeric: {value}") from exc

    if not math.isfinite(numeric_value):
        raise ValueError(f"{label} must be finite: {value}")

    if not minimum <= numeric_value <= maximum:
        raise ValueError(f"{label} is outside [{minimum}, {maximum}]: {value}")

    return numeric_value


def _require_finite_elevation(value, label: str):
    """Return a finite elevation when present; preserve None as unknown."""
    if value is None:
        return None

    try:
        numeric_value = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not numeric: {value}") from exc

    if not math.isfinite(numeric_value):
        raise ValueError(f"{label} must be finite: {value}")

    return numeric_value


def _require_valid_trail_prefix(trail_prefix: str) -> str:
    """Return an exact three-letter uppercase trail prefix."""
    if not isinstance(trail_prefix, str):
        raise ValueError("Trail prefix must be a string.")

    if TRAIL_PREFIX_PATTERN.fullmatch(trail_prefix) is None:
        raise ValueError(
            "Trail prefix must contain exactly three uppercase letters "
            "(example: HIK)."
        )

    return trail_prefix


def _require_whole_kilometre_step(step_size: float) -> int:
    """Return a valid positive whole-kilometre marker interval."""
    if not isinstance(step_size, (int, float)):
        raise ValueError("Step size must be a numeric value.")

    numeric_step = float(step_size)

    if not math.isfinite(numeric_step):
        raise ValueError("Step size must be finite.")

    if numeric_step <= 0:
        raise ValueError("Step size must be greater than zero.")

    if numeric_step > 100:
        raise ValueError("Step size is unrealistically large (>100 km).")

    if not numeric_step.is_integer():
        raise ValueError("Step size must be a whole number of kilometres.")

    return int(numeric_step)


def _validate_generated_waypoint_names(waypoints, prefix: str) -> None:
    """Validate generated waypoint names and enforce uniqueness."""
    expected_prefix = f"{prefix}_"
    seen_names = set()

    for index, waypoint in enumerate(waypoints):
        name = getattr(waypoint, "name", None)

        if not isinstance(name, str) or not name.startswith(expected_prefix):
            raise ValueError(
                f"Generated waypoint {index} has an invalid name: {name!r}."
            )

        if (
            TERRAIN_WAYPOINT_NAME_PATTERN.fullmatch(name) is None
            and DISTANCE_WAYPOINT_NAME_PATTERN.fullmatch(name) is None
        ):
            raise ValueError(
                f"Generated waypoint {index} does not match the normalized "
                f"naming convention: {name!r}."
            )

        if name in seen_names:
            raise ValueError(f"Generated waypoint name is not unique: {name}.")

        seen_names.add(name)


def _collect_nonempty_segments(gpx):
    """Return non-empty track-point sequences without joining their boundaries."""
    return [
        segment.points
        for track in gpx.tracks
        for segment in track.segments
        if segment.points
    ]

def validate_inputs(gpx, gpx_file: str, trail_prefix: str, step_size: float) -> None:
    """
    Validate command-line inputs for the GPX trail waypoint generator.

    This function performs three layers of validation:

    1. CLI Argument Validation
       - Ensures trail prefix format is correct
       - Ensures step size is within acceptable bounds

    2. File Validation
       - Confirms file existence and readability
       - Ensures file is a GPX file
       - Ensures file is not empty

    3. GPX Structural Validation
       - Ensures GPX can be parsed
       - Ensures required track structure exists
       - Ensures track contains valid points

    Parameters
    ----------
    gpx : gpxpy.gpx.GPX
        Parsed GPX object whose first track segment receives the preliminary
        structural check. ``parse_gpx_file`` subsequently validates all tracks
        and segments used by the CLI.

    gpx_file : str
        Path to the GPX file.

    trail_prefix : str
        Exact three-letter uppercase prefix used for waypoint names.

    step_size : float
        Positive whole-kilometre interval between cumulative markers.

    Raises
    ------
    FileNotFoundError
        If the GPX file does not exist.

    PermissionError
        If the file cannot be read.

    ValueError
        If any input parameter or GPX structure is invalid.
    """

    # ------------------------------------------------------------------
    # 1. CLI ARGUMENT VALIDATION
    # ------------------------------------------------------------------

    # Validate trail prefix and whole-kilometre marker interval.
    _require_valid_trail_prefix(trail_prefix)
    _require_whole_kilometre_step(step_size)

    # ------------------------------------------------------------------
    # 2. FILE VALIDATION
    # ------------------------------------------------------------------

    # File existence
    if not os.path.exists(gpx_file):
        raise FileNotFoundError(f"GPX file '{gpx_file}' does not exist.")

    # Ensure it is a file
    if not os.path.isfile(gpx_file):
        raise ValueError(f"'{gpx_file}' is not a valid file.")

    # Check file extension
    if not gpx_file.lower().endswith(".gpx"):
        raise ValueError("Input file must have a '.gpx' extension.")

    # Check readability
    if not os.access(gpx_file, os.R_OK):
        raise PermissionError(f"GPX file '{gpx_file}' is not readable.")

    # Check file size
    if os.path.getsize(gpx_file) == 0:
        raise ValueError("GPX file is empty.")

    # ------------------------------------------------------------------
    # 3. GPX STRUCTURAL VALIDATION
    # -----------------------------------------------------------------
    if not gpx.tracks:
        raise ValueError("GPX file contains no <trk> elements.")

    track = gpx.tracks[0]

    if not track.segments:
        raise ValueError("GPX track contains no <trkseg> elements.")

    segment = track.segments[0]

    if not segment.points:
        raise ValueError("GPX segment contains no <trkpt> elements.")

    if len(segment.points) < 2:
        raise ValueError("GPX track must contain at least two points.")

    # ------------------------------------------------------------------
    # 4. BASIC GEOSPATIAL SANITY CHECKS
    # ------------------------------------------------------------------

    for idx, point in enumerate(segment.points):
        _require_finite_coordinate(
            point.latitude,
            f"Track point {idx} latitude",
            -90.0,
            90.0,
        )
        _require_finite_coordinate(
            point.longitude,
            f"Track point {idx} longitude",
            -180.0,
            180.0,
        )

        if point.elevation is not None:
            elevation = _require_finite_elevation(
                point.elevation,
                f"Track point {idx} elevation",
            )
            if not (-1000 <= elevation <= 9000):
                raise ValueError(
                    f"Unrealistic elevation at point {idx}: {point.elevation}"
                )

def parse_gpx_file(gpx, strict=True, max_points=200000):
    """
    Parse a GPX file and extract track points.

    Parameters
    ----------
    gpx : gpxpy.gpx.GPX
        Parsed GPX object. Track points from all non-empty tracks and segments
        are returned in document order.

    strict : bool
        If True, perform strict validation checks.

    max_points : int
        Established maximum number of accepted track points. This is a
        processing-policy limit, not a proven memory-safety boundary.

    Returns
    -------
    dict
        Dictionary containing:
            - points: list of GPXTrackPoint
            - tracks: number of tracks
            - segments: number of segments
            - total_points: number of points

    Raises
    ------
    ValueError
        If the GPX structure is invalid.
    """
    if not gpx.tracks:
        raise ValueError("GPX file contains no <trk> elements.")

    all_points = []
    track_count = len(gpx.tracks)
    segment_count = 0

    for track in gpx.tracks:
        if not track.segments:
            if strict:
                raise ValueError("Track contains no <trkseg> elements.")
            continue

        for segment in track.segments:
            segment_count += 1

            if not segment.points:
                if strict:
                    raise ValueError("Track segment contains no <trkpt> elements.")
                continue

            all_points.extend(segment.points)

    if not all_points:
        raise ValueError("No valid track points found in GPX file.")

    if len(all_points) < 2:
        raise ValueError("GPX track must contain at least two points.")

    if len(all_points) > max_points:
        raise ValueError(
            f"GPX file contains too many points ({len(all_points)}). "
            f"Maximum allowed is {max_points}."
        )

    # Optional coordinate validation
    if strict:
        for i, p in enumerate(all_points):
            _require_finite_coordinate(
                p.latitude,
                f"Track point {i} latitude",
                -90.0,
                90.0,
            )
            _require_finite_coordinate(
                p.longitude,
                f"Track point {i} longitude",
                -180.0,
                180.0,
            )

            if p.elevation is not None:
                elevation = _require_finite_elevation(
                    p.elevation,
                    f"Track point {i} elevation",
                )
                if not (-1000 <= elevation <= 9000):
                    raise ValueError(
                        f"Unrealistic elevation at point {i}: {p.elevation}"
                    )

    return {
        "points": all_points,
        "tracks": track_count,
        "segments": segment_count,
        "total_points": len(all_points),
    }

def _compute_elevation_statistics_for_segments(point_segments):
    """Compute elevation statistics without bridging track-segment boundaries."""
    total_ascent = 0.0
    total_descent = 0.0
    max_elevation = None
    min_elevation = None

    for trackpoints in point_segments:
        prev_elevation = None

        for point in trackpoints:
            ele = getattr(point, "elevation", None)

            if ele is None:
                continue

            ele = _require_finite_elevation(ele, "Track-point elevation")

            if max_elevation is None or ele > max_elevation:
                max_elevation = ele

            if min_elevation is None or ele < min_elevation:
                min_elevation = ele

            if prev_elevation is not None:
                delta = ele - prev_elevation

                if delta > 0:
                    total_ascent += delta
                elif delta < 0:
                    total_descent += abs(delta)

            prev_elevation = ele

    if max_elevation is None or min_elevation is None:
        raise ValueError(
            "Elevation statistics cannot be computed: no elevation data found."
        )

    elevation_range = max_elevation - min_elevation

    return {
        "total_ascent": round(total_ascent, 2),
        "total_descent": round(total_descent, 2),
        "max_elevation": round(max_elevation, 2),
        "min_elevation": round(min_elevation, 2),
        "elevation_range": round(elevation_range, 2),
    }


def compute_elevation_statistics(trackpoints):
    """
    Compute core elevation statistics from one track-point sequence.

    Missing elevations remain unknown and are skipped. The public routine
    retains its established single-sequence contract; the CLI uses an internal
    segment-aware path so ascent and descent are not fabricated across GPX
    track-segment boundaries.

    Parameters
    ----------
    trackpoints : Iterable
        Sequence of GPX trackpoints containing an ``elevation`` attribute.

    Returns
    -------
    dict
        Rounded total ascent, total descent, maximum elevation, minimum
        elevation, and elevation range, all in meters.

    Raises
    ------
    ValueError
        If no finite elevation data is available.
    """
    return _compute_elevation_statistics_for_segments([trackpoints])

def print_elevation_statistics(stats):
    """
    Print elevation statistics in a structured CLI format.

    Parameters
    ----------
    stats : dict
        Output dictionary returned by compute_elevation_statistics().
    """

    print("\nElevation Statistics")
    print("--------------------")
    print(f"Total Ascent:     {stats['total_ascent']:.2f} m")
    print(f"Total Descent:    {stats['total_descent']:.2f} m")
    print(f"Max Elevation:    {stats['max_elevation']:.2f} m")
    print(f"Min Elevation:    {stats['min_elevation']:.2f} m")
    print(f"Elevation Range:  {stats['elevation_range']:.2f} m")

def calculate_3d_distance(point1, point2, distance_method="auto", dataset_size=None):
    """
    Compute a point-to-point distance from horizontal distance and elevation.

    The horizontal component is either a WGS-84 ellipsoidal geodesic or a
    spherical haversine distance. When both elevations are known, the result is
    ``sqrt(horizontal_distance**2 + elevation_difference**2)``. This is a
    straight-line local approximation over the horizontal metric; it is not
    actual terrain-surface distance.

    Parameters
    ----------
    point1 : GPXTrackPoint
    point2 : GPXTrackPoint
    distance_method : str
        Horizontal distance method:
            - 'geodesic'
            - 'haversine'
            - 'auto'
    dataset_size : int, optional
        Total number of track points used to determine algorithm
        automatically when distance_method='auto'.

    Returns
    -------
    float
        Distance in meters.
    """

    # ------------------------------------------------------------------
    # Coordinate validation
    # ------------------------------------------------------------------
    lat1 = _require_finite_coordinate(
        point1.latitude, "Point1 latitude", -90.0, 90.0
    )
    lon1 = _require_finite_coordinate(
        point1.longitude, "Point1 longitude", -180.0, 180.0
    )
    lat2 = _require_finite_coordinate(
        point2.latitude, "Point2 latitude", -90.0, 90.0
    )
    lon2 = _require_finite_coordinate(
        point2.longitude, "Point2 longitude", -180.0, 180.0
    )

    ele1 = _require_finite_elevation(point1.elevation, "Point1 elevation")
    ele2 = _require_finite_elevation(point2.elevation, "Point2 elevation")

    # The fast path follows validation so invalid identical objects cannot
    # bypass the public routine's coordinate and elevation contract.
    if point1 is point2:
        return 0.0

    # ------------------------------------------------------------------
    # Determine distance method
    # ------------------------------------------------------------------

    method = distance_method

    if distance_method == "auto":
        if dataset_size and dataset_size > AUTO_METHOD_THRESHOLD:
            method = "haversine"
        else:
            method = "geodesic"

    # ------------------------------------------------------------------
    # Horizontal distance calculation
    # ------------------------------------------------------------------

    if method == "geodesic":

        horizontal_distance = geodesic((lat1, lon1), (lat2, lon2)).meters

    elif method == "haversine":

        lat1_r, lon1_r, lat2_r, lon2_r = map(
            math.radians, (lat1, lon1, lat2, lon2)
        )

        dlat = lat2_r - lat1_r
        dlon = lon2_r - lon1_r

        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
        )

        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

        horizontal_distance = EARTH_RADIUS_M * c

    else:
        raise ValueError(
            "Invalid distance method. Allowed values: "
            "'auto', 'geodesic', 'haversine'."
        )

    # ------------------------------------------------------------------
    # Elevation difference
    # ------------------------------------------------------------------

    if ele1 is not None and ele2 is not None:
        elevation_diff = ele2 - ele1
    else:
        elevation_diff = 0.0

    # ------------------------------------------------------------------
    # 3D distance computation
    # ------------------------------------------------------------------

    return math.sqrt(horizontal_distance**2 + elevation_diff**2)

def _generate_waypoints_from_segments(
    point_segments,
    prefix,
    step_size,
    distance_method="auto",
):
    """
    Generate normalized waypoints without constructing distances across GPX gaps.

    Waypoints are sorted by source point/interpolation order, with cumulative
    3D distance as a secondary position key. When semantic waypoints share the
    same track position, the deterministic tie order is TH, LE, HE, MP, KM,
    then TE.
    """
    segments = [segment for segment in point_segments if segment]
    total_point_count = sum(len(segment) for segment in segments)

    if total_point_count < 2:
        return []

    prefix = _require_valid_trail_prefix(prefix)
    step_kilometres = _require_whole_kilometre_step(step_size)

    for point_index, point in enumerate(
        point for segment in segments for point in segment
    ):
        _require_finite_coordinate(
            point.latitude,
            f"Track point {point_index} latitude",
            -90.0,
            90.0,
        )
        _require_finite_coordinate(
            point.longitude,
            f"Track point {point_index} longitude",
            -180.0,
            180.0,
        )
        _require_finite_elevation(
            point.elevation,
            f"Track point {point_index} elevation",
        )

    def interpolate_point(p1, p2, fraction):
        """Interpolate within one source segment while preserving unknown elevation."""
        lat = p1.latitude + fraction * (p2.latitude - p1.latitude)

        raw_longitude_delta = p2.longitude - p1.longitude
        if abs(raw_longitude_delta) > 180.0:
            longitude_delta = (raw_longitude_delta + 180.0) % 360.0 - 180.0
        else:
            longitude_delta = raw_longitude_delta

        lon = p1.longitude + fraction * longitude_delta
        if lon > 180.0:
            lon -= 360.0
        elif lon < -180.0:
            lon += 360.0

        if p1.elevation is not None and p2.elevation is not None:
            ele = p1.elevation + fraction * (p2.elevation - p1.elevation)
        else:
            ele = None

        return lat, lon, ele

    segment_distance_groups = []
    segment_position_groups = []
    segment_order_groups = []
    total_distance = 0.0
    source_order = 0

    for segment in segments:
        distances = []
        positions = [total_distance]
        orders = []

        for _ in segment:
            orders.append(source_order)
            source_order += 1

        for point_index in range(1, len(segment)):
            distance = calculate_3d_distance(
                segment[point_index - 1],
                segment[point_index],
                distance_method,
                dataset_size=total_point_count,
            )
            distances.append(distance)
            total_distance += distance
            positions.append(total_distance)

        segment_distance_groups.append(distances)
        segment_position_groups.append(positions)
        segment_order_groups.append(orders)

    highest_point = None
    highest_position = None
    highest_order = None
    lowest_point = None
    lowest_position = None
    lowest_order = None

    for segment, positions, orders in zip(
        segments,
        segment_position_groups,
        segment_order_groups,
    ):
        for point, position, point_order in zip(segment, positions, orders):
            if point.elevation is None:
                continue

            if highest_point is None or point.elevation > highest_point.elevation:
                highest_point = point
                highest_position = position
                highest_order = point_order

            if lowest_point is None or point.elevation < lowest_point.elevation:
                lowest_point = point
                lowest_position = position
                lowest_order = point_order

    positioned_waypoints = []

    def add_waypoint(source_position, distance, waypoint_type, waypoint):
        """Record a waypoint with source order, distance, and semantic tie key."""
        positioned_waypoints.append(
            (
                float(source_position),
                float(distance),
                WAYPOINT_TIE_BREAK_ORDER[waypoint_type],
                len(positioned_waypoints),
                waypoint,
            )
        )

    start = segments[0][0]
    end = segments[-1][-1]

    add_waypoint(
        segment_order_groups[0][0],
        0.0,
        "TH",
        create_waypoint(
            start.latitude,
            start.longitude,
            start.elevation,
            f"{prefix}_TH",
            "Trail Head",
            reverse_geocode(start.latitude, start.longitude),
        ),
    )

    if lowest_point is not None:
        add_waypoint(
            lowest_order,
            lowest_position,
            "LE",
            create_waypoint(
                lowest_point.latitude,
                lowest_point.longitude,
                lowest_point.elevation,
                f"{prefix}_LE",
                "Lowest Elevation Point",
                reverse_geocode(lowest_point.latitude, lowest_point.longitude),
            ),
        )

    if highest_point is not None:
        add_waypoint(
            highest_order,
            highest_position,
            "HE",
            create_waypoint(
                highest_point.latitude,
                highest_point.longitude,
                highest_point.elevation,
                f"{prefix}_HE",
                "Highest Elevation Point",
                reverse_geocode(highest_point.latitude, highest_point.longitude),
            ),
        )

    halfway_distance = total_distance / 2.0
    cumulative_distance = 0.0
    next_marker_km = step_kilometres
    halfway_added = False

    for segment, distances, orders in zip(
        segments,
        segment_distance_groups,
        segment_order_groups,
    ):
        for point_index, segment_distance in enumerate(distances, start=1):
            p1 = segment[point_index - 1]
            p2 = segment[point_index]
            p1_order = orders[point_index - 1]
            p2_order = orders[point_index]
            segment_end_distance = cumulative_distance + segment_distance

            while (
                next_marker_km <= MAX_KILOMETRE_MARKER
                and segment_end_distance >= next_marker_km * 1000.0
            ):
                next_marker_distance = next_marker_km * 1000.0
                distance_into_segment = next_marker_distance - cumulative_distance

                if segment_distance == 0:
                    break

                fraction = distance_into_segment / segment_distance
                lat, lon, ele = interpolate_point(p1, p2, fraction)

                marker_source_position = p1_order + fraction * (
                    p2_order - p1_order
                )
                add_waypoint(
                    marker_source_position,
                    next_marker_distance,
                    "KM",
                    create_waypoint(
                        lat,
                        lon,
                        ele,
                        f"{prefix}_KM{next_marker_km:03d}",
                        f"{next_marker_km} km Marker",
                        reverse_geocode(lat, lon),
                    ),
                )

                next_marker_km += step_kilometres

            if not halfway_added and segment_end_distance >= halfway_distance:
                distance_into_segment = halfway_distance - cumulative_distance

                if segment_distance > 0:
                    fraction = distance_into_segment / segment_distance
                    lat, lon, ele = interpolate_point(p1, p2, fraction)

                    midpoint_source_position = p1_order + fraction * (
                        p2_order - p1_order
                    )
                    add_waypoint(
                        midpoint_source_position,
                        halfway_distance,
                        "MP",
                        create_waypoint(
                            lat,
                            lon,
                            ele,
                            f"{prefix}_MP",
                            "Halfway Point",
                            reverse_geocode(lat, lon),
                        ),
                    )
                    halfway_added = True

            cumulative_distance = segment_end_distance

    add_waypoint(
        segment_order_groups[-1][-1],
        total_distance,
        "TE",
        create_waypoint(
            end.latitude,
            end.longitude,
            end.elevation,
            f"{prefix}_TE",
            "Trail End",
            reverse_geocode(end.latitude, end.longitude),
        ),
    )

    positioned_waypoints.sort(
        key=lambda item: (item[0], item[1], item[2], item[3])
    )
    waypoints = [item[4] for item in positioned_waypoints]
    _validate_generated_waypoint_names(waypoints, prefix)

    return waypoints


def generate_waypoints(points, prefix, step_size, distance_method="auto"):
    """
    Generate trail waypoints from one continuous point sequence.

    The public API retains the established flat-sequence interface. The CLI
    invokes the internal segment-aware implementation so distinct GPX tracks
    and track segments are accumulated without an artificial boundary edge.

    Horizontal placement uses linear latitude interpolation and shortest-wrap
    longitude interpolation. This is not geodesic interpolation. Elevation is
    interpolated only when both source endpoints contain measured elevation;
    otherwise the generated elevation remains unknown.

    Parameters
    ----------
    points : Sequence
        One continuous sequence of GPX track points.
    prefix : str
        Exact three-letter uppercase waypoint-name prefix.
    step_size : float
        Positive whole-kilometre marker interval.
    distance_method : {"auto", "geodesic", "haversine"}, optional
        Horizontal-distance method used by ``calculate_3d_distance``.

    Returns
    -------
    list
        Generated GPXWaypoint objects in source-track order.
    """
    return _generate_waypoints_from_segments(
        [points],
        prefix,
        step_size,
        distance_method=distance_method,
    )


def create_waypoint(*args):
    """
    Create a GPX waypoint while preserving the established call patterns.

    Supported call patterns:

    1. ``create_waypoint(point, name, description)``
       Backward-compatible form. The waypoint receives ``description`` and no
       comment.

    2. ``create_waypoint(lat, lon, ele, name, description)``
       Backward-compatible interpolated-coordinate form.

    3. ``create_waypoint(point, name, comment, description)``
       Enriched form assigning semantic text to ``<cmt>`` and geographic
       context to ``<desc>``.

    4. ``create_waypoint(lat, lon, ele, name, comment, description)``
       Enriched interpolated-coordinate form used by waypoint generation.

    Empty descriptions are normalized to ``None`` so failed reverse geocoding
    does not emit an empty ``<desc>`` element.
    """

    if len(args) == 3:
        point, name, description = args
        comment = None

        return gpxpy.gpx.GPXWaypoint(
            latitude=point.latitude,
            longitude=point.longitude,
            elevation=point.elevation,
            name=name,
            comment=comment,
            description=description or None,
            time=point.time,
        )

    if len(args) == 4:
        point, name, comment, description = args

        return gpxpy.gpx.GPXWaypoint(
            latitude=point.latitude,
            longitude=point.longitude,
            elevation=point.elevation,
            name=name,
            comment=comment or None,
            description=description or None,
            time=point.time,
        )

    if len(args) == 5:
        lat, lon, ele, name, description = args
        comment = None

        return gpxpy.gpx.GPXWaypoint(
            latitude=lat,
            longitude=lon,
            elevation=ele,
            name=name,
            comment=comment,
            description=description or None,
        )

    if len(args) == 6:
        lat, lon, ele, name, comment, description = args

        return gpxpy.gpx.GPXWaypoint(
            latitude=lat,
            longitude=lon,
            elevation=ele,
            name=name,
            comment=comment or None,
            description=description or None,
        )

    raise TypeError(
        "create_waypoint() expected one of: "
        "(point, name, description), "
        "(lat, lon, ele, name, description), "
        "(point, name, comment, description), or "
        "(lat, lon, ele, name, comment, description)"
    )


def save_gpx_file(
    original_gpx,
    waypoints,
    output_file: str | Path,
    *,
    creator: str = "Trail One GPX Trail Waypoint Generator",
    metadata_name: str = "Trail One - Waypoint-Enriched GPX",
    metadata_desc: str = (
        "GPX trail export generated by Trail One. "
        "Includes computed waypoint markers, preserved route/track geometry, "
        "generated metadata and bounds, and optional extensions."
    ),
    author_name: str = "Trail One",
    author_link_href: str = "https://es.wikiloc.com/wikiloc/user.do?id=18125352",
    author_link_text: str = "Trail One on Wikiloc",
    author_link_type: str = "text/html",
    copyright_author: str = "Trail One | Solo Hiking",
    copyright_year: Optional[int] = None,
    copyright_license: Optional[str] = None,
    metadata_time: Optional[datetime] = None,
    metadata_keywords: Optional[Sequence[str]] = None,
    metadata_extensions: Optional[Iterable[ET.Element]] = None,
    root_extensions: Optional[Iterable[ET.Element]] = None,
    preserve_existing_waypoints: bool = True,
    preserve_existing_routes: bool = True,
    preserve_existing_tracks: bool = True,
    preserve_existing_metadata_extensions: bool = True,
) -> Path:
    """
    Serialize a waypoint-enriched GPX 1.1 document to disk using a safe,
    atomic write strategy.

    The function reconstructs a GPX 1.1 document in the element order defined
    by the GPX 1.1 model, computes bounds, injects default metadata, preserves
    selected navigation geometry, supports metadata/root extensions retained
    by the parsed object model, validates XML well-formedness before and after
    writing, and replaces the target file atomically. Well-formedness checks
    are not a substitute for validation against the official GPX 1.1 XSD.

    Parameters
    ----------
    original_gpx : gpxpy.gpx.GPX
        Parsed GPX object representing the source file.

    waypoints : Sequence[gpxpy.gpx.GPXWaypoint]
        Generated waypoint markers to be injected into the output GPX.

    output_file : str | pathlib.Path
        Destination GPX file path.

    creator : str, optional
        GPX root `creator` attribute. GPX 1.1 requires this attribute.

    metadata_name : str, optional
        Metadata `<name>` value.

    metadata_desc : str, optional
        Metadata `<desc>` value.

    author_name : str, optional
        Metadata `<author><name>` value.

    author_link_href : str, optional
        Metadata author/profile URL and metadata-level link URL.

    author_link_text : str, optional
        Human-readable text for the author/profile link.

    author_link_type : str, optional
        MIME-like type for the profile link.

    copyright_author : str, optional
        Metadata copyright author attribute.

    copyright_year : int | None, optional
        Copyright year. Defaults to current UTC year when omitted.

    copyright_license : str | None, optional
        Optional license URI for `<copyright><license>`.

    metadata_time : datetime | None, optional
        Metadata timestamp. Defaults to current UTC timestamp when omitted.

    metadata_keywords : Sequence[str] | None, optional
        Comma-separated keyword payload for GPX metadata. If omitted,
        a project-specific Trail One keyword set is generated.

    metadata_extensions : Iterable[xml.etree.ElementTree.Element] | None, optional
        Additional XML elements to append under `<metadata><extensions>`.

    root_extensions : Iterable[xml.etree.ElementTree.Element] | None, optional
        Additional XML elements to append under root `<extensions>`.

    preserve_existing_waypoints : bool, optional
        Preserve original GPX `<wpt>` elements.

    preserve_existing_routes : bool, optional
        Preserve original GPX `<rte>` elements.

    preserve_existing_tracks : bool, optional
        Preserve original GPX `<trk>` elements.

    preserve_existing_metadata_extensions : bool, optional
        Preserve original `<metadata><extensions>` payload when present.

    Returns
    -------
    pathlib.Path
        The final resolved output path.

    Raises
    ------
    TypeError
        If inputs are of the wrong type.

    ValueError
        If the output path is invalid, XML generation fails, or the final
        document cannot be validated.

    OSError
        If the file cannot be written or atomically replaced.

    Notes
    -----
    Design goals:
    1. Full GPX 1.1 metadata population by default.
    2. Computed bounds across original geometry and generated waypoints.
    3. Preservation of selected routes/tracks/waypoints and extensions exposed
       by the parsed GPX object model.
    4. UTF-8 XML output with declaration.
    5. Atomic write via temporary file + fsync + os.replace().
    """

    # ------------------------------------------------------------------
    # Local helpers
    # ------------------------------------------------------------------

    def _require_datetime_utc(value: datetime) -> datetime:
        """Normalize datetimes to timezone-aware UTC."""
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _isoformat_z(value: datetime) -> str:
        """Return a GPX-friendly ISO 8601 UTC string."""
        return _require_datetime_utc(value).isoformat().replace("+00:00", "Z")

    def _local_name(tag: str) -> str:
        """Return the local XML tag name without namespace."""
        return tag.split("}", 1)[1] if tag.startswith("{") else tag

    def _qname(ns: str, local: str) -> str:
        """Build a namespaced XML tag."""
        return f"{{{ns}}}{local}"

    def _append_text(parent: ET.Element, ns: str, tag: str, text: Optional[str]) -> Optional[ET.Element]:
        """Append a child tag when text is not blank."""
        if text is None:
            return None
        text = str(text).strip()
        if not text:
            return None
        child = ET.SubElement(parent, _qname(ns, tag))
        child.text = text
        return child

    def _append_link(
        parent: ET.Element,
        ns: str,
        href: str,
        text: Optional[str] = None,
        link_type: Optional[str] = None,
    ) -> ET.Element:
        """Append a GPX 1.1 linkType element."""
        link_el = ET.SubElement(parent, _qname(ns, "link"), {"href": href})
        _append_text(link_el, ns, "text", text)
        _append_text(link_el, ns, "type", link_type)
        return link_el

    def _iter_point_coords_from_xml(parent: ET.Element, point_tags):
        """Yield finite (latitude, longitude) pairs under one selected element."""
        for elem in parent.iter():
            if _local_name(elem.tag) not in point_tags:
                continue

            lat = elem.attrib.get("lat")
            lon = elem.attrib.get("lon")
            if lat is None or lon is None:
                continue

            try:
                lat_value = float(lat)
                lon_value = float(lon)
            except (TypeError, ValueError):
                continue

            if not math.isfinite(lat_value) or not math.isfinite(lon_value):
                continue
            if not -90.0 <= lat_value <= 90.0:
                continue
            if not -180.0 <= lon_value <= 180.0:
                continue

            yield lat_value, lon_value

    def _iter_point_coords_from_waypoints(items) -> Iterable[tuple[float, float]]:
        """Yield valid waypoint coordinates from generated waypoint objects."""
        for point in items:
            lat = getattr(point, "latitude", None)
            lon = getattr(point, "longitude", None)
            if lat is None or lon is None:
                continue

            lat_value = float(lat)
            lon_value = float(lon)
            if not math.isfinite(lat_value) or not math.isfinite(lon_value):
                continue
            if not -90.0 <= lat_value <= 90.0:
                continue
            if not -180.0 <= lon_value <= 180.0:
                continue

            yield lat_value, lon_value

    def _compute_bounds(
        source_root: ET.Element,
        generated_waypoints,
        ns: str,
        *,
        include_waypoints: bool,
        include_routes: bool,
        include_tracks: bool,
    ) -> Optional[tuple[float, float, float, float]]:
        """Compute bounds only across content selected for final serialization."""
        coords = []

        if include_waypoints:
            for waypoint in source_root.findall(_qname(ns, "wpt")):
                coords.extend(_iter_point_coords_from_xml(waypoint, {"wpt"}))

        if include_routes:
            for route in source_root.findall(_qname(ns, "rte")):
                coords.extend(_iter_point_coords_from_xml(route, {"rtept"}))

        if include_tracks:
            for track in source_root.findall(_qname(ns, "trk")):
                coords.extend(_iter_point_coords_from_xml(track, {"trkpt"}))

        coords.extend(_iter_point_coords_from_waypoints(generated_waypoints))

        if not coords:
            return None

        lats = [lat for lat, _ in coords]
        lons = [lon for _, lon in coords]
        return min(lats), min(lons), max(lats), max(lons)

    def _deepcopy_children(parent: Optional[ET.Element]) -> list[ET.Element]:
        """Deep-copy child elements for safe XML reattachment."""
        if parent is None:
            return []
        return [copy.deepcopy(child) for child in list(parent)]

    def _serialize_waypoints_to_xml_elements(generated_waypoints, ns: str) -> list[ET.Element]:
        """
        Serialize GPXWaypoint objects through gpxpy so waypoint XML, including
        fields and extensions known to gpxpy, is preserved faithfully.
        """
        tmp_gpx = gpxpy.gpx.GPX()
        tmp_gpx.waypoints.extend(generated_waypoints)
        tmp_xml = tmp_gpx.to_xml()
        tmp_root = ET.fromstring(tmp_xml.encode("utf-8"))
        return [copy.deepcopy(elem) for elem in tmp_root.findall(_qname(ns, "wpt"))]

    def _validate_output_path(path_obj: Path) -> Path:
        """Validate and normalize destination path semantics."""
        if path_obj.exists() and path_obj.is_dir():
            raise ValueError(f"Output path points to a directory, not a file: {path_obj}")
        if path_obj.suffix.lower() != ".gpx":
            raise ValueError("Output file must use the '.gpx' extension.")
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        return path_obj.resolve()

    # ------------------------------------------------------------------
    # Input validation
    # ------------------------------------------------------------------

    if original_gpx is None:
        raise TypeError("original_gpx must be a parsed GPX object, not None.")

    if not hasattr(original_gpx, "to_xml"):
        raise TypeError("original_gpx must expose a to_xml() method compatible with gpxpy.GPX.")

    if waypoints is None:
        raise TypeError("waypoints must not be None.")

    output_path = _validate_output_path(Path(output_file))

    for idx, waypoint in enumerate(waypoints):
        if not hasattr(waypoint, "latitude") or not hasattr(waypoint, "longitude"):
            raise TypeError(f"Waypoint at index {idx} is not GPX waypoint-compatible.")
        _require_finite_coordinate(
            waypoint.latitude,
            f"Waypoint {idx} latitude",
            -90.0,
            90.0,
        )
        _require_finite_coordinate(
            waypoint.longitude,
            f"Waypoint {idx} longitude",
            -180.0,
            180.0,
        )
        _require_finite_elevation(
            getattr(waypoint, "elevation", None),
            f"Waypoint {idx} elevation",
        )

    # ------------------------------------------------------------------
    # Parse original GPX XML for schema-aware reconstruction
    # ------------------------------------------------------------------

    try:
        # Perform the established serialization first so observable gpxpy
        # side effects remain compatible with Priority 2.
        original_xml = original_gpx.to_xml()
        original_root = ET.fromstring(original_xml.encode("utf-8"))

        source_namespace = (
            original_root.tag.split("}", 1)[0][1:]
            if original_root.tag.startswith("{")
            else ""
        )
        source_version = original_root.attrib.get("version")

        if source_namespace != GPX_11_NAMESPACE or source_version != "1.1":
            preserved_state = {
                name: copy.deepcopy(getattr(original_gpx, name))
                for name in ("version", "creator", "nsmap", "schema_locations")
                if hasattr(original_gpx, name)
            }
            try:
                # Force gpxpy to regenerate a coherent 1.1 schema-location
                # declaration on the temporary normalized serialization.
                if hasattr(original_gpx, "schema_locations"):
                    original_gpx.schema_locations = []
                normalized_xml = original_gpx.to_xml(version="1.1")
            finally:
                for name, value in preserved_state.items():
                    setattr(original_gpx, name, value)

            original_root = ET.fromstring(normalized_xml.encode("utf-8"))
    except Exception as exc:
        raise ValueError(f"Unable to serialize/parse original GPX object: {exc}") from exc

    # The output contract is GPX 1.1, including normalized GPX 1.0 inputs.
    gpx_ns = GPX_11_NAMESPACE

    ET.register_namespace("", gpx_ns)

    now_utc = _require_datetime_utc(metadata_time or datetime.now(timezone.utc))
    year_utc = int(copyright_year or now_utc.year)

    default_keywords = [
        "Trail One",
        "Solo Hiking",
        "GPX",
        "GPX 1.1",
        "hiking trail",
        "waypoint generation",
        "trailhead",
        "trail end",
        "halfway point",
        "cumulative distance markers",
        "3D distance",
        "geodesic",
        "haversine",
        "elevation",
        "route analysis",
        "trail navigation",
        "outdoor navigation",
        "mountain trail",
        "volcanic hiking",
        "LCST",
        "Nicaragua",
        "TrailOne Hiking Series",
    ]
    keywords = list(metadata_keywords) if metadata_keywords else default_keywords
    keywords_text = ", ".join(dict.fromkeys(k.strip() for k in keywords if str(k).strip()))

    bounds = _compute_bounds(
        original_root,
        waypoints,
        gpx_ns,
        include_waypoints=preserve_existing_waypoints,
        include_routes=preserve_existing_routes,
        include_tracks=preserve_existing_tracks,
    )

    # ------------------------------------------------------------------
    # Rebuild final GPX document in GPX 1.1 schema order
    # ------------------------------------------------------------------

    final_root = ET.Element(
        _qname(gpx_ns, "gpx"),
        {
            "version": "1.1",
            "creator": creator,
        },
    )

    # ------------------------------
    # metadata
    # ------------------------------
    metadata_el = ET.SubElement(final_root, _qname(gpx_ns, "metadata"))

    _append_text(metadata_el, gpx_ns, "name", metadata_name)
    _append_text(metadata_el, gpx_ns, "desc", metadata_desc)

    # author: personType -> name, email, link
    author_el = ET.SubElement(metadata_el, _qname(gpx_ns, "author"))
    _append_text(author_el, gpx_ns, "name", author_name)
    _append_link(author_el, gpx_ns, author_link_href, author_link_text, author_link_type)

    # copyright: copyrightType -> author attr, optional year/license
    copyright_el = ET.SubElement(
        metadata_el,
        _qname(gpx_ns, "copyright"),
        {"author": copyright_author},
    )
    _append_text(copyright_el, gpx_ns, "year", str(year_utc))
    _append_text(copyright_el, gpx_ns, "license", copyright_license)

    # metadata-level link
    _append_link(metadata_el, gpx_ns, author_link_href, author_link_text, author_link_type)

    _append_text(metadata_el, gpx_ns, "time", _isoformat_z(now_utc))
    _append_text(metadata_el, gpx_ns, "keywords", keywords_text)

    if bounds is not None:
        min_lat, min_lon, max_lat, max_lon = bounds
        ET.SubElement(
            metadata_el,
            _qname(gpx_ns, "bounds"),
            {
                "minlat": f"{min_lat:.8f}",
                "minlon": f"{min_lon:.8f}",
                "maxlat": f"{max_lat:.8f}",
                "maxlon": f"{max_lon:.8f}",
            },
        )

    # metadata extensions: preserve existing + append caller-provided
    existing_metadata = original_root.find(_qname(gpx_ns, "metadata"))
    existing_metadata_extensions = None
    if existing_metadata is not None:
        existing_metadata_extensions = existing_metadata.find(_qname(gpx_ns, "extensions"))

    metadata_extension_children = []
    if preserve_existing_metadata_extensions:
        metadata_extension_children.extend(_deepcopy_children(existing_metadata_extensions))
    if metadata_extensions:
        metadata_extension_children.extend(copy.deepcopy(ext) for ext in metadata_extensions)

    if metadata_extension_children:
        metadata_ext_el = ET.SubElement(metadata_el, _qname(gpx_ns, "extensions"))
        for child in metadata_extension_children:
            metadata_ext_el.append(child)

    # ------------------------------
    # wpt (original first, then generated)
    # ------------------------------
    if preserve_existing_waypoints:
        for wpt in original_root.findall(_qname(gpx_ns, "wpt")):
            final_root.append(copy.deepcopy(wpt))

    for generated_wpt in _serialize_waypoints_to_xml_elements(waypoints, gpx_ns):
        final_root.append(generated_wpt)

    # ------------------------------
    # rte
    # ------------------------------
    if preserve_existing_routes:
        for rte in original_root.findall(_qname(gpx_ns, "rte")):
            final_root.append(copy.deepcopy(rte))

    # ------------------------------
    # trk
    # ------------------------------
    if preserve_existing_tracks:
        for trk in original_root.findall(_qname(gpx_ns, "trk")):
            final_root.append(copy.deepcopy(trk))

    # ------------------------------
    # root extensions
    # ------------------------------
    existing_root_extensions = original_root.find(_qname(gpx_ns, "extensions"))
    root_extension_children = _deepcopy_children(existing_root_extensions)
    if root_extensions:
        root_extension_children.extend(copy.deepcopy(ext) for ext in root_extensions)

    if root_extension_children:
        root_ext_el = ET.SubElement(final_root, _qname(gpx_ns, "extensions"))
        for child in root_extension_children:
            root_ext_el.append(child)

    # ------------------------------------------------------------------
    # Pre-write XML validation
    # ------------------------------------------------------------------

    try:
        xml_bytes = ET.tostring(final_root, encoding="utf-8", xml_declaration=True)
        ET.fromstring(xml_bytes)
    except Exception as exc:
        raise ValueError(f"Generated GPX XML is not well-formed: {exc}") from exc

    # ------------------------------------------------------------------
    # Atomic write: temp file -> fsync -> replace
    # ------------------------------------------------------------------

    temp_path: Optional[Path] = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            suffix=".gpx",
            prefix=f"{output_path.stem}.",
            dir=str(output_path.parent),
            delete=False,
        ) as tmp_file:
            temp_path = Path(tmp_file.name)
            tmp_file.write(xml_bytes)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())

        # Post-write validation from disk to catch truncation/corruption issues.
        with open(temp_path, "rb") as f:
            ET.parse(f)

        os.replace(temp_path, output_path)

    except Exception:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise

    return output_path

def main():
    parser = argparse.ArgumentParser(description="Analyze GPX hiking trails and generate waypoint markers.")
    parser.add_argument("gpx_file", type=str, help="Path to the GPX file.")
    parser.add_argument("trail_prefix", type=str, help="Three-letter uppercase prefix for waypoint markers.")
    parser.add_argument(
        "step_size",
        type=float,
        help="Positive whole-kilometre interval (1–100) for cumulative markers.",
    )
    parser.add_argument("--distance-method", choices=["auto", "geodesic", "haversine"],default="auto",help="Horizontal distance calculation method.")
    args = parser.parse_args()

    try:
        with open(args.gpx_file, "r", encoding="utf-8") as fh:
            gpx = gpxpy.parse(fh)

        validate_inputs(gpx, args.gpx_file, args.trail_prefix, args.step_size)

        result = parse_gpx_file(gpx)
        points = result["points"]
        point_segments = _collect_nonempty_segments(gpx)

        elevation_stats = _compute_elevation_statistics_for_segments(
            point_segments
        )
        print_elevation_statistics(elevation_stats)

        waypoints = _generate_waypoints_from_segments(
            point_segments,
            args.trail_prefix,
            args.step_size,
            distance_method=args.distance_method
        )
        
        output_file = f"{os.path.splitext(args.gpx_file)[0]}_{args.step_size}_wpt.gpx"
        
        original_gpx = gpx

        save_gpx_file(
            original_gpx=original_gpx,
            waypoints=waypoints,
            output_file=output_file,
        )
        
        print("\n✅ GPX Waypoint Generation Complete!")

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1) from e

if __name__ == "__main__":
    main()
