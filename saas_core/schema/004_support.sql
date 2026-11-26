-- Phase 6 support query indexes; re-runnable and SaaS-only.
CREATE INDEX IF NOT EXISTS idx_tickets_user_updated
 ON tickets(user_id,updated_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS idx_tickets_queue_priority
 ON tickets(status,priority,updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_tickets_assignee_status
 ON tickets(assigned_to,status,updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ticket_messages_ticket_created
 ON ticket_messages(ticket_id,created_at,id);
CREATE INDEX IF NOT EXISTS idx_faq_published_order
 ON faq_articles(published,sort_order,question);
