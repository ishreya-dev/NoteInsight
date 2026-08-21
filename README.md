# Note Insight

> **Branch note:** This README documents a separate feature branch of the project — not the `main` branch. This branch focuses specifically on observability, reliability, and latency improvements to the Gemini analysis pipeline, described in detail below.

An AI-powered clinical note analysis application that extracts potential conditions, supporting evidence, documentation gaps, and a concise clinical summary from free-text notes.

This branch focuses on improving **observability, reliability, retry behavior, error handling, and understanding of latency bottlenecks** in the Gemini analysis pipeline.

> ⚠️ This application is designed as an analysis and documentation-support tool. It does not replace professional clinical judgment.

---

## What Makes This Branch Different?

The main goal of this branch was not to add more features.

Instead, the focus was on understanding:

- Where analysis latency actually comes from
- How large Gemini responses typically are
- How many conditions and documentation gaps are returned
- How token usage behaves
- Why some requests fail
- How Gemini rate limits affect the user experience
- How retries should behave when the provider explicitly tells us when to retry
- How to show meaningful errors instead of generic failure messages

The result is a more observable and resilient AI pipeline.

---

## Architecture

```text
                    ┌─────────────────┐
                    │   React Client  │
                    └────────┬────────┘
                             │
                             │ Create Note
                             ▼
                    ┌─────────────────┐
                    │     FastAPI     │
                    └────────┬────────┘
                             │
                             │ Background Analysis Job
                             ▼
              ┌──────────────────────────┐
              │    Analysis Job Service  │
              └────────────┬─────────────┘
                           │
                           ▼
              ┌──────────────────────────┐
              │      Gemini Client       │
              │                          │
              │  • Prompt construction   │
              │  • API timing            │
              │  • Retry handling        │
              │  • Schema validation     │
              │  • Response metrics      │
              └────────────┬─────────────┘
                           │
                           ▼
                    ┌─────────────┐
                    │   Gemini    │
                    │     API     │
                    └─────────────┘
                           │
                           ▼
              ┌──────────────────────────┐
              │ Structured JSON Response │
              └────────────┬─────────────┘
                           │
                           ▼
              ┌──────────────────────────┐
              │ AnalysisResult Validation│
              └────────────┬─────────────┘
                           │
                           ▼
                    ┌─────────────┐
                    │  Firestore  │
                    └─────────────┘
                           │
                           ▼
                    ┌─────────────┐
                    │ SSE / Poll  │
                    └─────────────┘
                           │
                           ▼
                    ┌─────────────┐
                    │     UI      │
                    └─────────────┘
```

---

## Core Features

- AI-powered clinical note analysis
- Structured JSON output
- Condition extraction
- Evidence quote extraction
- Documentation gap detection
- Clinical summary generation
- Evidence validation
- Retry handling
- Gemini API latency measurement
- Response-size metrics
- Token usage metrics
- Provider-aware rate-limit retry handling
- Background analysis jobs
- SSE-based analysis status updates
- Firestore persistence
- Meaningful UI error messages
- Automated tests

---

## The Main Bottleneck Investigation

Initially, the analysis sometimes took around:

```text
11 seconds
13 seconds
22 seconds
```

The first question was:

> Is the application slow, or is the Gemini API responsible for most of the latency?

To answer that, separate timing was added around the actual Gemini API request.

```text
gemini_api_completed
```

Example:

```text
gemini_api_completed attempt=1 duration_ms=13288
```

Response processing was also measured separately:

```text
gemini_response_processing_completed attempt=1 duration_ms=1
```

The complete attempt was measured as:

```text
gemini_attempt_completed attempt=1 duration_ms=13290
```

This makes the bottleneck visible.

Example interpretation:

```text
Gemini API call:        13,288 ms
Response processing:         1 ms
Total attempt:          13,290 ms
```

### Conclusion

The application-side response processing was negligible.

The majority of successful request latency was coming from the external Gemini API call.

---

## Response Metrics

Before changing prompt size, schema limits, or output-token limits, measurements were added.

The application now logs:

```text
gemini_response_metrics
```

Example:

```text
gemini_response_metrics
attempt=1
condition_count=5
gap_count=3
summary_char_count=287
max_evidence_quote_char_count=29
total_response_char_count=1703
input_tokens=1087
output_tokens=396
total_tokens=3567
```

These metrics help answer questions such as:

