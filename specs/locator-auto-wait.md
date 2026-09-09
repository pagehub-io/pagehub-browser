# Locator auto-wait and uniform ambiguity reporting

**Status:** implemented (2026-09-09). This design was reviewed as a
section of the first consumer's plan, `specs/evals-as-fixtures.md` in
`pagehub-io/app-serve-3263df92` (six independent review rounds; gate
passed at rev 6). It is copied here verbatim so this repo carries its own
record. Verification of the browser cases ran inside this repo's Docker
image, because headless Chromium on a bare WSL2 host never ticks
`requestAnimationFrame` and Playwright's actionability wait cannot
complete there.

Two behaviours of `pagehub-browser` differ from the Playwright calls the
platform service made, and both would turn passing steps into failures:

- **No auto-wait.** `PlaywrightEngineSession._resolve_single` resolves a
  locator with an immediate `count()`; zero raises `ElementNotFound` (404)
  and the action's `timeout` only governs the action itself. Platform used
  `page.click(selector, timeout=5000)`, which waits up to the timeout for
  the element to appear. 65 of the suite's 70 click/type/get-text steps are
  not immediately preceded by a `wait-for` on the same element, so any
  element that renders a few hundred milliseconds late is a 404 here and a
  pass there.
- **Ambiguity is reported two ways.** click/type/get-text raise
  `LocatorAmbiguous` (409); `wait-for` calls `Locator.wait_for`, whose
  strict-mode violation is a generic `PlaywrightError` that
  `_classify_playwright_error` turns into `RuntimeEvalError` (422, detail
  containing "strict mode violation").

Change, in `api/engine/playwright_engine.py`, keeping every documented
status code and the documented meaning of `timeout`:

1. `_resolve_single(loc, timeout, *, state="attached")` records a start
   time, awaits `pw_loc.first.wait_for(state=state, timeout=timeout)`, and
   only then counts. Errors from the wait are converted **inside
   `_resolve_single`**, with the same handler `_count` already uses for
   selector errors, because click, type, select, hover, get-text, get-html and
   get-attribute call it outside their `try` (screenshot and press-key
   inside one whose handlers only catch `PlaywrightError`, so a converted
   `EngineError` passes through either way) and `_run_action` turns any
   non-`EngineError` into a 500. `_count` itself is rewritten to this same
   unified handler (dead-target → `EngineCrash`; css/xpath →
   `InvalidLocatorSyntax`; else `_classify_playwright_error`) and the wait
   path calls it, so `find` and `wait_for_element` stop re-raising a bare
   `PlaywrightError` for non-css strategies:
   - `PlaywrightTimeoutError` with `state == "attached"` →
     `ElementNotFound(loc.repr_str())`: the same **404** and the same
     `no element matched <locator>` message the SPEC documents (row 154),
     now produced after waiting up to `timeout`. With any other state the
     element may well exist, so the arm counts first: `0` →
     `ElementNotFound`; `> 1` without `nth` → `LocatorAmbiguous(count)`;
     otherwise `ActionTimeout(f"Timed out after {timeout}ms waiting for
     {loc.repr_str()} to be {state}.")`, the **409** and message of SPEC
     row 157. A present-but-hidden element on get-text is therefore a 409,
     never a false "no element matched".
   - any other `PlaywrightError` → dead-target check first
     (`_is_dead_target_error` → `EngineCrash`, as `_classify_playwright_error`
     does; `_count` today skips this and would misreport a crashed page on a
     css locator as a syntax error), then `InvalidLocatorSyntax` for `css`
     and `xpath` strategies (a malformed selector raises a generic error from
     `wait_for`, verified: `Unexpected token "<" while parsing css
     selector`), else `_classify_playwright_error(exc, locator=loc)`. This
     keeps the SPEC's **400** row and the platform eval `bad-css-400`.
   The strict count after the wait is unchanged, so ambiguity behaves as
   before; one `nth` case changes: an `nth` beyond the match count was a
   409 action timeout and is now a 404 after `timeout`, which is what row
   154 says (verified on Playwright 1.58: `first.wait_for` returns
   in about 15 ms for a present element, waits for a late one, and leaves
   `count() == 2` for a duplicate).
2. **One deadline.** `_resolve_single` returns `(pw_loc, remaining_ms)`
   with `remaining_ms = max(1, timeout − elapsed)`, and every caller passes
   `remaining_ms` to the action, so `timeout` keeps its documented meaning
   (SPEC row 157: the budget for the element to reach state and the action
   to complete) and platform parity (`page.click(selector, timeout=5000)`
   is one 5 s budget). Without this an element that attaches late but never
   becomes visible would take up to 2 × `timeout` (verified: 7.8 s under a
   5,000 budget), the evals engine's 10 s read timeout would fire first, the
   companion would classify that transient and re-fire the click.
3. click and type wait for `attached` (they then wait for visible and
   enabled themselves, as Playwright actions do). get-text waits for
   `visible`, matching the platform's `wait_for_selector(visible)` before
   `text_content()`.
4. `_classify_playwright_error` maps a message containing "strict mode
   violation" to `LocatorAmbiguous(count)`, parsing the count from
   `resolved to (\d+) elements` (always present in that message; the
   function is synchronous and holds the engine `Locator`, not the
   Playwright one, so there is no counting fallback; if the count regex
   ever fails to match, fall through to `RuntimeEvalError` rather than
   invent a count), placed before the
   `RuntimeEvalError` fall-through, so `wait-for` reports ambiguity as 409
   with the same "matched N elements; pass options.nth" body as every other
   verb (verified text on `Locator.wait_for` for both `visible` and
   `hidden`, and on `Locator.click`).

SPEC changes in the same PR: row 154 gains "after waiting up to `timeout`"
and applies to every `_resolve_single` verb, and drops `wait-for` from its
scope (`wait_for_element` is unchanged and already answers 409 on zero
match, a pre-existing mismatch the row edit corrects); row 155 no longer
lists `wait-for` as accepting multi-match (it is 409 now, 422 before). Platform
evals that keep passing: `no-match-404` (its click sends no `timeout`, so
the browser default 5,000 applies under the seed's 15,000 request timeout)
and `bad-css-400`.

Tests: unit cases in `tests/test_engine_locator_unit.py` drive
`_resolve_single`, `_locator_error` and `_classify_playwright_error` with a
stubbed Playwright locator and real Playwright error instances (strict-mode
parsing, unparseable count, dead-target precedence, every timeout arm,
the `remaining` deduction and clamp, `nth` skipping the count). Browser
cases live in `tests/test_locator_auto_wait.py` (`pytestmark = browser`,
`make test-browser`). Cases: an element rendered
after a delay is clicked; zero match is a 404 with the documented message
after `timeout`; malformed css on click is a 400 `Invalid css selector
syntax`; an element that attaches late and never becomes visible fails at
about `timeout`, not about 2 × `timeout`, with the row-157 "to be
actionable" message; an `nth` beyond the match count is a 404 after
`timeout`; `wait-for hidden`/`detached` on zero match is still a success; a duplicate `data-testid` is a 409 on click, on
`wait-for visible` and on `wait-for hidden`, with the count in the body;
`options.nth` bypasses the strict check; get-text on a present-but-hidden element is a 409 with the row-157
message after `timeout`.

This is a separate PR on that repo with its own review; it is listed in
the partner README as a minimum version.

