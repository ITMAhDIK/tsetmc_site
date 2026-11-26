# Phase 6 — Support

## Scope implemented in this foundation
- Public FAQ reads show published articles only.
- Customers can open tickets, list their own tickets, inspect ticket conversations and reply.
- Initial ticket message and ticket record are created atomically.
- A per-user daily ticket creation quota is enforced while holding the user row lock.
- Staff with `tickets.manage` can view the queue, inspect full conversations, assign tickets to active support/admin/superadmin users, change status, send public replies and add private internal notes.
- Only users with `faq.manage` can create, edit, publish or unpublish FAQ articles.
- Assignment, status changes and FAQ changes create admin audit records.
- Customer-facing ticket queries never return internal notes. The public FAQ never returns unpublished articles.

## API routes
- GET /support/faq
- GET /support/tickets
- POST /support/tickets
- GET /support/tickets/{ticket_id}
- POST /support/tickets/{ticket_id}/messages
- GET /support/manage/tickets
- GET /support/manage/tickets/{ticket_id}
- POST /support/manage/tickets/{ticket_id}/assign
- PATCH /support/manage/tickets/{ticket_id}/status
- POST /support/manage/tickets/{ticket_id}/messages
- POST /support/manage/tickets/{ticket_id}/internal-notes
- GET /support/manage/faq
- POST /support/manage/faq
- PATCH /support/manage/faq/{article_id}

## Security and data boundaries
- All persistence uses the dedicated SaaS PostgreSQL connection.
- Staff operations enforce server-side RBAC against `tickets.manage` or `faq.manage`.
- Customers can only read/reply to tickets they own.
- Internal notes are written through a separate staff-only endpoint and excluded from customer queries.
- Message content is stored as plain text; API clients must not render it as trusted HTML.
- FAQ article content is plain text/Markdown content and must be escaped/sanitized by the presentation layer.
- No market-data database, collector, API or schema is accessed or modified.

## Not included
- File attachments, outbound email/SMS notifications, SLA timers, canned responses, customer satisfaction surveys and advanced spam/device/IP scoring.
- Staff UI and customer support frontend.
- Distributed rate limiting. A database-backed per-user daily ticket quota is included.
- PostgreSQL integration, concurrency and endpoint tests have not been run.

## Validation status
Pure validation tests are included. Keep this PR Draft until PostgreSQL integration tests, RBAC test matrix, internal-note isolation tests and a security review are complete.
