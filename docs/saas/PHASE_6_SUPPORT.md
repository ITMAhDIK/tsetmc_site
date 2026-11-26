# Phase 6 — Support

## User features
- Public FAQ contains published articles only.
- Authenticated users can create tickets, list their own tickets, read their own conversation and reply.
- User replies reopen non-closed tickets. Closed tickets cannot be replied to until staff reopens them.
- Ticket ownership is enforced in SQL queries; another user's ticket is indistinguishable from a missing ticket.

## Staff features
- The support queue supports status and assignee filters, pagination and priority ordering.
- Only users with `tickets.manage` can access staff routes.
- Assignment targets must be active users who themselves hold `tickets.manage`.
- Staff can reply, add private internal notes and transition ticket status.
- Internal notes are never included in user-facing ticket reads.
- Status transitions are explicit: open → waiting/answered/closed; waiting → open/answered/closed; answered → open/waiting/closed; closed → open.
- Assignment, staff replies, internal notes, status changes and FAQ management are recorded in the admin audit log.

## FAQ management
- Public GET /support/faqs exposes published content only.
- Users with `faq.manage` can create FAQ articles and update content, ordering and publication state.
- FAQ slugs are lowercase ASCII words separated by hyphens; duplicate slugs are rejected by the database unique constraint.

## API
- GET /support/faqs
- POST /support/tickets
- GET /support/tickets
- GET /support/tickets/{ticket_id}
- POST /support/tickets/{ticket_id}/messages
- GET /support/admin/tickets
- GET /support/admin/tickets/{ticket_id}
- POST /support/admin/tickets/{ticket_id}/assign
- POST /support/admin/tickets/{ticket_id}/messages
- POST /support/admin/tickets/{ticket_id}/notes
- PATCH /support/admin/tickets/{ticket_id}/status
- POST /support/admin/faqs
- PATCH /support/admin/faqs/{faq_id}

## Validation and boundaries
- Migration `004_support.sql` adds indexes for user ticket lists, staff queues, conversation history and published FAQ lookup.
- PostgreSQL integration tests cover ownership isolation, internal-note privacy, status transitions, staff permission gates, assignment, FAQ publication and audit logging.
- All records remain in the isolated SaaS PostgreSQL database. No market-data DB, collector, public market API or GitHub Pages static preview changes.
- No email/SMS notifications or external helpdesk integration are claimed in this phase; they remain future notification work.


## Regression test coverage and phase boundaries
- PostgreSQL integration tests verify owner-only ticket reads, internal-note filtering in SQL, FAQ publication gates, staff assignment/status/reply behavior and audit records.
- The API integration test creates real Phase 2 bearer sessions, verifies owner isolation, confirms a support role can access the staff queue, confirms a normal user receives 403, and verifies revoked sessions receive 401.
- Assignment authorization is enforced both at the API boundary and inside the service; the actor and assignee must both have tickets.manage, and the assignee must be active.
- Support references the shared SaaS user and permission model from Phases 1–2. It intentionally does not couple ticket operations to Phase 3 profile fields, Phase 4 payment settlement, or Phase 5 wallet/referral ledgers. Those domains remain separate; a ticket may discuss them but support never reads or mutates their records.
- The API maps invalid internal-note text to a client validation response rather than leaking an unhandled server error.


## Additional regression coverage
- Staff queue and staff-detail reads enforce `tickets.manage` inside the service layer as well as at the API boundary.
- An end-to-end API test now covers a real user creating and replying to a ticket, another account receiving 404, a support agent assigning/replying/adding a private note/closing, FAQ publication by an authorized admin, and rejection after the staff session is revoked.
- Internal notes are asserted absent from the owner's serialized response; the same staff view can see them.