- Are responses actually large?
- Are we extracting too many conditions?
- Are documentation gaps excessive?
- Are evidence quotes too long?
- Is output size contributing to latency?
- What is the approximate token usage?

---

## Why These Metrics Were Added

The schema allows relatively large maximum values.

For example:

```text
Maximum conditions: 50
Maximum documentation gaps: 50
Maximum evidence quote length: 1000 characters
Maximum summary length: 2000 characters
```

However, the allowed maximum is not the same as the typical response.

Changing these limits without data could cause:

```text
Smaller limit
      ↓
Legitimate response rejected
      ↓
Validation failure
      ↓
Retry
      ↓
More latency
      ↓
Possible analysis failure
```

So instead of guessing, the application first collects measurements.

---

## Safe Logging

The metrics intentionally avoid logging clinical content.

The application does **NOT** log:

```text
❌ Clinical note text
❌ Prompt contents
❌ Patient information
❌ Evidence quotes
❌ Generated summary
❌ Full Gemini response
❌ API secrets
```

Only structural information is logged.

Example:

```text
condition_count=5
gap_count=3
summary_char_count=287
total_response_char_count=1703
```

This provides useful observability without exposing the actual clinical content.

---

## Gemini Rate Limit Problem

During testing, the Gemini API returned errors such as:

```text
429 RESOURCE_EXHAUSTED
```

Example provider response:

```text
Please retry in 15.44s
```

The original retry behavior could immediately retry after receiving this error.

That creates a problem:

```text
Request
   ↓
429 Rate Limit
   ↓
Immediate Retry
   ↓
Still Rate Limited
   ↓
Failure
```

The provider already tells us how long to wait. Ignoring that information makes the retry less useful.

---

## Provider-Aware Retry

The retry logic now detects:

```text
429
RESOURCE_EXHAUSTED
```

If Gemini provides:

```text
Please retry in 15.44s
```

The application schedules the retry using that delay.

Example:

```text
gemini_retry_scheduled
attempt=1
error_type=429
retry_delay_ms=15440
delay_source=provider
```

---

## Retry Delay Cap

Provider delays can sometimes be large.

For example:

```text
Please retry in 50.43s
```

Waiting indefinitely would create a poor user experience. Therefore, the retry delay is capped at:

```text
30 seconds
```

Example:

```text
Provider requested: 50.43 seconds
Actual retry delay:  30 seconds
```

Log:

```text
gemini_retry_scheduled
attempt=1
error_type=429
retry_delay_ms=30000
delay_source=provider
```

---

## Retry Flow

```text
                Gemini Request
                       │
                       ▼
                 ┌───────────┐
                 │ Success?  │
                 └─────┬─────┘
                       │
              ┌────────┴────────┐
              │                 │
             Yes               No
              │                 │
              ▼                 ▼
        Validate Result      Check Error
              │                 │
              ▼                 ▼
         Return Result       429 / RESOURCE?
                                  │
                         ┌────────┴────────┐
                         │                 │
                        Yes               No
                         │                 │
                         ▼                 ▼
                 Parse Retry Delay    Existing Retry
                         │
                         ▼
                   Apply 30s Cap
                         │
                         ▼
                  Async Wait
                         │
                         ▼
                      Retry
```

---

## Important Retry Rules

The implementation follows these rules:

```text
Maximum attempts: 2
```

For rate-limit errors:

```text
Provider delay available
        ↓
Use provider delay
        ↓
Cap at 30 seconds
```

If the delay cannot be parsed:

```text
Use 1 second fallback
```

On the final attempt:

```text
No unnecessary sleep
```

Non-429 errors:

```text
Existing retry behavior remains unchanged
```

---

## Why `asyncio.sleep()` Is Used

The backend is asynchronous.

Using:

```python
await asyncio.sleep(delay)
```

allows the application to wait without blocking the event loop.

This is better than:

```python
time.sleep(delay)
```

because `time.sleep()` blocks the current execution thread.

---

## Timing Boundaries

The implementation separates API time from retry waiting time.

For example:

```text
gemini_api_completed attempt=1 duration_ms=235
```

The provider-aware wait happens after the API request. Then:

```text
gemini_attempt_completed attempt=1 duration_ms=2135
```

This means:

```text
API call:       235 ms
Retry delay:   1893 ms
Total:         2135 ms
```

This distinction is important. Otherwise, a retry delay could incorrectly appear as Gemini API latency.

---

## Example Successful Analysis

