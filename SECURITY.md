# Security policy

## Reporting a vulnerability
Please do **not** open a public issue for a security problem. Use GitHub's private vulnerability reporting
("Security" tab → "Report a vulnerability") on this repository. Include what you found, how to reproduce it, and
the impact. We aim to acknowledge reports within a week.

Especially welcome: any way to read or write a silo you should not reach, to bypass the token gate, to make a
regex or tool call consume unbounded time or memory, or to get private data past the scrubber.

## Scope and known limits
- The server enforces silo access for connector requests. Operators with direct storage access can read every silo
  (STANDARDS 6b); that is by design and documented to users.
- Prompt-injected writes by a user's own assistant are an open design question (docs/roadmap.md, item 1).
- The scrubber is a backstop, not the control: STANDARDS 4 says what must never be written.

## Supported versions
Only the latest `main` is supported.
