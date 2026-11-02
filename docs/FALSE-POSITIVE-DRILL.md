# Parent-led false-positive response drill

Run this tabletop before taking BanTheFake live. It rehearses how to respond if the scanner or campaign wrongly
describes media. Use a fictional case and dummy text only: do not use intimate media, an image of a child, a real
person's disputed photo, or anyone's private information.

A parent may be the **incident lead only when they are a safe trusted adult for the affected person**. Otherwise a
different safe trusted adult or safeguarding lead takes over. The incident lead decides what is published, handles
contact with the person affected, and stops the exercise if it becomes a real safeguarding, legal, privacy or
security matter. The student is the **technical lead and scribe**: they collect the minimum facts, reproduce safe
technical behavior, draft the correction and record follow-up work. A third person may play the reporter, but the
safe trusted adult can read the injects instead.

## Ground rules

- A scan is an indicator, not an accusation or a fact about the person shown.
- No participant may say the scanner proves media is fake, real or authentic.
- Protect the affected person before protecting the project. Stop repeating the disputed claim while checking it.
- Collect the result text and public context. Do not ask the reporter for the original file; use an existing
  licence-compatible fixture or a synthetic, non-sensitive substitute for technical checks.
- Keep the case record private. Record no more personal information than the response needs.
- Do not promise a technical answer when the honest outcome is unresolved.

## Scenario

A fictional reporter says BanTheFake labelled an ordinary, edited photograph as “Likely AI-generated.” A campaign
account then reposted the result with wording that named the person in the picture and treated the model result as
proof. The reporter asks for the post to be removed and the record corrected.

Use this dummy intake:

```text
Where: campaign post linking to a BanTheFake scan
Result text: “Likely AI-generated”
When: today, during the drill
Reporter says: the image is an ordinary photograph with routine colour and crop edits
Public reach: several people have reshared the campaign post
Permission to quote the report: no
Media supplied: none
```

## Phase 1: intake and immediate containment

The safe trusted adult reads the dummy intake. The team must:

1. Acknowledge the report without defending the model or asking the reporter to prove the image is real.
2. Assign a local case label that contains no person's name.
3. Save the exact BanTheFake wording and location of the campaign statement.
4. Remove the campaign post or mark it clearly as disputed while the review runs. Do not silently edit away the
   history if a correction can be attached safely.
5. Confirm that no one has downloaded or forwarded sensitive media.

Suggested acknowledgement:

> Thank you for telling us. A BanTheFake result is an automated indicator, not proof or an accusation. We have
> stopped repeating the disputed claim while a safe-adult-led review checks what happened. Please do not send the
> original file if it is sensitive; the result text and a safe description are enough to start.

Pass when the affected person is no longer being exposed to a fresh claim from a BanTheFake-controlled account.

## Phase 2: verify the claim safely

The student records, without the original media:

- the exact verdict and signal wording;
- whether it came from the site, extension, bot, documentation or a campaigner paraphrase;
- the deployed commit or release, if available;
- date and approximate time;
- the public statements BanTheFake controls; and
- what evidence is missing.

Check whether the public claim exceeded the scanner's own wording. A model result may be correctly displayed while
the campaign still made a serious error by presenting it as proof. Do not infer that a screenshot is forged merely
because the result cannot be reproduced.

Reproduce decision logic with an existing licence-compatible fixture or a synthetic, non-sensitive substitute on
the same release and the current release. Do not request private or intimate media. Save the result text and
versions, not extra media copies. Never add private or restrictively licensed media to the repository.

Classify the case as one of:

- **scanner false positive** — the model produced a mistaken AI signal;
- **wording or interface problem** — the product made uncertainty too easy to miss;
- **campaign misuse** — someone turned a cautious result into a factual allegation;
- **not a BanTheFake result** — supported by evidence, without accusing the reporter; or
- **unresolved** — evidence is insufficient. This is not evidence that the person or media is deceptive.

## Phase 3: escalation injects

