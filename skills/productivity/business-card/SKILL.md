---
name: business-card
description: "Use when capturing, storing, or retrieving business card contact data with OpenViking memory and structured contact files."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [contacts, business-cards, crm, memory, productivity]
    related_skills: [ocr-and-documents]
---

# Business Card Contact Handling

## Overview

Business card data is high-value relationship context. Treat it as both a
structured contact record and a semantic memory. The structured Markdown file is
the source of truth for exact fields such as phone number, email, company, and
title. OpenViking memory is the semantic layer for fuzzy lookup, meeting notes,
proposal links, task associations, and later recall.

When a user gives you a business card, extracted OCR text, contact details, or a
note about exchanging cards, use the Dual-Write workflow. When a user asks about
a contact or relationship, prefer the Retrieval Priority workflow for exact
lookup, and use Query Expansion when the request needs associations across
contacts, proposals, CRM records, or tasks.

## When to Use

- The user shares a business card image, OCR result, vCard, email signature, or
  typed contact details.
- The user says they exchanged a card with someone and gives meeting context.
- The user asks who a contact is, where they met, which company they represent,
  what follow-up is needed, or whether the person is tied to proposals/tasks.
- The user asks for contact lists, CRM-style summaries, or relationship history.

Do not use this skill for generic document OCR unless the output is contact or
relationship data. For raw PDF/image text extraction, use `ocr-and-documents`
first, then apply this skill to the extracted contact fields.

## Data Model

Store each contact as a Markdown file:

```markdown
# 王小明 (Wang Xiaoming)
- 公司：XYZ Corp
- 職稱：Business Development Manager
- 電話：+886-912-345-678
- Email：wang@xyz.com
- 備註：2026-05-27 在 AI Summit 交換名片
```

Use `~/.hermes/contacts/<normalized_name>.md` as the path. Normalize the file
name conservatively:

| Input | Filename |
|-------|----------|
| `王小明 (Wang Xiaoming)` | `wang-xiaoming.md` |
| `Jane Q. Lee` | `jane-q-lee.md` |
| `陳大文 / David Chen` | `david-chen.md` |
| `XYZ Corp - Sales Desk` | `xyz-corp-sales-desk.md` |

Prefer a romanized or Latin-script name when present. Otherwise use a simple
lowercase slug from the visible name. Keep only letters, numbers, and hyphens.

## Workflow 1: Dual-Write

When business card data is received, you MUST persist it in both places before
answering the user:

1. Call `viking_remember` with `category: "entity"` to store the contact and
   relationship context in OpenViking semantic memory.
2. Call `write_file` to create or update
   `~/.hermes/contacts/<normalized_name>.md` with a structured Markdown record.

These writes are one logical operation. If the tool runtime supports parallel
tool calls, issue both writes in the same assistant turn. If the runtime
serializes tool calls, still perform both before sending the final reply. Do not
only say that the contact was saved.

### Step-by-Step Example

User:

> I met 王小明 / Wang Xiaoming from XYZ Corp at AI Summit today. His title is
> Business Development Manager, phone +886-912-345-678, email wang@xyz.com.

Tool call 1:

```json
{
  "name": "viking_remember",
  "arguments": {
    "category": "entity",
    "content": "Contact: 王小明 (Wang Xiaoming), Business Development Manager at XYZ Corp. Phone: +886-912-345-678. Email: wang@xyz.com. Context: exchanged business cards on 2026-05-27 at AI Summit."
  }
}
```

Tool call 2:

```json
{
  "name": "write_file",
  "arguments": {
    "path": "~/.hermes/contacts/wang-xiaoming.md",
    "content": "# 王小明 (Wang Xiaoming)\n- 公司：XYZ Corp\n- 職稱：Business Development Manager\n- 電話：+886-912-345-678\n- Email：wang@xyz.com\n- 備註：2026-05-27 在 AI Summit 交換名片\n"
  }
}
```

Final answer after both writes succeed:

> Saved Wang Xiaoming to `~/.hermes/contacts/wang-xiaoming.md` and OpenViking
> memory.

### Updating an Existing Contact

If the user adds new information for a known contact, update both stores again.
For the Markdown file, preserve existing fields and append or revise the
relevant note instead of overwriting useful history.

Example:

```json
{
  "name": "viking_remember",
  "arguments": {
    "category": "entity",
    "content": "Update for 王小明 (Wang Xiaoming): interested in the Q3 AI operations proposal; asked for pricing follow-up by Friday."
  }
}
```

```json
{
  "name": "write_file",
  "arguments": {
    "path": "~/.hermes/contacts/wang-xiaoming.md",
    "content": "# 王小明 (Wang Xiaoming)\n- 公司：XYZ Corp\n- 職稱：Business Development Manager\n- 電話：+886-912-345-678\n- Email：wang@xyz.com\n- 備註：2026-05-27 在 AI Summit 交換名片\n- 後續：對 Q3 AI operations proposal 有興趣；週五前寄 pricing follow-up\n"
  }
}
```

## Workflow 2: Query Expansion

When the user asks about a contact and the answer may involve relationship
history, proposals, CRM entries, or tasks, search multiple OpenViking namespaces
in parallel.

First discover the available namespace layout:

```json
{
  "name": "viking_browse",
  "arguments": {
    "action": "tree",
    "path": "viking://user/"
  }
}
```

Then search the relevant namespaces. Prefer scoped searches when the namespace
exists. If `viking://user/contacts/` is not available, run a general semantic
search without `scope`.

### Parallel Search Pattern

For a question like:

> What do we know about Wang Xiaoming, and is he connected to any proposal or
> follow-up task?

Run these searches in parallel after browsing:

