"""Independently verify a staged private evidence-image bundle without activating it."""

import argparse
import json

from multiuser.image_bundle import verify_staged_image_bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(verify_staged_image_bundle(args.bundle)))
    except Exception as error:
        parser.exit(1, f"Image bundle verification refused/failed: {type(error).__name__}.\n")


if __name__ == "__main__":
    main()
