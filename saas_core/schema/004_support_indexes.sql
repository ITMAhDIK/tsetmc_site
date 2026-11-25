-- Phase 6 support indexes. Existing support tables were created in 001_initial.sql.
-- Applies only to the dedicated SaaS PostgreSQL database.
CREATE INDEX IF NOT EXISTS idx_tickets_user_updated
 ON tickets(user_id,updated_at DESC,ticket_number DESC);
CREATE INDEX IF NOT EXISTS idx_tickets_queue
 ON tickets(status,priority,updated_at DESC,ticket_number DESC);
CREATE INDEX IF NOT EXISTS idx_tickets_assignee
 ON tickets(assigned_to,status,updated_at DESC) WHERE assigned_to IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ticket_messages_ticket_created
 ON ticket_messages(ticket_id,created_at,id);
CREATE INDEX IF NOT EXISTS idx_faq_published_order
 ON faq_articles(published,sort_order,updated_at DESC);
