# Bug 4: the stats `days` limit is far too high

**Plant it in:** `playground\bug4\shortener\app.py`, method `_stats`.

**Find:**

```python
if not 1 <= days <= 90:
```

**Change `90` to `9000`** so it reads:

```python
if not 1 <= days <= 9000:
```

**What you broke:** `GET /api/v1/links/{code}/stats?days=N` must reject anything outside 1 to 90 with a 400. Now `days=365` (or 5000) is accepted, which lets a caller
request a huge time window.

**Prove it is red** (from `playground\bug4`): the test run must now include a failure in `test_invalid_days_return_json_400`.

**Run the agents:**

```powershell
python run.py brownfield --repo playground\bug4 --requirement-file input\brownfield\bugs\bug4_stats_days_limit\requirement.txt --out output\my_bug4 --verify
```

**A good result:** `release\changes.diff` puts `90` back, with a regression test for `days=91` (or higher) returning 400.
