"""The Blob URL signing is a port of @vercel/blob; golden_blob.json holds URLs produced by the real JS SDK
(@vercel/blob 2.8.1, `presignUrl`) for fixed inputs, so any drift in the port fails here."""
import json
import os
from urllib.parse import urlencode

import pytest

from solidpilot_gateway import blobstore as bs

with open(os.path.join(os.path.dirname(__file__), "golden_blob.json"), encoding="utf-8") as _fh:
    GOLD = json.load(_fh)
NOW = 1_700_000_000_000


def _url(case):
    p = case["pathname"]
    if case["op"] == "put":
        payload = bs.presign(GOLD["deleg"], GOLD["key"], operation="put", pathname=p, valid_until_ms=4102444700000,
                             max_size=1234567, content_types=["application/octet-stream"], add_random_suffix=False,
                             allow_overwrite=False, now_ms=NOW)
        return bs.add_presigned_params(f"{bs.API_URL}/?{urlencode({'pathname': p})}", payload)
    until = 10**15 if case.get("novalid") else 4102444700000   # no per-URL expiry == the delegation's own
    payload = bs.presign(GOLD["deleg"], GOLD["key"], operation="get", pathname=p, valid_until_ms=until, now_ms=NOW)
    host = bs.store_id_of(GOLD["deleg"]).lower()
    return bs.add_presigned_params(f"https://{host}.private.blob.vercel-storage.com/{p}", payload)


@pytest.mark.parametrize("i", range(3))
def test_presign_matches_js_sdk(i):
    assert _url(GOLD["cases"][i]) == GOLD["cases"][i]["url"]


def test_store_id_strips_prefix():
    assert bs.store_id_of(GOLD["deleg"]) == "ABC123"


def test_presign_enforces_scope_and_expiry():
    with pytest.raises(bs.BlobError):
        bs.presign(GOLD["deleg"], "k", operation="delete", pathname="x", valid_until_ms=NOW + 10, now_ms=NOW)
    with pytest.raises(bs.BlobError):
        bs.presign(GOLD["deleg"], "k", operation="get", pathname="x", valid_until_ms=NOW - 1, now_ms=NOW)
    with pytest.raises(bs.BlobError):
        bs.presign(GOLD["deleg"], "k", operation="get", pathname="x", valid_until_ms=10**15, now_ms=4102444800001)


@pytest.mark.parametrize("bad", ["", "/abs", "a//b", "x" * 951])
def test_pathname_rules(bad):
    with pytest.raises(bs.BlobError):
        bs._check_pathname(bad)
