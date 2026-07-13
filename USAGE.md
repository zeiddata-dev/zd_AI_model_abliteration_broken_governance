# Zeid Data Open Model: Usage Guide

A private, self-hosted large language model with the refusal filter removed,
running on a rented GPU and reachable over a password-gated web chat or an
authenticated API. This guide covers how to use it, what "abliteration" actually
means, where it is useful, and how to prompt it well.

## 1. What this is

The model is `gpt-oss-20b-heretic`: an abliterated build of OpenAI's open-weight
`gpt-oss-20b`. It runs locally on your own hardware. Nothing you send it goes to
a third-party provider. Because the refusal behavior has been removed at the
weight level, it will attempt almost any request rather than declining. That is
a capability and a liability at the same time. See section 6.

## 2. How to use it

There are three ways in, from easiest to most technical.

### Web chat (easiest)

Open the chat URL in a browser, enter the access password, and type. Replies
stream in as they generate. The conversation keeps context across turns within a
session, so you can ask follow-ups that refer back to earlier messages.

Notes:
- The box must be running. If the page loads but messages fail, the GPU instance
  is stopped.
- The first message after the box starts is slow, because the model loads into
  the GPU (one to three minutes). After that it is fast for the rest of the
  session.
- The reasoning trace is shown dimmed above each answer.

### API, single prompt (for scripts)

Stateless. Each call is one self-contained turn.

    curl https://YOUR_ENDPOINT/api/generate \
      -H "Authorization: Bearer YOUR_TOKEN" \
      -d '{"model":"svjack/gpt-oss-20b-heretic","prompt":"your prompt","stream":false}'

### API, multi-turn chat (for conversations)

Send the full message history each call so the model has context.

    curl https://YOUR_ENDPOINT/api/chat \
      -H "Authorization: Bearer YOUR_TOKEN" \
      -d '{"model":"svjack/gpt-oss-20b-heretic","messages":[{"role":"user","content":"hello"}],"stream":false}'

Append each reply to the messages array before sending the next question.

## 3. What abliteration is

Standard aligned models carry a learned "refusal direction" in their internal
activations. When a prompt trips it, the model produces a refusal ("I can't help
with that") instead of an answer.

Abliteration is a technique that identifies that refusal direction in the model's
weights and mathematically removes it, so the model no longer steers toward
refusing. It is not a prompt trick or a jailbreak that you type in. It is a
permanent modification of the model file itself. There is no prompt to undo it
and no system message that re-enables the filter.

This particular build was produced with a tool called Heretic, which combines
directional ablation with automated parameter optimization (a TPE sampler via
Optuna). The optimizer co-minimizes two things at once: the number of refusals,
and how far the modified model drifts from the original (measured by KL
divergence). The goal is a model that stops refusing while behaving as close to
the original as possible on everything else, so general quality is preserved.

One quirk specific to `gpt-oss-20b-heretic`: the model tends to reason about its
own safety policy in its thinking trace before answering. It may visibly
deliberate, then comply anyway. Automated refusal benchmarks sometimes misread
that hesitation as a refusal, which is why early scores understated how
compliant it actually is.

What abliteration is NOT:
- It is not fine-tuning on new knowledge. The model knows exactly what the base
  model knew, no more.
- It does not make the model more accurate or more capable. If anything, the
  process can introduce small regressions, which the optimization step works to
  minimize.
- It does not remove hallucination, bias, or errors. Those are properties of the
  base model and remain.

## 4. Use cases

The value of an unfiltered local model is in the situations where a hosted,
filtered model refuses, hedges, or logs your prompts, and where you have a
legitimate reason to work without those constraints.

Defensive security research
- Analyzing malware behavior, attacker tradecraft, and threat-actor techniques
  in depth without the model refusing to discuss the mechanics.
- Reasoning through attack scenarios to build detections, so you can write
  better Sigma, KQL, SPL, or Elastic rules against techniques you fully
  understand.
- Red-team scenario planning and tabletop exercises where the model plays an
  adversary honestly instead of sanitizing the threat.

OSINT and investigation
- Drafting search strategies, pivot logic, and enrichment pipelines.
- Summarizing and cross-referencing large volumes of collected open-source
  material without content-policy interruptions on sensitive subject matter.
- Generating and refining extraction logic for messy data.

Writing and analysis
- Long-form drafting, editing, and rewriting in a specific voice, with no
  hosted-provider tone policing.
- Blunt, unhedged critique of a plan, argument, or design.
- Working through topics a filtered model treats as off-limits but that are
  legitimate to research.

Development
- Code generation, review, and debugging.
- Explaining unfamiliar code, protocols, or systems.

Privacy-sensitive work
- Anything where sending prompts to a third-party API is not acceptable. The
  model runs on your box; the traffic does not leave your infrastructure.

## 5. Prompt examples

The model responds well to direct, specific prompts. Vague prompts get vague
answers. State the role, the goal, the constraints, and the output format you
want.

Detection engineering

    You are a detection engineer. Given this description of a persistence
    technique, list the observable artifacts on a Windows endpoint, then write a
    Sigma rule to detect it. Explain each condition. Technique: [describe it].

Malware analysis reasoning

    Explain, at a behavioral level, how a loader that uses process hollowing
    operates: the sequence of API calls, what each step achieves, and which of
    those steps are the strongest detection opportunities.

OSINT workflow

    I am investigating an alias across platforms. Lay out a structured pivot
    plan: what identifiers to collect first, how each one can lead to others,
    and where the common dead ends are. Assume only open sources.

Adversary emulation

    Act as a threat actor planning a phishing campaign against a mid-size
    company, for a defensive tabletop. Describe the stages you would go through
    so the blue team can map detections to each stage. Focus on what defenders
    should watch for.

Blunt critique

    Here is my incident response runbook. Tear it apart. Assume it will fail
    under pressure and tell me exactly where and why, no reassurance.

Data extraction logic

    Write a deterministic extractor that parses these log lines and outputs one
    JSON record per event with a DECISION field explaining why each line was
    classified as it was. Sample lines: [paste].

Writing

    Rewrite this paragraph so it sounds like a human wrote it, not a corporate
    template. Keep it blunt and dry. No filler. [paste].

Prompting tips
- Give it a role ("You are a detection engineer").
- State the output format ("write a Sigma rule", "one JSON record per line",
  "a numbered list").
- Provide the raw material inline rather than describing it.
- For reasoning tasks, ask it to show its steps.
- If the first answer is too cautious or too long, tell it directly: "shorter",
  "no caveats", "just the code".

## 6. Responsibility and limits

- No filter means no safety net. The model will attempt to answer whatever it is
  asked. Whatever it generates traces back to the infrastructure it runs on.
  Deciding what is appropriate to ask, and what to do with the output, is the
  operator's responsibility.
- It is not authoritative. It hallucinates, it can be confidently wrong, and it
  has a knowledge cutoff. Verify anything that matters against primary sources.
- It is a 20B model on a single GPU. Capable, but not frontier-class. Expect
  solid reasoning and drafting, not perfect accuracy on hard technical detail.
- Access is only live while the box is running. Stop the box when done.