The safe trusted adult introduces these one at a time. The student states the immediate action and who owns it.

1. **The person shown is under 18.** Stop technical evidence gathering from the person, do not request the file,
   and move to a safe trusted adult or safeguarding lead. Use the support and reporting routes on the campaign
   site when relevant.
2. **The media is intimate or accompanied by threats, harassment or doxxing.** Do not view, copy or redistribute
   it for the drill. Preserve safe non-media facts only, involve a safe trusted adult or safeguarding lead, and use
   specialist help. Immediate danger goes to local emergency services.
3. **A journalist or election campaign asks for an instant verdict.** The safe trusted adult owns the response.
   Say the result is disputed and under review; do not speculate or repeat the allegation.
4. **The reporter sends a legal demand.** Preserve the request, stop informal argument and have the safe trusted
   adult obtain appropriate advice. Do not promise an outcome the project cannot deliver.
5. **The report reveals leaked files, exposed personal data or a way to make the service disclose information.**
   Treat it as a privacy or security incident. Stop the affected feature if needed and move technical details to a
   private channel rather than a public issue.
6. **Several unrelated ordinary images show the same error.** Disable or clearly withdraw the affected claim or
   model path pending review; do not tune against the reports and immediately declare the problem solved.
7. **The affected child says a parent is unsafe.** Do not contact that parent automatically. Stop questioning the
   child and move to a different safe trusted adult or safeguarding lead, sharing only what is needed to help.
8. **A reporter uses the process to accuse a named person.** Do not repeat the allegation, publish the name or
   treat the scan as evidence against them. Remove identifying details and handle only the scanner-quality issue.
9. **A public issue contains doxxing or harmful media.** Do not download or forward it. Limit further exposure
   using the platform's moderation controls, keep only a minimal reference, and move safeguarding, privacy or
   security handling to the appropriate private route.

The drill fails if anyone asks a child or affected person to resend harmful media, publicly debates whether they are
lying, or describes an automated score as proof.

## Phase 4: decide and correct

The safe trusted adult chooses one classification from Phase 2. Draft a correction for the original audience:

```text
Correction: BanTheFake previously described this media using an automated scan result. That result was not proof
and should not have been presented as a fact. We have removed or corrected our statement. Our review found
[scanner false positive / misleading wording / campaign misuse / unresolved result]. We are sorry.
[State what changed and what remains uncertain. Never declare the file or depicted scene real. If separate
evidence supports a precise provenance statement, state only that narrow fact and its limits.]
```

The correction should be at least as easy to find as the original BanTheFake claim. Contact the reporter privately
with the same conclusion, without sharing internal personal information or blaming another person.

Without the exact, safely shareable media and independent ground truth, keep the file-specific outcome unresolved.
A synthetic substitute may reveal a general failure mode, but it cannot prove the reported file was a false
positive. Correct campaign misuse or misleading wording independently of that unresolved technical question.

## Phase 5: follow-up record

Complete this private template:

```text
Case label:
Safe trusted adult or safeguarding lead:
Date opened / closed:
BanTheFake-controlled locations affected:
Exact original wording:
Containment action:
Classification and evidence:
Correction text and locations:
People or services escalated to:
Personal data retained, reason and deletion date:
Code, wording, test or documentation follow-up:
Owner and check-back date:
```

A code regression needs a consented, licence-compatible and non-sensitive fixture or a synthetic reproduction.
If no safe fixture exists, document the limitation and test the decision logic without storing the reported media.

## Debrief and pass criteria

The safe trusted adult asks:

- Did we stop our own harmful claim before debating the model?
- Could a reporter use the public report template without sending sensitive media?
- Did every public draft distinguish an estimate from proof?
- Did we know when a safe trusted adult, safeguarding lead, emergency services, legal advice or private security
  reporting should take over?
- Did we correct the original audience and create a concrete prevention task?

The drill passes when each answer is yes and the completed case record contains no real person's data. Record gaps
as tasks and repeat the affected phase after fixing them.