```json
{
  "name": "viking_search",
  "arguments": {
    "query": "Wang Xiaoming 王小明 XYZ Corp contact business card",
    "mode": "auto",
    "scope": "viking://user/contacts/",
    "limit": 5
  }
}
```

```json
{
  "name": "viking_search",
  "arguments": {
    "query": "Wang Xiaoming 王小明 XYZ Corp proposal pricing AI operations",
    "mode": "auto",
    "scope": "viking://user/proposals/",
    "limit": 5
  }
}
```

```json
{
  "name": "viking_search",
  "arguments": {
    "query": "Wang Xiaoming 王小明 XYZ Corp CRM account relationship",
    "mode": "auto",
    "scope": "viking://user/crm/",
    "limit": 5
  }
}
```

```json
{
  "name": "viking_search",
  "arguments": {
    "query": "Wang Xiaoming 王小明 XYZ Corp follow-up task next action",
    "mode": "auto",
    "scope": "viking://user/tasks/",
    "limit": 5
  }
}
```

### Reading Search Hits

If a search result contains a useful `viking://` URI but the snippet is not
enough, read it before answering:

```json
{
  "name": "viking_read",
  "arguments": {
    "uri": "viking://user/proposals/q3-ai-operations.md",
    "level": "overview"
  }
}
```

Use `level: "abstract"` for quick confirmation, `level: "overview"` for most
answers, and `level: "full"` only when exact details are needed.

### Synthesis Pattern

In the final answer, separate what came from each namespace:

```text
Wang Xiaoming is Business Development Manager at XYZ Corp.

Contact record: phone and email are present.
Proposal links: he is associated with the Q3 AI operations pricing discussion.
CRM context: no active account record found.
Tasks: pricing follow-up is due Friday.
```

If a namespace is missing or returns no hits, say that directly. Do not invent
associations.

## Workflow 3: Retrieval Priority

When looking up a contact, use this order:

1. First try `read_file("~/.hermes/contacts/<name>.md")`. This structured file
   is the source of truth for exact contact data.
2. If the file is missing or incomplete, fall back to `viking_search` for
   semantic search.
3. Synthesize both results in the final answer, making clear which source
   provided exact fields and which provided contextual memory.

Use this workflow for direct questions such as:

- "What is Wang Xiaoming's email?"
- "Who was the XYZ Corp person I met at AI Summit?"
- "Show me the contact details for David Chen."
- "Do we have a card for Jane Lee?"

### Step-by-Step Example

User:

> What is Wang Xiaoming's phone number?

Step 1:

```json
{
  "name": "read_file",
  "arguments": {
    "path": "~/.hermes/contacts/wang-xiaoming.md"
  }
}
```

If the file exists, answer from it and optionally mention any useful memory
context already known in the conversation:

```text
Wang Xiaoming's phone number is +886-912-345-678. The structured contact file
lists him as Business Development Manager at XYZ Corp.
```

If the file is missing, run semantic search:

```json
{
  "name": "viking_search",
  "arguments": {
    "query": "Wang Xiaoming 王小明 phone email XYZ Corp business card",
    "mode": "auto",
    "limit": 10
  }
}
```

If search finds likely matches, answer with confidence boundaries:

```text
I did not find `~/.hermes/contacts/wang-xiaoming.md`, but OpenViking memory has
a matching contact for 王小明 (Wang Xiaoming) at XYZ Corp with phone
+886-912-345-678.
```

### Exact Field vs Context

Use the Markdown file for exact fields:

- Name
- Company
- Title
- Phone
- Email
- Address
- Website
- Direct notes entered from the card

Use OpenViking for semantic context:

- Where and when the card was exchanged
- Follow-up intent
- Proposal or task associations
- Fuzzy name variants
- Relationship history
- Notes that were remembered but not yet written into the file

When both sources disagree, state the mismatch and ask whether to update the
structured contact file. Do not silently choose a new email or phone number from
memory over the file.

## Common Pitfalls

1. **Only writing OpenViking memory.** This loses the structured source of truth.
   Always also write `~/.hermes/contacts/<normalized_name>.md`.

2. **Only writing the Markdown file.** This makes fuzzy recall and association
   search weaker. Always also call `viking_remember(category: "entity")`.

3. **Answering before persistence finishes.** Acknowledge only after both the
   memory write and file write have succeeded, or report the failed write.

4. **Skipping namespace discovery.** Before Query Expansion, call
   `viking_browse` so you know which `viking://user/...` namespaces exist.

5. **Treating semantic hits as exact source data.** Search snippets are useful
   context, but exact phone numbers and emails should come from the structured
   contact file when available.

6. **Overwriting existing notes.** When updating a contact file, preserve useful
   historical notes and append new dated context.

7. **Using inconsistent filenames.** Normalize the same contact name the same
   way every time. Prefer the Latin-script name on the card when available.

8. **Inventing missing fields.** If OCR or user input does not include a phone,
   email, company, or title, leave it out or mark it unknown. Do not infer it.

## Verification Checklist

- [ ] Business card data triggers both `viking_remember(category: "entity")`
      and `write_file`.
- [ ] The contact file path is
      `~/.hermes/contacts/<normalized_name>.md`.
- [ ] The Markdown file contains structured fields for name, company, title,
      phone, email, and notes when available.
- [ ] Query Expansion starts with `viking_browse` to discover namespaces.
- [ ] Query Expansion searches contacts/general memory, proposals, CRM, and
      tasks when those namespaces exist.
- [ ] Direct contact lookup tries `read_file` before semantic search.
- [ ] Final answers synthesize structured contact data and OpenViking context
      without hiding source conflicts.
- [ ] Missing files, missing namespaces, and no-hit searches are reported
      plainly.
