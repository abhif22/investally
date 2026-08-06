---
name: change-reviewer
description: carry out a comprehensive review of the changes done since the last commit
---

This subagent reviews all the changes since the last commit using shell commands
IMPORTANT: you should not review the changes yourself but rather you should run the following shell command to use codex. codex is a separate AI agent that will carry out an independent review.
Run this shell command
`codex exec "Please review all the changes since the last commit and write the feedback to palnning/CHANGE_REVIEW.md"`
This will run the change review process and save the results.
Do not review yourself
