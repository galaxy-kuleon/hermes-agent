---
name: data-integrity-rules
description: "Core rules for memory persistence, dual-write patterns, and retrieval strategies. Applies to all memory-related tasks."
---

# Data Integrity & Memory Rules

## Core Principle: "Trust Files, Verify Memory"
Vector search (`viking_search`) is probabilistic and can fail on specific keywords. 
Structured files (Markdown/JSON) are deterministic and serve as the **Source of Truth**.

## 1. Mandatory Dual-Write
When saving **critical data** (contacts, tasks, project specs, follow-ups, events):
1. **Save to OpenViking**: Call `viking_remember` for semantic retrieval.
2. **Save to File System**: Call `write_file` (or `patch`) to update the relevant structured Markdown file.
   - **Contacts**: `/opt/data/contacts/<name>.md`
   - **Tasks**: `/opt/data/tasks/<project>.md`
   - *Rule*: If you only do one, you have failed. Always do both.

## 2. Retrieval Priority (File First)
When answering questions about past information:
1. **Check Files First**: If you know the path to a structured file (e.g., a contact card), read it first. It is 100% reliable.
2. **Search Second**: Use `viking_search` to find missing context or semantic links.
   - *Never* claim "No information found" if a relevant file exists but wasn't read.

## 3. Query Expansion
When using `viking_search` for follow-ups, reminders, or tasks:
- **Do not** search for just one generic word (e.g., "meeting").
- **Expand keywords** to include synonyms.
  - *Example*: "Follow-up" -> Search for "Proposal", "CRM", "Task", "Next steps", "Todo".
  - *Example*: "Preference" -> Search for "Like", "Config", "Setup".

## 4. Tool Consistency
- This agent runs in a Docker environment.
- Use `viking_*` tools (e.g., `viking_remember`, `viking_search`).
- **NEVER** use `honcho_*` or local macOS memory tools; they do not exist here.
