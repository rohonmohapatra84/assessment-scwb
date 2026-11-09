# Inputs

Every scenario has an **input** here and an **output** in `output/` with the same folder name. You can open a requirement, edit it, and run again.

```
input/<scenario>/requirement.txt   -->   python run.py <command>   -->   output/<scenario>/
```

| Scenario | Input (what you give the system) | Command | Output |
|---|---|---|---|
| Greenfield | [`greenfield/requirement.txt`](greenfield/requirement.txt): one sentence. No existing code. | `python run.py greenfield` | `output/greenfield/` |
| Brownfield, **bug fix** | [`brownfield/requirement.txt`](brownfield/requirement.txt): a plain-English **bug report**, **plus existing code**: a copy of the greenfield service with one bug planted by the script | `python run.py brownfield --verify` | `output/brownfield/` |
| Brownfield, enhancement | [`enhancement/requirement.txt`](enhancement/requirement.txt) + existing code (the greenfield service) | `python run.py enhancement` | `output/enhancement/` |
| Brownfield, refactor | [`refactor/requirement.txt`](refactor/requirement.txt) + existing code | `python run.py refactor` | `output/refactor/` |
| Brownfield, tests only | [`tests_improvement/requirement.txt`](tests_improvement/requirement.txt) + existing code | `python run.py tests` | `output/tests_improvement/` |
| Ambiguous | [`ambiguous/requirement.txt`](ambiguous/requirement.txt): a vague sentence. [`ambiguous/answers.json`](ambiguous/answers.json): the human's answers to the questions the system asks | `python run.py ambiguous`, then `python run.py ambiguous --answers input/ambiguous/answers.json` | `output/ambiguous/` |

## Two kinds of input

1. **The requirement** (a text file here). This is the request a person makes. For greenfield it is the whole input.
2. **The existing code** (brownfield only). Brownfield changes software that already exists, so its input also includes a codebase. There is no copy of it in this folder because it is
   generated: the greenfield run produces `output/greenfield/release`, and brownfield changes a **copy** of it. The bug-fix scenario first plants one bug in that copy (see
   `output/brownfield/input/existing_code_with_bug/` and the top of `output/brownfield/RUN_STORY.md`). Your original is never modified.

## Where each run's input is kept

Each run also writes `DEMO_RESULT.md`: the released service really used (a long URL in, a short URL out, the redirect followed). Each run copies the input it used into its own output folder, so an output is self-describing: `output/<scenario>/input/requirement.txt` (and `answers.json` for the ambiguous case).
For the bug fix it also writes `output/brownfield/input/existing_code_and_planted_bug.md`: which bug was planted, in which file and line, before and after.

## Use your own requirement

```powershell
python run.py greenfield --requirement-file my_requirement.txt --out output\my_greenfield
python run.py custom "Build a to-do list API with persistence" --out output\my_custom
```

`custom` also takes `--repo <path>` to change a codebase of your own.
