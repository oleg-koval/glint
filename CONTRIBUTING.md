# Contributing to glint

Thanks for taking a look. glint is deliberately small — one Python file, no
dependencies — and the goal is to keep it that way.

## Ground rules

- **Stay single-file and dependency-free.** Standard library only. If a feature
  needs a third-party package, it probably doesn't belong here.
- **Every segment degrades gracefully.** A missing field hides its segment; it
  never errors. New segments must follow the same pattern.
- **Never break the status bar.** The top-level `try/except` fallback is
  load-bearing. Keep it.
- **Fast.** The script runs on every render. Keep it well under ~100 ms; bound
  any subprocess with a timeout.

## Developing

```bash
# render against a sample payload
echo '{"model":{"display_name":"Claude Sonnet 4.6"},"cwd":"'"$PWD"'","cost":{"total_cost_usd":0.42}}' | python3 glint.py

# run the smoke test
python3 test_glint.py
```

## Pull requests

1. Keep the diff focused — one change per PR.
2. Run `python3 test_glint.py` and make sure it passes.
3. Describe what the segment shows and when it hides.

## Reporting bugs

Open an [issue](https://github.com/oleg-koval/glint/issues) with the payload
(redact paths/cost if you like) and what you expected vs. saw.
