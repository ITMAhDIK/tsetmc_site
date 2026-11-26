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
