-- Preserve deterministic event order within each authenticated browser session.
-- occurred_at is the server receive timestamp; several events in one batch can
-- share a timestamp, so route reconstruction must use this monotonic sequence.
CREATE SEQUENCE IF NOT EXISTS public.analytics_event_order_seq AS BIGINT;

ALTER TABLE analytics_events
 ADD COLUMN IF NOT EXISTS event_order BIGINT;

ALTER TABLE analytics_events
 ALTER COLUMN event_order
 SET DEFAULT nextval('public.analytics_event_order_seq'::regclass);

UPDATE analytics_events
 SET event_order=nextval('public.analytics_event_order_seq'::regclass)
 WHERE event_order IS NULL;

ALTER TABLE analytics_events
 ALTER COLUMN event_order SET NOT NULL;

ALTER SEQUENCE public.analytics_event_order_seq
 OWNED BY analytics_events.event_order;

SELECT setval(
 'public.analytics_event_order_seq'::regclass,
 GREATEST((SELECT COALESCE(MAX(event_order),0)+1 FROM analytics_events),1),
 FALSE
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_analytics_event_order
 ON analytics_events(event_order);

CREATE INDEX IF NOT EXISTS idx_analytics_session_event_order
 ON analytics_events(user_id,session_id,event_order)
 INCLUDE(page_path,occurred_at)
 WHERE user_id IS NOT NULL
   AND session_id IS NOT NULL
   AND event_name='page_view';
