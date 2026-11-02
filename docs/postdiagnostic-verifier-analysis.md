# Post-diagnostic verifier investigation

The frozen 1,000-case comparison shows a regression in proposal verification.
Released and improved runs produced **1,000 byte-identical raw outputs**, with
770 strict exact calls in each. Released verification reached 717 exact calls;
the frozen improved source at `e5e76b8` reached 678. These results, the original
gold calls and the independent pre-output control alternatives remain unchanged.
This investigation was performed after outputs became available. New tests and
any later cached replay are development regressions, not fresh acceptance.

The paired change is 40 lost exact calls and one recovered title, for a net loss
of 39. All 40 new damages are false clarifications: four request families lose
eight cases each, and one album-download family loses eight. The broad identity
pattern rejects ordinary self-attribution and media-class phrases, including
“as me”, “as my request” and “as a TV-series request”. The performer detector
rejects “whose performer is”. An equals-sign artist label is also rejected in
new probes even though it assigns a clear performer role.

Strict damage to originally correct model calls rises from 95 to 135:

- Both sources damage 62 correct control calls by changing the refusal reason
  or clarification slot. The result is still a blocking control, but it is not
  the correct raw prediction or the original strict label.
- False abstentions among raw-correct operational calls rise from eight to 56.
  Forty are the newly damaged calls above. Another eight negated-request library
  reads already selected the wrong operational tool in released verification;
  improved verification changes those errors into clarifications.
- Other raw-correct operational-tool changes fall from 25 to 17. The improved
  source still turns 16 explicit resume requests into transfer-list reads, and
  one current-activity request into history after reacting to a negated history
  term. The resume wordings use the positive inflection “resuming”.

Across all cases, improved verification has 75 false abstentions, 85 incorrect
control labels that remain controls, 70 decoder rejections, 47 wrong operational
tools, 13 same-tool entity errors and 18 same-tool selector errors. Fourteen
concrete proposals remain where a control was required, including the two
incorrect writes. Together with 678 exact calls, these mutually exclusive
categories account for all 1,000 inputs. The remaining entity
errors are 12 artist/album role mistakes and one filename error. Selector errors
include specials inclusion and paused/stalled transfer filters. Refusing writes
does not resolve these read errors.

The verifier also reduces incorrect write proposals from ten to two. It catches
eight conditional acquisitions but leaves two partial-track requests as album
search proposals. The scope checker removes arbitrary model-proposed entity
strings before checking restrictions. When the model calls a track selector an
album name, that removal hides the restriction. In a new isolated loopback
diagnostic, an unrelated album literally bearing the selector text could reach a
single confirmed album-ID command. No command ran before fake confirmation, and
no production service was contacted. The confirmation summary named the artist
and IDs but omitted album titles, weakening the review surface. This is a
concrete safety defect requiring repair; it is not evidence of an execution rate
in the frozen comparison, which performed no service execution.

Preserving validated control calls after hard unsafe/complex guards is a safe
direction. The current application-style second boundary can overwrite a safe
first-boundary result: a conditional acquisition becomes `unsupported(complex)`
inside inference, then `clarify(reference)` at the next check. Controls cannot
dispatch writes, so preserving their reason/slot avoids this erosion while hard
guards retain precedence. A wrong model refusal remains wrong; it must not gain
raw exact-match credit.

Universal “each/every missing episode” wording can safely mean the represented
eligible missing set. That does not permit numbered episodes, ranges, relative
selectors, exceptions or conditions, and must preserve an explicit season. An
artist assignment using `=` or an explicit “whose performer is” relationship
can likewise be recognized without inventing a performer. Containment checks
must still reject assigning an artist from inside an album title. Identity
language needs a bounded distinction between known caller-self/request-class
phrases and foreign, unknown, administrative or on-behalf identities.

Scope/quality checks must inspect the unquoted instruction before removing
arbitrary model-proposed text. Quoted literal titles remain data. The new test
file uses fresh probe entities, tests actual preflight refusal through an
isolated HTTP fixture, and requires both exact-album and artist-wide summaries
to name their selected albums. At the unchanged base `bddab3a`, its expected red
run is 24 failures and 22 passes. Planner and summary changes are owned by the
integration branch; this branch changes no runtime, training input or result.

The predeclared sensitivity stays separate: raw strict appropriate controls are
106/200; both verified runs have 68/200 strict and 76/200 under the 24-case
pre-output alternatives. No new alternatives are added after reading results.
The structured [analysis](postdiagnostic-verifier-analysis.json) records source
receipt hashes, case IDs and all 128 family summaries. The corpus remains a
correlated diagnostic candidate, with semantic scenario separation and complete
public-adapter training lineage unresolved.
