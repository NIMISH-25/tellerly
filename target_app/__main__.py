"""Run the mock target directly: ``python -m target_app [--port 8000]``."""
import argparse

from target_app.app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Tellerly Teller Console (mock target)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--interstitial-every",
        type=int,
        default=3,
        help="Show the maintenance interstitial every Nth member-record load (0 disables).",
    )
    parser.add_argument(
        "--session-ttl", type=int, default=180, help="Idle seconds before session expiry."
    )
    parser.add_argument(
        "--tenant",
        choices=["ridgeline", "bluepeak"],
        default="ridgeline",
        help="Tenant skin to run (bluepeak adds the verify screen).",
    )
    args = parser.parse_args()
    app = create_app(
        {
            "INTERSTITIAL_EVERY": args.interstitial_every,
            "SESSION_TTL_S": args.session_ttl,
            "TENANT": args.tenant,
        }
    )
    app.run(host=args.host, port=args.port, use_reloader=False)


if __name__ == "__main__":
    main()
