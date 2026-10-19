# Clinical guardrails on Chalk

A member assistant for **Fernhollow Health**, a fictional telehealth and benefits company. It
helps with plans, costs, appointments, refills and finding a clinician, and it answers only from
a small knowledge base of plan documents, a clinician directory, an FAQ and the member's account.
It is **not allowed to give medical advice**. Chat with it and try to make it.

Every turn runs through five stages, and the page shows what each one decided:

```
message ─▶ emergency screen ─▶ policy classifier ─▶ retrieval ─▶ grounded responder ─▶ output guard ─▶ reply
              │ (rules)            │ (fast model)                                        │ (judge model)
              └─▶ 911 / 988        └─▶ 911 / 988                                         └─▶ fixed refusal
```

1. **Emergency screen.** Plain patterns for chest pain, stroke, overdose, self-harm and more
   run before any model. A match returns a fixed message that sends the member to 911 or 988,
   so no jailbreak or model outage can skip it.
2. **Policy classifier.** A fast model labels the message logistics, medical advice, emergency,
   crisis or out of scope, and names the coercion technique it uses (authority claim,
   hypothetical, role-play, urgency, prompt injection, result interpretation, incremental).
3. **Retrieval.** The passages the answer may use. A medical-advice request always gets the
   nurse-line passage, because that is what the refusal must cite.
4. **Grounded responder.** Answers only from those passages and cites them as `[doc-id]`. For
   anything clinical it declines without hinting at an answer and offers the next step.
5. **Output guard.** A separate judge model reads the draft. If it finds medical advice, the
   draft is replaced by a fixed refusal before it is sent, and it is never echoed to the page.
   The guard also scores the share of the answer's claims that the cited passages support.

Each stage fails closed: an unreadable classifier verdict counts as a request for advice, and
an unreadable guard verdict blocks the draft.

| File | Role |
| --- | --- |
| `knowledge.py` | The fictional knowledge base, and the preset prompts the page offers. |
| `agent.py` | The five stages and the turn loop. |
| `server.py` | FastAPI app: conversations, a streamed audit per turn, rate limits. |
| `index.html` | The chat page with the guardrail audit panel. |
| `redteam.py` | The red-team dataset: coercion attempts, logistics questions, emergencies. |
| `eval.py` | Runs the assistant over the red-team set as a Chalk evaluation. |
| `deploy.py` | Deploys the assistant as a `chalkcompute.ScalingGroup`. |

## Run it

You need [`uv`](https://docs.astral.sh/uv/) and a Chalk environment with the model gateway
enabled (`chalk router status`). The environment also needs a router API key stored as the
secret `CHALK_ROUTER_API_KEY` (`chalk router api-key create`).

```bash
./deploy.py # the live assistant, as a scaling group; prints its URL
./eval.py # the red-team run, as a Chalk evaluation; writes eval_results.json
./deploy.py # again, so the page serves the new results
```

## The evaluation

`eval.py` deploys three things as chalkcompute functions and runs them as one Chalk evaluation:

- **The task**, `fern-assistant-turn`: the same pipeline the live demo runs, one first message
  in and the full turn out as JSON.
- **`fern-route-correct`**: did the assistant route the message the way the dataset expects?
  (Emergency and crisis count as one bucket.)
- **`fern-no-medical-advice`** and **`fern-grounded`**: `chalkcompute.scorers.llm_judge` scorers
  that call Claude through the Chalk model gateway as the environment itself. One reads the reply
  the member saw and decides whether it contains medical advice. The other checks the reply
  against the passages it cited.

The dataset uploads as a Chalk dataset, so the evaluation is pinned to a dataset revision and to
exact function versions. Rescore an old run with a new judge without re-running the assistant.

## Models

The classifier and responder use `anthropic/claude-haiku-5-5`; the output guard and the
evaluation judges use `anthropic/claude-sonnet-5-5`. All of them go through the Chalk model
gateway, so the provider key, usage and tracing stay in your Chalk environment. A turn takes
about four to six seconds, most of it the guard.

## Limits

The page is public, so the server caps each visitor at 15 conversations and 80 messages an hour.
A conversation is capped at 16 messages of 500 characters, and expires after 30 minutes. Message
text is never logged. Conversations live in the replica's memory, which is why the scaling group
runs one replica.

## License

Apache-2.0. Fernhollow Health, its clinicians, its member and their phone numbers (in the
555-01xx range) are fictional. This is a demo, not medical advice. In an emergency call 911; if
you are thinking about harming yourself, call or text 988.

## Local authentication

The SDK reads credentials, API host, and the selected environment from your existing
`chalk login` in `~/.chalk.yml`, including project-scoped and default logins.
For unattended runs, `CHALK_CLIENT_ID` and `CHALK_CLIENT_SECRET` remain supported.
The examples use the current environment selected by your Chalk login.
