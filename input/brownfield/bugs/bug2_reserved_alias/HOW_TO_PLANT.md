# Bug 2: the reserved alias "admin" can be registered

**Plant it in:** `playground\bug2\shortener\validation.py`, function `validate_alias`.

**Find** the set of reserved names:

```python
{"api", "healthz", "openapi.json", "admin"}
```

**Remove `"admin"`** so it reads:

```python
{"api", "healthz", "openapi.json"}
```

**What you broke:** reserved words are rejected so a user cannot grab a path the service itself uses or a name that looks official. `admin` is now allowed.

**Prove it is red** (from `playground\bug2`): the test run must now include a failure in `test_reserved_aliases_return_400`.

**Run the agents:**

```powershell
python run.py brownfield --repo playground\bug2 --requirement-file input\brownfield\bugs\bug2_reserved_alias\requirement.txt --out output\my_bug2 --verify
```

**A good result:** `release\changes.diff` adds `"admin"` back to the reserved set, with a regression test that posts `custom_alias: "admin"` and expects 400.
