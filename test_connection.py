"""Verification test script for muse2api & OpenAI compatibility."""

import os
import sys

import httpx
from openai import OpenAI

BASE_URL = os.environ.get("MUSE2API_URL", "http://127.0.0.1:18610")
API_KEY = os.environ.get("MUSE2API_API_KEY", "sk-muse2api-local-secret")
ADMIN_KEY = os.environ.get("MUSE2API_ADMIN_KEY", "sk-muse2api-admin-secret")


def test_gateway():
    print(f"1. Checking gateway health at {BASE_URL}/healthz ...")
    try:
        r = httpx.get(f"{BASE_URL}/healthz", timeout=5.0)
        print(f"   Status: {r.status_code}, Body: {r.text}")
    except Exception as e:
        print(f"   Gateway not running or connection failed: {e}")
        return False

    print("\n2. Checking registered accounts in pool ...")
    try:
        r = httpx.get(f"{BASE_URL}/admin/accounts", headers={"Authorization": f"Bearer {ADMIN_KEY}"}, timeout=5.0)
        print(f"   Status: {r.status_code}")
        res = r.json()
        accounts = res.get("data", res) if isinstance(res, dict) else res
        print(f"   Accounts configured: {len(accounts)}")
        for acc in accounts:
            print(f"   - ID: {acc.get('id')}, Label: {acc.get('label')}, Status: {acc.get('status')}")
    except Exception as e:
        print(f"   Failed to query admin accounts: {e}")

    print("\n3. Testing OpenAI Client integration ...")
    try:
        client = OpenAI(base_url=f"{BASE_URL}/v1", api_key=API_KEY)
        models = client.models.list()
        print(f"   Available models ({len(models.data)}): {[m.id for m in models.data[:5]]}...")
    except Exception as e:
        print(f"   Failed to list models via OpenAI SDK: {e}")
        return False

    return True


if __name__ == "__main__":
    success = test_gateway()
    sys.exit(0 if success else 1)
