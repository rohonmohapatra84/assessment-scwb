# Bug 1: aliases shorter than 3 characters are accepted (easiest)

**Plant it in:** `playground\bug1\shortener\validation.py`, function `validate_alias`.

**Find:**

```python
re.fullmatch(r"[A-Za-z0-9_-]{3,32}", value)
```

**Change `{3,32}` to `{1,32}`** so the line reads:

```python
re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value)
```

**What you broke:** the minimum alias length. `ab` or even `a` is now a valid custom alias, when the contract says 3 to 32 characters.

**Prove it is red** (from `playground\bug1`): `python -m unittest discover -s tests -t .` must now include a failure in
`test_ac4_invalid_aliases_return_400`.

**Run the agents:**

```powershell
python run.py brownfield --repo playground\bug1 --requirement-file input\brownfield\bugs\bug1_short_alias\requirement.txt --out output\my_bug1 --verify
```

**A good result:** `release\changes.diff` shows `{1,32}` going back to `{3,32}` and nothing else of substance, plus new regression tests that
reject `a` and `ab` and that fail on your buggy copy.
