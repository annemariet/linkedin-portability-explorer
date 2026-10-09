"""Console entrypoint: linkedin-check-token."""

import argparse
import sys

from linkedin_api.utils.token_health import TokenHealthLevel, build_token_health_report


def _print_report(probe_api: bool) -> tuple[bool, TokenHealthLevel]:
    report = build_token_health_report(probe_api=probe_api)
    for msg in report.messages:
        if report.level == TokenHealthLevel.ERROR:
            print(f"❌ {msg}")
        elif report.level == TokenHealthLevel.WARN:
            print(f"⚠️  {msg}")
        else:
            print(f"✅ {msg}")
    return report.level != TokenHealthLevel.ERROR, report.level


def main() -> None:
    parser = argparse.ArgumentParser(description="Check LinkedIn access token health")
    parser.add_argument(
        "--probe-api",
        action="store_true",
        help="Call LinkedIn changelog API to verify the token is accepted",
    )
    parser.add_argument(
        "--warn-exit-code",
        action="store_true",
        help="Exit 2 when expiry is in the warning window (for Scalingo cron alerts)",
    )
    args = parser.parse_args()

    ok, level = _print_report(probe_api=args.probe_api)
    if not ok:
        sys.exit(1)
    if args.warn_exit_code and level == TokenHealthLevel.WARN:
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