```text
analysis_job_started

gemini_api_completed
duration_ms=13288

gemini_response_metrics
condition_count=5
gap_count=3
summary_char_count=287
total_response_char_count=1703

gemini_response_processing_completed
duration_ms=1

gemini_attempt_completed
duration_ms=13290

persist_analysis_for_note
duration_ms=312

analysis_job_completed
total_duration_ms=13681
```

This shows the complete breakdown.

---

## Example Rate-Limited Analysis

```text
gemini_api_completed
duration_ms=723

429 RESOURCE_EXHAUSTED

gemini_retry_scheduled
attempt=1
retry_delay_ms=30000
delay_source=provider

gemini_attempt_completed
duration_ms=30702
```

The first API request itself was fast. The long attempt duration came from the intentional provider-aware retry delay.

---

## User-Facing Error Handling

Previously, a rate-limit failure could appear as a generic error such as:

```text
Gemini did not return valid output after 2 attempts
```

This is misleading when the actual problem is:

```text
429 RESOURCE_EXHAUSTED
```

The UI now distinguishes the failure type.

For rate-limit failures:

```text
Analysis temporarily unavailable

The AI service is currently receiving too many requests.
Please wait a moment and try again.
```

The user can then:

```text
Retry analysis
```

or:

```text
Start a different note
```

---

## Why This Matters

These failures are different:

```text
Invalid model response
```

and:

```text
Provider rate limit
```

They should not produce the same user-facing message.

Better error classification improves:

- User experience
- Debugging
- Observability
- Supportability

---

## Prompt and Output Investigation

The current analysis prompt contains:

```text
Fixed prompt instructions
        +
Clinical note text
```

The note can be up to:

```text
20,000 characters
```

The prompt uses:

```text
temperature=0.2
response_mime_type="application/json"
structured response schema
```

There is currently no explicit `max_output_tokens` limit. This was intentionally not changed without production measurements.

---

## Why `max_output_tokens` Was Not Added Yet

A low output limit could cause:

```text
Response generation
        ↓
Output truncated
        ↓
Invalid JSON
        ↓
Schema validation failure
        ↓
Retry
        ↓
Higher latency
```

The correct approach is:

```text
Measure actual output
        ↓
Collect token usage
        ↓
Analyze response distribution
        ↓
Choose a safe limit
        ↓
Test
```

The newly added response metrics support this process.

---

## Example Test Notes

The following types of notes were used to test different output sizes and behaviors.

### 1. Short Note

```text
Patient reports a mild sore throat for two days. No fever or shortness of breath. Advised hydration and rest.
```

Useful for testing:

- Small input
- Small output
- Basic condition extraction

### 2. Medium Note

```text
Patient presents with cough, nasal congestion, fatigue, and intermittent fever for five days. The patient reports reduced appetite but is able to tolerate fluids. No chest pain or shortness of breath. Physical examination notes mild throat redness. Supportive care was recommended, and the patient was advised to return if symptoms worsen.
```

Useful for testing:

- Moderate input size
- Multiple symptoms
- Evidence extraction
- Documentation analysis

### 3. Longer Note

```text
Patient reports progressive fatigue over the last three weeks along with intermittent headaches and difficulty concentrating. Sleep has been irregular because of increased work-related stress. The patient also reports occasional dizziness when standing quickly but denies loss of consciousness. Appetite has decreased slightly, although fluid intake remains normal.

Past medical history includes no known chronic illnesses. The patient is not currently taking regular medication. No recent travel was reported. The patient denies chest pain, shortness of breath, persistent vomiting, or neurological weakness.

On examination, the patient was alert and oriented. Blood pressure was documented as mildly elevated during the visit. Heart rate and oxygen saturation were within normal limits. No acute distress was observed.

The plan includes monitoring symptoms, improving sleep habits, maintaining hydration, and arranging follow-up if dizziness, headaches, or fatigue continue or worsen.
```

Useful for testing:

- Larger prompt
- Multiple clinical signals
- Longer summary
- Multiple evidence quotes
- Documentation gaps

### 4. Multiple Conditions and Documentation Gaps

```text
Patient reports persistent cough for approximately three weeks with intermittent wheezing and fatigue. The patient also reports episodes of heartburn after meals and difficulty sleeping because of coughing at night.

The patient has a history of seasonal allergies but no other medical history is documented. Current medication information is not available. Smoking history is not documented.

During the visit, the patient denied chest pain. Oxygen saturation was recorded as normal. No temperature was documented. Lung examination findings were not included.

The patient was advised to increase fluid intake and follow up if symptoms worsen. No clear follow-up timeframe was documented.
```

