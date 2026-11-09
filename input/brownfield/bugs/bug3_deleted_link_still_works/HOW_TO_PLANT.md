# Bug 3: a deleted short link still works

**Plant it in:** `playground\bug3\shortener\storage.py`, method `get_link`.

**Find** the lookup query (the one that starts with `SELECT code, long_url, created_at`):

```python
"SELECT code, long_url, created_at FROM links WHERE code = ? AND deleted_at IS NULL"
```

**Delete only ` AND deleted_at IS NULL`** from that one query so it reads:

```python
"SELECT code, long_url, created_at FROM links WHERE code = ?"
```

> Careful: a similar `AND deleted_at IS NULL` also appears in `delete_link` just below. **Leave that one alone.** Only edit the `SELECT` in `get_link`.

**What you broke:** deletion is a "soft delete" (a `deleted_at` timestamp). Lookups used to ignore rows with that timestamp. Now they do not, so a deleted link
redirects again and its details are still returned.

**Prove it is red** (from `playground\bug3`): the test run must now include a failure in `test_delete_then_redirect_and_metadata_are_404`.

**Run the agents:**

```powershell
python run.py brownfield --repo playground\bug3 --requirement-file input\brownfield\bugs\bug3_deleted_link_still_works\requirement.txt --out output\my_bug3 --verify
```

**A good result:** `release\changes.diff` restores the `deleted_at IS NULL` filter in `get_link`. This bug is a good one to read in `ENGINEERING_SUMMARY.md` section 2:
the agents must find that "deleted link still redirects" lives in the storage layer, not in the HTTP code.
