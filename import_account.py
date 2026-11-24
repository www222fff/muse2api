"""Interactive or CLI cookie import script for muse2api."""

import argparse
import sys

import httpx

ADMIN_URL = "http://127.0.0.1:18610/admin/accounts"
ADMIN_KEY = "sk-muse2api-admin-secret"


def _cookie_payload(args) -> dict:
    """Build the account payload from CLI args or interactive prompts."""
    if args.raw:
        return {"label": args.label, "cookies": args.raw.strip()}
    if args.sess and args.device:
        cookies = {
            "hatch_sess": args.sess.strip(),
            "hatch_native_auth_device": args.device.strip(),
        }
        if args.gw:
            cookies["hatch_gw"] = args.gw.strip()
        return {"label": args.label, "cookies": cookies}

    print("--- muse.ai Cookie Importer ---")
    label = input("Account label [primary]: ").strip() or "primary"
    sess = input("hatch_sess cookie: ").strip()
    device = input("hatch_native_auth_device cookie: ").strip()
    gw = input("hatch_gw cookie (optional, press Enter to skip): ").strip()

    if not sess or not device:
        print("Error: Both hatch_sess and hatch_native_auth_device are required.")
        sys.exit(1)

    cookies = {"hatch_sess": sess, "hatch_native_auth_device": device}
    if gw:
        cookies["hatch_gw"] = gw
    return {"label": label, "cookies": cookies}


def _send_registration(payload: dict) -> None:
    print(f"\nSending registration to {ADMIN_URL}...")
    headers = {"Authorization": f"Bearer {ADMIN_KEY}", "Content-Type": "application/json"}
    try:
        r = httpx.post(ADMIN_URL, headers=headers, json=payload, timeout=15.0)
        if r.status_code in (200, 201):
            print("✅ Account registered successfully!")
            print(f"Server response: {r.text}")
        else:
            print(f"❌ Registration failed ({r.status_code}): {r.text}")
    except Exception as e:
        print(f"❌ Failed to reach gateway: {e}")


def main():
    parser = argparse.ArgumentParser(description="Import muse.ai session cookies into muse2api")
    parser.add_argument("--sess", help="hatch_sess cookie value")
    parser.add_argument("--device", help="hatch_native_auth_device cookie value")
    parser.add_argument("--gw", default="", help="hatch_gw cookie value (optional)")
    parser.add_argument("--label", default="primary", help="Account label name")
    parser.add_argument("--raw", help="Raw Cookie header string (e.g., 'hatch_sess=...; hatch_native_auth_device=...')")
    args = parser.parse_args()
    _send_registration(_cookie_payload(args))


if __name__ == "__main__":
    main()