Useful for testing:

- Multiple possible conditions
- Multiple evidence quotes
- Multiple documentation gaps

Potential missing information includes:

```text
Smoking history
Current medications
Temperature
Lung examination findings
Follow-up timeframe
```

---

## Testing Strategy

The Gemini client tests cover:

```text
✓ Successful response metrics
✓ Optional token metadata
✓ Missing token metadata
✓ No metrics for failed attempts
✓ Metrics only for successful retry attempts
✓ Provider retry delay parsing
✓ Retry delay cap
✓ Fallback delay
✓ No sleep after final attempt
✓ Non-429 behavior unchanged
✓ Structured retry logging
```

Current test status:

```text
Focused Gemini tests: 23 passed
Related Gemini + SSE tests: 36 passed
Full backend suite: 115 passed
```

---

## Bottlenecks Identified

### 1. External Model Latency

Successful analysis latency is primarily dominated by:

```text
Gemini API request time
```

rather than:

```text
JSON processing
Schema validation
Evidence processing
```

### 2. Provider Rate Limits

The free-tier request quota can produce:

```text
429 RESOURCE_EXHAUSTED
```

This is an external provider constraint. The application can handle the response intelligently, but it cannot remove the provider quota.

### 3. Retry Waiting Time

A provider-aware retry may increase the duration of a single analysis attempt.

Example:

```text
API request: 700 ms
Provider retry wait: 30,000 ms
Second request: 600 ms

Total: ~31 seconds
```

This is intentional. The delay avoids immediately sending another request that is likely to fail.

---

## Troubleshooting

### Analysis Takes 10–20 Seconds

Check:

```text
gemini_api_completed
```

If this duration is large while `gemini_response_processing_completed` remains close to 1–5 ms, then the external model call is the primary bottleneck.

### Analysis Takes Around 30 Seconds

Check for:

```text
gemini_retry_scheduled
```

Example:

```text
retry_delay_ms=30000
```

This means the provider returned a rate-limit response and the retry delay was capped at 30 seconds.

### Analysis Fails Immediately

Check for:

```text
429 RESOURCE_EXHAUSTED
```

If both attempts fail, the provider quota is still exhausted. The application should show:

```text
Analysis temporarily unavailable
```

rather than incorrectly reporting an invalid model response.

### Token Metrics Look Unexpected

Check:

```text
input_tokens
output_tokens
total_tokens
```

These fields are provider metadata and may not always follow a simple `input_tokens + output_tokens = total_tokens` relationship, because providers may account for additional internal token categories. The application records the values reported by the SDK rather than estimating them.

---

## Bonus Features

The project can be extended with:

- Streaming analysis into the UI
- Caching identical notes
- Inline evidence highlighting
- Expanded failure-path tests
- Human correction metrics
- PDF note upload
- Image note upload
- Per-user rate limiting

For the current branch, the priority remains:

```text
Core requirements
        +
Reliable analysis
        +
Meaningful error handling
        +
Good observability
```

PDF and image upload support is intentionally not part of this implementation.

---

## Tech Stack

**Frontend**
```text
React
TypeScript
Vite
```

**Backend**
```text
Python
FastAPI
Uvicorn
Pydantic
```

**AI**
```text
Google Gemini API
Structured JSON Output
```

**Data**
```text
Firestore
```

**Testing**
```text
Pytest
```

---

## Key Design Principle

This branch follows a measurement-first approach.

Instead of immediately changing:

```text
Prompt size
Schema limits
Output token limits
Retry strategy
```

the application first collects enough information to understand the real behavior.

```text
Measure
   ↓
Identify bottleneck
   ↓
Make targeted change
   ↓
Test
   ↓
Measure again
```

This reduces the risk of making an optimization that improves one metric while breaking reliability somewhere else.

---

## Summary

The important improvement in this branch is not simply faster AI analysis. It is the ability to understand what is happening.

The application can now distinguish between:

```text
Slow provider response
Fast provider response + application processing
Large response
Rate-limited request
Retry waiting time
Invalid model output
```

This makes future optimization based on real measurements instead of assumptions.

---

## Current Focus

```text
Reliable core functionality
        +
Clear failure handling
        +
Latency visibility
        +
Response metrics
        +
Provider-aware retries
        +
Strong automated tests
```

> A complete and reliable implementation is more valuable than adding optional features that make the system harder to maintain.