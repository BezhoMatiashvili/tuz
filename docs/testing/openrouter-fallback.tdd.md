# Claude billing fallback verification

The bot retries an unfunded direct Anthropic request through OpenRouter using the same
Claude model, conversation, tools and inference settings. The configured assistant model
is `claude-sonnet-5-5`; its OpenRouter ID is `anthropic/claude-sonnet-5.5`. Credentials
stay in the server environment. This report records local verification; no commit or
push was made for this task.

## User journeys and RED evidence

Journeys came directly from the user's request: continue an existing conversation when
Anthropic runs out of funds, keep the same model and settings, and protect both keys.
The initial reproducer was run before implementing fallback:

```sh
.venv/bin/python -m unittest discover -s tests -p test_model_fallback.py -v
```

It ran 13 tests and failed with 1 failure and 8 errors. The real Anthropic SDK propagated
the fixture's insufficient-credit response instead of retrying it; settings also lacked
the OpenRouter key. Tests for direct success, an absent backup key, unrelated provider
errors and an unknown model already passed.

## Current GREEN evidence

After recovery, the current implementation and expanded tests were inspected and verified
again, preserving intervening workspace changes:

```sh
.venv/bin/python -m pytest tests/test_model_fallback.py tests/test_assistant.py tests/test_conversational_chat.py tests/test_ai_orchestration.py tests/test_chat_review.py tests/test_config.py tests/test_site_config.py -q --cov=automotive.model_client --cov-report=term-missing --cov-fail-under=80
```

Result: **256 passed in 7.59 seconds**. `automotive/model_client.py`: **127 statements,
0 missed, 100% statement coverage**. This is not a claim of branch coverage.

| Guarantee | Evidence | Result |
| --- | --- | --- |
| Insufficient funds retry the same model and request through OpenRouter | `tests/test_model_fallback.py` | PASS |
| An existing conversation retains tool results and thinking signatures | SDK transport tests and assistant catalogue-search integration | PASS |
| Account spend limits are distinguished from ordinary rate limits | Billing and rate-limit fixtures | PASS |
| A stream switches before opening; a started stream is not replayed by billing fallback | Stream fixtures using the real SDK | PASS |
| Direct and gateway credentials remain isolated, including environment overrides | HTTP header assertions and secret-redaction tests | PASS |
| Unknown models and nonbilling failures do not silently change providers or models | Failure fixtures | PASS |
| The fallback respects remaining time and closes both clients | Timeout and lifecycle tests | PASS |
| Rechecks back off while an account stays unfunded and recover automatically after a top-up | Funding-state and client-copy tests | PASS |
| Native warm-up and model-option differences are handled | Warm-up, SDK and assistant request tests | PASS |

Scoped Ruff and Bandit checks passed, as did compilation and the whitespace check.
During initial verification, repository-wide `ruff check .` reported 39 existing issues
in untouched database-explorer code and its tests; they were left outside this change.
The project was already extensively modified, and Git metadata is read-only in the
managed workspace, so no checkpoint commits captured unrelated work. The RED/GREEN
evidence is preserved here instead.

## Live verification and present limitation

An actual streamed request exhausted direct Anthropic credit, switched automatically to
OpenRouter and returned `reported_model=anthropic/claude-sonnet-5.5` with a tool-use
response. The server was restarted locally and its health endpoint returned HTTP 200
with `status=OK`. A public-chat check completed and exposed no internal diagnostics,
usage or memory fields.

The final public greeting check returned `FAILED`. Its server-side record identified
`engine_status=PROVIDER_BILLING`, rather than a transport or model-conversion error.
The OpenRouter credits API then reported total usage above total credited funds.
Consequently, the current full chatbot request needs OpenRouter credit replenished.
No top-up or other account purchase was performed. No additional code change or key
change is required merely to replenish that balance.

The two API keys were checked without printing their values. `.env` remains ignored by
Git with mode `0600`, and the provided key was not present in the reviewed source,
example configuration or documentation.

## Final self-evaluation

Overall: **4.6/5** across the ECC five-axis review.

| Axis | Score | Evidence or limitation |
| --- | --- | --- |
| Accuracy | 5 | Current tests, actual SDK streaming and live billing metadata support the claims. |
| Completeness | 4 | The integration is complete; the final full public request is blocked by depleted OpenRouter credit. |
| Clarity | 5 | Configuration, provider behavior and the present account limitation are stated separately. |
| Actionability | 4 | The code is active; replenishing the external balance is required for the current full request. |
| Conciseness | 5 | The user-facing handoff can state the behavior, verification and balance limitation briefly. |

No axis requires code rework. The remaining improvement is operational: after the owner
replenishes OpenRouter credit, repeat one public greeting check. The user can assess this
limitation directly from the public failure and the provider's own balance response.
