#!/usr/bin/env python3
"""Export the public Sky NZ Sports TV guide to CSV.

The Sky TV Guide React application reads its schedule from the public
GraphQL endpoint used below. Calling that endpoint directly avoids browser
rendering failures on GitHub-hosted runners and removes the Playwright/
Chromium dependency.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GRAPHQL_URL = "https://api.skyone.co.nz/exp/graph"
TV_GUIDE_URL = "https://tvguide.sky.co.nz/"
SKY_TIMEZONE = ZoneInfo("Pacific/Auckland")
COUNTRY_CODE = "NZ"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_RETRIES = 3

CHANNEL_GROUPS_QUERY = """
query getChannelGroups {
  experience(appId: TV_GUIDE_WEB) {
    channelGroups {
      id
      title
    }
    appId
  }
}
"""

CHANNEL_GROUP_QUERY = """
query getChannelGroup($id: ID!, $date: LocalDate) {
  experience(appId: TV_GUIDE_WEB) {
    channelGroup(id: $id) {
      id
      title
      channels {
        ... on LinearChannel {
          id
          title
          number
          tileImage {
            uri
          }
          slotsForDay(date: $date) {
            slots {
              id
              startMs
              endMs
              live
              programme {
                ... on Episode {
                  id
                  title
                  show {
                    id
                    title
                    type
                  }
                }
                ... on Movie {
                  id
                  title
                }
                ... on PayPerViewEventProgram {
                  id
                  title
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


class SkyGuideError(RuntimeError):
    """Raised when the Sky guide API returns unusable data."""


def _post_graphql(
    query: str,
    variables: dict[str, Any] | None = None,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    debug: bool = False,
) -> dict[str, Any]:
    payload = json.dumps(
        {"query": query, "variables": variables or {}},
        separators=(",", ":"),
    ).encode("utf-8")
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        request = Request(
            GRAPHQL_URL,
            data=payload,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": TV_GUIDE_URL.rstrip("/"),
                "Referer": TV_GUIDE_URL,
                "User-Agent": "jerco-sky-tvguide/2.0 (+public schedule exporter)",
            },
        )

        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                status = response.status
                content_type = response.headers.get("Content-Type", "")
                body = response.read(MAX_RESPONSE_BYTES + 1)

            if len(body) > MAX_RESPONSE_BYTES:
                raise SkyGuideError(
                    f"GraphQL response exceeds {MAX_RESPONSE_BYTES} bytes"
                )
            if "json" not in content_type.lower():
                raise SkyGuideError(
                    f"Unexpected GraphQL content type: {content_type!r}"
                )

            document = json.loads(body.decode("utf-8"))
            if not isinstance(document, dict):
                raise SkyGuideError("GraphQL response root is not an object")
            if document.get("errors"):
                raise SkyGuideError(
                    "GraphQL returned errors: "
                    + json.dumps(document["errors"], ensure_ascii=False)[:2000]
                )

            data = document.get("data")
            if not isinstance(data, dict):
                raise SkyGuideError("GraphQL response has no data object")

            if debug:
                print(
                    f"[debug] GraphQL request succeeded: HTTP {status}, "
                    f"{len(body)} bytes",
                    file=sys.stderr,
                )
            return data

        except HTTPError as error:
            snippet = error.read(2000).decode("utf-8", "replace")
            last_error = SkyGuideError(
                f"GraphQL HTTP {error.code}: {snippet or error.reason}"
            )
            retryable = error.code == 429 or 500 <= error.code < 600
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            last_error = error
            retryable = True
        except SkyGuideError as error:
            last_error = error
            retryable = False

        if not retryable or attempt == retries:
            break

        delay = 2 ** (attempt - 1)
        print(
            f"[warning] GraphQL attempt {attempt}/{retries} failed: "
            f"{last_error}. Retry in {delay}s.",
            file=sys.stderr,
        )
        time.sleep(delay)

    raise SkyGuideError(
        f"GraphQL request failed after {retries} attempt(s): {last_error}"
    ) from last_error


def _find_sports_group_id(*, debug: bool = False) -> str:
    data = _post_graphql(CHANNEL_GROUPS_QUERY, debug=debug)
    experience = data.get("experience")
    if not isinstance(experience, dict):
        raise SkyGuideError("Missing experience object in channel-groups response")

    groups = experience.get("channelGroups")
    if not isinstance(groups, list):
        raise SkyGuideError("Missing channelGroups list in GraphQL response")

    for group in groups:
        if (
            isinstance(group, dict)
            and str(group.get("title", "")).strip().casefold() == "sports"
            and group.get("id")
        ):
            if debug:
                print(
                    f"[debug] Sports group: {group['id']}",
                    file=sys.stderr,
                )
            return str(group["id"])

    available = [
        str(group.get("title"))
        for group in groups
        if isinstance(group, dict) and group.get("title")
    ]
    raise SkyGuideError(
        f"Sports channel group not found. Available groups: {available}"
    )


def _format_sky_time(epoch_ms: Any) -> str:
    if not isinstance(epoch_ms, (int, float)):
        raise SkyGuideError(f"Invalid programme timestamp: {epoch_ms!r}")
    value = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    value = value.astimezone(SKY_TIMEZONE)
    return value.strftime("%I:%M%p").lstrip("0")


def _scrape_day(
    sports_group_id: str,
    target_date: date,
    *,
    debug: bool = False,
) -> list[dict[str, str]]:
    data = _post_graphql(
        CHANNEL_GROUP_QUERY,
        {"id": sports_group_id, "date": target_date.isoformat()},
        debug=debug,
    )
    experience = data.get("experience")
    group = experience.get("channelGroup") if isinstance(experience, dict) else None
    if not isinstance(group, dict):
        raise SkyGuideError(
            f"Missing Sports channelGroup for {target_date.isoformat()}"
        )

    channels = group.get("channels")
    if not isinstance(channels, list):
        raise SkyGuideError(
            f"Missing channels list for {target_date.isoformat()}"
        )

    scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    rows: list[dict[str, str]] = []

    for channel in channels:
        if not isinstance(channel, dict):
            continue

        channel_name = str(channel.get("title") or "").strip()
        channel_display_name = f"[{COUNTRY_CODE}] {channel_name}"

        slots_for_day = channel.get("slotsForDay")
        slots = (
            slots_for_day.get("slots")
            if isinstance(slots_for_day, dict)
            else None
        )
        if not isinstance(slots, list):
            slots = []

        parsed_for_channel = 0
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            programme = slot.get("programme")
            if not isinstance(programme, dict):
                continue
            title = str(programme.get("title") or "").strip()
            if not title:
                continue

            try:
                start_time = _format_sky_time(slot.get("startMs"))
                end_time = _format_sky_time(slot.get("endMs"))
            except SkyGuideError as error:
                print(
                    f"[warning] Skip slot {slot.get('id')!r}: {error}",
                    file=sys.stderr,
                )
                continue

            rows.append(
                {
                    "country_code": COUNTRY_CODE,
                    "channel_id": str(channel.get("id") or ""),
                    "channel_number": str(channel.get("number") or ""),
                    "channel_name": channel_name,
                    "channel_display_name": channel_display_name,
                    "date": target_date.isoformat(),
                    "program_title": title,
                    "start_time": start_time,
                    "end_time": end_time,
                    "scraped_at": scraped_at,
                }
            )
            parsed_for_channel += 1

        if debug:
            print(
                f"[debug] {target_date.isoformat()} channel "
                f"{channel.get('number')} {channel_display_name}: "
                f"{parsed_for_channel} programmes",
                file=sys.stderr,
            )

    if not rows:
        raise SkyGuideError(
            f"GraphQL returned zero usable Sports programmes for "
            f"{target_date.isoformat()}"
        )

    print(
        f"[info] {target_date.isoformat()}: {len(channels)} channels, "
        f"{len(rows)} programmes",
        file=sys.stderr,
    )
    return rows


def _write_csv(output_path: str, rows: list[dict[str, str]]) -> None:
    os.makedirs(os.path.dirname(output_path) or SCRIPT_DIR, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(
            csv_file,
            fieldnames=[
                "country_code",
                "channel_id",
                "channel_number",
                "channel_name",
                "channel_display_name",
                "date",
                "program_title",
                "start_time",
                "end_time",
                "scraped_at",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


def _git_commit_and_push(csv_filename: str) -> None:
    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            cwd=SCRIPT_DIR,
            capture_output=True,
            text=True,
            check=False,
        )

    add_result = run(["git", "add", csv_filename])
    if add_result.returncode != 0:
        raise SkyGuideError(f"git add failed: {add_result.stderr.strip()}")

    commit_message = (
        f"chore: sports schedule sync "
        f"{datetime.now(SKY_TIMEZONE).date().isoformat()}"
    )
    commit_result = run(["git", "commit", "-m", commit_message])
    if commit_result.returncode != 0:
        combined = (commit_result.stdout + commit_result.stderr).strip()
        if "nothing to commit" in combined.lower():
            print("[info] No CSV changes to commit.", file=sys.stderr)
            return
        raise SkyGuideError(f"git commit failed: {combined}")

    push_result = run(["git", "push"])
    if push_result.returncode != 0:
        raise SkyGuideError(f"git push failed: {push_result.stderr.strip()}")
    print(f"[info] Committed and pushed: {commit_message}", file=sys.stderr)


def _save_failure_artifact(error: BaseException) -> None:
    diagnostic_dir = os.path.join(SCRIPT_DIR, "debug_artifacts")
    os.makedirs(diagnostic_dir, exist_ok=True)
    diagnostic = {
        "timestamp": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "endpoint": GRAPHQL_URL,
        "error_type": type(error).__name__,
        "error": str(error),
    }
    path = os.path.join(diagnostic_dir, "failure.json")
    with open(path, "w", encoding="utf-8") as output:
        json.dump(diagnostic, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(f"[error] Diagnostic saved to {path}", file=sys.stderr)


def scrape(
    output_path: str,
    *,
    debug: bool = False,
    git_push: bool = False,
    days: int = 1,
    start_date: date | None = None,
) -> list[dict[str, str]]:
    start_date = start_date or datetime.now(SKY_TIMEZONE).date()
    sports_group_id = _find_sports_group_id(debug=debug)
    rows: list[dict[str, str]] = []

    for offset in range(days):
        rows.extend(
            _scrape_day(
                sports_group_id,
                start_date + timedelta(days=offset),
                debug=debug,
            )
        )

    _write_csv(output_path, rows)
    print(f"Done. {len(rows)} programmes saved to {output_path}")

    if git_push:
        _git_commit_and_push(os.path.relpath(output_path, SCRIPT_DIR))
    return rows


def _parse_date(value: str, option_name: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as error:
        raise SystemExit(
            f"ERROR: {option_name} must use YYYY-MM-DD format: {value!r}"
        ) from error


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export Sky NZ Sports TV Guide data to CSV"
    )
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--until")
    parser.add_argument("--start-offset", type=int, default=0)
    parser.add_argument("--start-date")
    parser.add_argument("--output")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--git-push", action="store_true")
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Deprecated compatibility option; no browser is used",
    )
    args = parser.parse_args()

    if args.headful:
        print(
            "[warning] --headful is deprecated; GraphQL mode uses no browser.",
            file=sys.stderr,
        )

    today = datetime.now(SKY_TIMEZONE).date()
    start_date = (
        _parse_date(args.start_date, "--start-date")
        if args.start_date
        else today + timedelta(days=args.start_offset)
    )

    if args.until:
        until_date = _parse_date(args.until, "--until")
        days = (until_date - start_date).days + 1
    else:
        days = args.days

    if days < 1:
        parser.error("requested date range must contain at least one day")
    if days > 28:
        parser.error("Sky publishes at most 28 days; --days must be <= 28")

    end_date = start_date + timedelta(days=days - 1)
    default_name = (
        f"sports_{start_date.isoformat()}.csv"
        if days == 1
        else f"sports_{start_date.isoformat()}_to_{end_date.isoformat()}.csv"
    )
    output_name = args.output or default_name
    output_path = (
        output_name
        if os.path.isabs(output_name)
        else os.path.join(SCRIPT_DIR, output_name)
    )

    try:
        scrape(
            output_path,
            debug=args.debug,
            git_push=args.git_push,
            days=days,
            start_date=start_date,
        )
    except Exception as error:
        _save_failure_artifact(error)
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())