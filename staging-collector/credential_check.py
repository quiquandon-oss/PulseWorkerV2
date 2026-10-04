#!/usr/bin/env python3
"""
READ-ONLY staging credential check (test.yml job `staging-credential-check`).

Loads the collector from the reviewed checkout given as argv[1] and runs exactly two of its steps:
  1. validate_target()            -- pure, no network: the exact staging triple, production refused
  2. StagingD1.verify_remote_identity() -- one GET of the staging database's metadata
No SQL is sent: StagingD1.run() is never called. The token is read from the environment by the collector and is never
printed (D1Error messages are already redacted by the collector).

Exit codes: 0 authenticated and confirmed staging, 2 target refused, 3 request failed (e.g. HTTP 401).
"""
import json
import sys


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    sys.path.insert(0, argv[0])
    import collector as c  # the reviewed collector, from argv[0]

    try:
        target = c.validate_target()
    except c.TargetError as e:
        print(f"RESULT: TARGET REFUSED -- {e}")
        return 2
    print(f"Validated staging target: account={target.account_id} database={target.database_name} id={target.database_id}")
    try:
        identity = c.StagingD1(target).verify_remote_identity()
    except c.TargetError as e:
        print(f"RESULT: IDENTITY MISMATCH -- {e}")
        return 2
    except (c.D1Error, c.NetworkPolicyError) as e:
        print(f"RESULT: AUTHENTICATION/REQUEST FAILED -- {e}")
        return 3
    print("Cloudflare response: " + json.dumps(identity))
    print("RESULT: AUTHENTICATED -- the staging collector token can read the staging D1 database metadata. No SQL was sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
