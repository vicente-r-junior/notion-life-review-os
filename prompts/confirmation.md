You are building a WhatsApp confirmation message for a productivity assistant.
Show the user what was captured and ask them to confirm before saving to Notion.

Payload to summarize: {payload}

Format the message like this (adapt based on what's present):

"Here's what I captured:

Tasks (N):
  - [task title] — due [date] | [custom fields if any]

Projects (N):
  - [project name] -> '[progress note]'

Learnings (N):
  - [insight]

Mood: X/5 | Energy: [level]
[one-line summary]

Reply *confirm* to save to Notion, or *cancel* to discard."

Rules:
- Only show sections that have content. Skip empty ones.
- Include custom fields naturally in the task line.
- Keep it concise. No markdown headers. Plain text with simple formatting.
- Do not add any text before or after the message.
