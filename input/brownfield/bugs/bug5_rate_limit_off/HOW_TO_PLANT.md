# Bug 5: the creation rate limit is never enforced (hardest to describe, most tests go red)

**Plant it in:** `playground\bug5\shortener\ratelimit.py`, the token-bucket `allow` method.

**Find:**

```python
allowed = tokens >= 1.0
```

**Change `1.0` to `0.0`** so it reads:

```python
allowed = tokens >= 0.0
```

**What you broke:** the limiter lets a request through only if the client has at least one token. With `0.0` the check is always true, so no client is ever
throttled and the service never answers 429 to a flood of link creations.

**Prove it is red** (from `playground\bug5`): several tests fail, including `test_ac8_rate_limit_two_requests_then_429` and
`test_x_forwarded_for_cannot_bypass_rate_limit`.

**Run the agents:**

```powershell
python run.py brownfield --repo playground\bug5 --requirement-file input\brownfield\bugs\bug5_rate_limit_off\requirement.txt --out output\my_bug5 --verify
```

**A good result:** `release\changes.diff` restores the `>= 1.0` comparison. Because many tests already fail, this is a good case to read in `RUN_STORY.md`: the agents
get a richer red baseline, but the validator still demands **new** tests that fail on the buggy copy.
